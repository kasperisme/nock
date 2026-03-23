import asyncio
import logging
import os
import threading
from typing import Any, Dict

import httpx

from config import (
    _error,
    _ok,
)
from slack_slash import (
    _run_slash_command,
    _web_url_to_rest_url,
)

logger = logging.getLogger(__name__)


async def _run_interaction_worker(args: Dict) -> Dict:
    """Run slash command (/nock <text>) and post result via chat.update (Log it button completion)."""
    text = (args.get("text") or "").strip()
    upd = args.get("_interaction_update") or {}
    channel = upd.get("channel", "")
    message_ts = upd.get("message_ts", "")
    if not text or not channel or not message_ts:
        logger.warning("Interaction worker missing text/channel/message_ts")
        return _error(400, "Missing interaction params")
    worker_args = {
        **args,
        "text": text,
        "response_url": "",  # Not used; _interaction_update triggers chat.update
        "_interaction_update": upd,
    }
    return await _run_slash_command(worker_args)


async def _handle_slack_interaction(args: Dict) -> Dict:
    """
    Handle Slack Interactivity payloads (Log it / Dismiss buttons from event confirmation).
    Must respond within 3 seconds. For Log it, we ack immediately and process in background.
    """
    payload = args.get("_interaction_payload") or {}
    if payload.get("type") != "block_actions":
        return _error(400, "Unsupported interaction type")

    logger.info("Slack interaction block_actions received")
    actions = payload.get("actions") or []
    if not actions:
        return _ok({"body": ""})

    action = actions[0]
    action_id = action.get("action_id", "")
    value = action.get("value", "")

    if action_id in ("event_dismiss", "event_clarify_no"):
        response_url = payload.get("response_url", "").strip()
        if response_url:
            try:
                with httpx.Client(timeout=5.0) as client:
                    client.post(
                        response_url,
                        json={"delete_original": True},
                    )
            except Exception as e:
                logger.warning("Dismiss response_url failed: %s", e)
        return _ok({"body": ""})

    if (
        action_id in ("event_confirm_log", "event_clarify_yes")
        and value
        and value != "dismiss"
    ):
        response_url = payload.get("response_url", "").strip()
        team = payload.get("team") or {}
        user = payload.get("user") or {}
        team_id = team.get("id", "")
        user_id = user.get("id", "")
        channel = (payload.get("channel") or {}).get("id") or (
            payload.get("container") or {}
        ).get("channel_id", "")
        message_ts = (payload.get("container") or {}).get("message_ts") or (
            payload.get("message") or {}
        ).get("ts", "")
        bot_token = os.environ.get("SLACK_BOT_TOKEN", "").strip()

        if not response_url:
            logger.warning("Interaction event_confirm_log: no response_url in payload")
            return _ok({"body": ""})

        # Fetch the original message to ensure we have correct channel/ts (handles thread vs channel)
        thread_ts = None
        if bot_token and channel and message_ts:
            try:
                with httpx.Client(timeout=5.0) as client:
                    r = client.get(
                        "https://slack.com/api/conversations.replies",
                        params={
                            "channel": channel,
                            "ts": message_ts,
                            "limit": 1,
                            "inclusive": "true",
                        },
                        headers={"Authorization": f"Bearer {bot_token}"},
                    )
                if r.status_code == 200:
                    data = r.json()
                    if data.get("ok") and data.get("messages"):
                        msg = data["messages"][0]
                        message_ts = msg.get("ts", message_ts)
                        thread_ts = msg.get("thread_ts")
                        logger.info(
                            "Fetched message: channel=%s ts=%s thread_ts=%s",
                            channel,
                            message_ts,
                            thread_ts,
                        )
                    else:
                        logger.warning(
                            "conversations.replies returned no message: %s",
                            data.get("error", data)[:100],
                        )
                else:
                    logger.warning("conversations.replies failed: %s", r.status_code)
            except Exception as e:
                logger.warning("Failed to fetch message: %s", e)

        # Immediately replace the interaction box with "Processing your request..."
        try:
            with httpx.Client(timeout=5.0) as client:
                client.post(
                    response_url,
                    json={
                        "replace_original": True,
                        "text": "*CRM*\nProcessing your request...",
                    },
                )
        except Exception as e:
            logger.warning("Interaction response_url (processing msg) failed: %s", e)

        logger.info(
            "Interaction event_confirm_log: processing text=%s team=%s",
            value[:80],
            team_id,
        )
        # Run as slash command: /nock <text> with result posted via chat.update
        async_url = os.environ.get("DO_SLACK_ASYNC_URL", "").strip()
        async_token = os.environ.get("DO_SLACK_ASYNC_TOKEN", "").strip()
        worker_payload = {
            "text": value,
            "response_url": "",
            "team_id": team_id,
            "user_id": user_id,
            "channel_id": channel,
            "slack_context": {"team_id": team_id, "user_id": user_id},
            "_interaction_worker": 1,
            "_worker_token": async_token,
            "_interaction_update": {
                "channel": channel,
                "message_ts": message_ts,
                "thread_ts": thread_ts,
            },
        }
        if async_url and async_token:
            base = _web_url_to_rest_url(async_url)
            invoke_url = f"{base}?blocking=false"
            auth = (
                async_token
                if async_token.startswith("Bearer ") or async_token.startswith("Basic ")
                else f"Basic {async_token}"
            )
            try:
                with httpx.Client(timeout=5.0) as client:
                    r = client.post(
                        invoke_url,
                        json=worker_payload,
                        headers={
                            "Content-Type": "application/json",
                            "Authorization": auth,
                        },
                    )
                if r.status_code in (200, 202):
                    logger.info("Interaction event_confirm_log: async invoke started")
                else:
                    logger.warning(
                        "Interaction async invoke failed: %s %s",
                        r.status_code,
                        r.text[:200],
                    )
            except Exception as e:
                logger.warning("Interaction async invoke failed: %s", e)
        else:
            # No async URL: run in thread (may be killed before completion on serverless)
            def _run_and_post():
                try:
                    asyncio.run(_run_interaction_worker(worker_payload))
                except Exception as e:
                    logger.warning("Interaction background run failed: %s", e)

            t = threading.Thread(target=_run_and_post, daemon=True)
            t.start()

    return _ok({"body": ""})
