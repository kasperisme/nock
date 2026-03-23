import base64
import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx
from anthropic import AsyncAnthropic

from config import (
    EVENT_BORDERLINE_LOW,
    EVENT_CONFIDENCE_THRESHOLD,
    MODEL_RELEVANCE_SCORE,
    _error,
    _ok,
    _load_slack_event_scoring_prompt,
)
from crm_logger import get_and_clear_pending_reply, get_notion_connection
from notion_client import NotionClient
from notion_utils import _resolve_settings_path
from slack_slash import _run_slash_command, _web_url_to_rest_url

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Workspace context cache — keyed by team_id, TTL 15 minutes
# ---------------------------------------------------------------------------

_CONTEXT_CACHE_TTL = 900  # seconds
_workspace_context_cache: Dict[str, Tuple[str, float]] = {}


async def _load_workspace_context(team_id: str) -> str:
    """
    Load a condensed workspace context string for the given Slack team.

    Combines:
      - The names and purposes of all registered Notion databases
        (from Settings/Database or list_databases)
      - The user's Settings/Context page (their CRM instructions)

    Result is cached per team_id for CONTEXT_CACHE_TTL seconds to avoid
    a Notion round-trip on every incoming Slack message.

    Returns an empty string on any error so the scorer falls back to generic keywords.
    """
    now = time.monotonic()
    cached = _workspace_context_cache.get(team_id)
    if cached and now - cached[1] < _CONTEXT_CACHE_TTL:
        return cached[0]

    try:
        connection = get_notion_connection(slack_team_id=team_id)
        if not connection:
            return ""
        token = connection.get("access_token", "")
        settings_path = connection.get("settings_path") or ""
        if not token:
            return ""

        notion = NotionClient(token=token)
        parts: List[str] = []

        # 1. Database list — tells the scorer what kinds of things this workspace tracks
        try:
            databases = await notion.list_databases()
            if databases:
                db_names = [
                    (db.get("title") or db.get("name") or "")
                    for db in databases
                    if db.get("title") or db.get("name")
                ]
                if db_names:
                    parts.append("Databases in this workspace: " + ", ".join(db_names))
        except Exception as exc:
            logger.debug("Could not load database list for scoring context: %s", exc)

        # 2. Settings/Context page — user's own CRM instructions and conventions
        if settings_path:
            try:
                path = _resolve_settings_path(settings_path)
                page_id = await notion.find_page_by_path(path)
                if page_id:
                    content = await notion.get_page_content_as_text(page_id)
                    if content and content.strip():
                        parts.append("CRM instructions:\n" + content.strip()[:800])
            except Exception as exc:
                logger.debug("Could not load Settings/Context for scoring: %s", exc)

        context = "\n\n".join(parts)
        _workspace_context_cache[team_id] = (context, now)
        return context

    except Exception as exc:
        logger.debug("Workspace context load failed for team %s: %s", team_id, exc)
        return ""


def _verify_slack_signature(args: Dict) -> bool:
    """Verify X-Slack-Signature using SLACK_SIGNING_SECRET."""
    secret = os.environ.get("SLACK_SIGNING_SECRET", "").strip()
    if not secret:
        return True  # signature checking disabled (dev mode)
    headers = args.get("__ow_headers") or (args.get("http") or {}).get("headers") or {}
    timestamp = headers.get("x-slack-request-timestamp", "")
    signature = headers.get("x-slack-signature", "")
    body_str = args.get("__ow_body_raw", "")
    if not body_str:
        raw_body = args.get("__ow_body", "")
        if raw_body:
            try:
                body_str = base64.b64decode(raw_body).decode("utf-8")
            except Exception:
                body_str = raw_body if isinstance(raw_body, str) else ""
    sig_basestring = f"v0:{timestamp}:{body_str}".encode("utf-8")
    expected = (
        "v0="
        + hmac.new(secret.encode("utf-8"), sig_basestring, hashlib.sha256).hexdigest()
    )
    return hmac.compare_digest(expected, signature)


async def _score_crm_relevance(
    text: str, workspace_context: str = ""
) -> Tuple[float, str, List[str]]:
    """
    Use Claude to score a Slack message for CRM relevance.
    Returns (score 0-1, reason, extracted entity names).

    workspace_context — optional string containing database names and
    Settings/Context content for this team.  When provided, the scoring
    prompt is enriched so the scorer calibrates against what THIS workspace
    actually tracks rather than generic CRM keywords.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return 0.0, "", []
    client = AsyncAnthropic(api_key=api_key)
    system_prompt = _load_slack_event_scoring_prompt(workspace_context)

    user_prompts = [
        f'Message: "{text}"',
        f'Score CRM relevance 0-1 for: "{text}"',
    ]

    for user_content in user_prompts:
        try:
            resp = await client.messages.create(
                model=MODEL_RELEVANCE_SCORE,
                max_tokens=2048,
                system=system_prompt,
                messages=[{"role": "user", "content": user_content}],
            )
            content = ""
            for block in resp.content or []:
                if hasattr(block, "text"):
                    content += block.text
                elif isinstance(block, dict) and block.get("type") == "text":
                    content += block.get("text", "")
            content = content.strip()
            if not content:
                logger.warning(
                    "CRM relevance scoring: empty response (stop_reason=%s), trying next prompt",
                    getattr(resp, "stop_reason", "unknown"),
                )
                continue
            if content.startswith("```"):
                content = re.sub(r"^```(?:json)?\s*", "", content)
                content = re.sub(r"\s*```$", "", content).strip()
            result = json.loads(content)
            score = float(result.get("score", 0.0))
            reason = result.get("reason", "")
            entities = result.get("entities") or []
            return score, reason, entities
        except Exception as e:
            logger.warning("CRM relevance scoring failed: %s", e)
            continue
    return 0.0, "", []


async def _post_event_confirmation(
    bot_token: str,
    channel: str,
    thread_ts: str,
    message_text: str,
    reason: str,
    entities: List[str],
) -> None:
    """Post a thread confirmation with confirm / dismiss buttons."""
    entity_strs = [
        (e.get("name") or e.get("entity") or str(e) if isinstance(e, dict) else str(e))
        for e in (entities or [])[:2]
    ]
    entity_str = ", ".join(str(s) for s in entity_strs if s) or "this conversation"
    fallback_text = (
        f"Looks like this is about {entity_str} — want me to log it to the CRM?"
    )
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"Looks like this is about *{entity_str}* — "
                    f"want me to log it to the CRM?\n_{reason}_"
                ),
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Log it"},
                    "style": "primary",
                    "action_id": "event_confirm_log",
                    "value": message_text[:500],
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Dismiss"},
                    "action_id": "event_dismiss",
                    "value": "dismiss",
                },
            ],
        },
    ]
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            "https://slack.com/api/chat.postMessage",
            headers={
                "Authorization": f"Bearer {bot_token}",
                "Content-Type": "application/json",
            },
            json={
                "channel": channel,
                "thread_ts": thread_ts,
                "text": fallback_text,
                "blocks": blocks,
            },
        )
        if resp.status_code != 200 or not resp.json().get("ok"):
            logger.warning("chat.postMessage failed: %s", resp.text[:200])


async def _post_clarification_question(
    bot_token: str,
    channel: str,
    thread_ts: str,
    message_text: str,
    reason: str,
    entities: List[str],
) -> None:
    """Post a thread question asking whether the borderline message should be logged."""
    entity_strs = [
        (e.get("name") or e.get("entity") or str(e) if isinstance(e, dict) else str(e))
        for e in (entities or [])[:2]
    ]
    entity_str = ", ".join(str(s) for s in entity_strs if s) or "this conversation"
    fallback_text = f"Is this about {entity_str}? Should I log it to the CRM?"
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"Is this about *{entity_str}*? Should I log it to the CRM?\n_{reason}_"
                ),
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Yes, log it"},
                    "style": "primary",
                    "action_id": "event_clarify_yes",
                    "value": message_text[:500],
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "No thanks"},
                    "action_id": "event_clarify_no",
                    "value": "dismiss",
                },
            ],
        },
    ]
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            "https://slack.com/api/chat.postMessage",
            headers={
                "Authorization": f"Bearer {bot_token}",
                "Content-Type": "application/json",
            },
            json={
                "channel": channel,
                "thread_ts": thread_ts,
                "text": fallback_text,
                "blocks": blocks,
            },
        )
        if resp.status_code != 200 or not resp.json().get("ok"):
            logger.warning(
                "chat.postMessage (clarification) failed: %s", resp.text[:200]
            )


async def _handle_message_event(event: Dict, team_id: str) -> None:
    """Score a Slack message for CRM relevance and optionally post a thread confirmation."""
    # Ignore bot messages and message subtypes (edits, deletions, etc.)
    if event.get("bot_id") or event.get("subtype"):
        return

    text = (event.get("text") or "").strip()
    channel = event.get("channel", "")
    ts = event.get("ts", "")
    user_id = event.get("user", "")

    if not text or not channel or not ts:
        return

    # Check if the agent is waiting for a reply from this user in this channel
    if team_id and user_id:
        pending = get_and_clear_pending_reply(
            slack_team_id=team_id, slack_user_id=user_id
        )
        if pending:
            pending_channel, tool_use_id = pending
            if pending_channel == channel:
                logger.info(
                    "Routing event reply to agent: team=%s user=%s channel=%s tool_use_id=%s",
                    team_id, user_id, channel, bool(tool_use_id),
                )
                await _run_slash_command({
                    "text": text,
                    "team_id": team_id,
                    "user_id": user_id,
                    "channel_id": channel,
                    "_reply_channel": channel,
                    "_reply_thread_ts": ts,
                    "_ask_user_tool_use_id": tool_use_id,
                })
                return

    # Load workspace context (databases + Settings/Context) to calibrate scoring.
    # Cached per team — only hits Notion once every 15 minutes.
    workspace_context = await _load_workspace_context(team_id) if team_id else ""
    score, reason, entities = await _score_crm_relevance(text, workspace_context)
    logger.info("Event score=%.2f channel=%s entities=%s", score, channel, entities)

    if score < EVENT_BORDERLINE_LOW:
        return

    bot_token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    if not bot_token:
        logger.warning("SLACK_BOT_TOKEN not set — cannot post event message")
        return

    if score >= EVENT_CONFIDENCE_THRESHOLD:
        await _post_event_confirmation(
            bot_token=bot_token,
            channel=channel,
            thread_ts=ts,
            message_text=text,
            reason=reason,
            entities=entities,
        )
    else:
        await _post_clarification_question(
            bot_token=bot_token,
            channel=channel,
            thread_ts=ts,
            message_text=text,
            reason=reason,
            entities=entities,
        )


async def _process_slack_event(event: Dict, team_id: str) -> Dict:
    """Dispatch a Slack event to the appropriate handler."""
    event_type = event.get("type", "")
    if event_type == "message":
        await _handle_message_event(event, team_id)
    else:
        logger.info("Unhandled Slack event type: %s", event_type)
    return _ok({"status": "processed"})


async def _handle_slack_event(args: Dict) -> Dict:
    """
    Entry point for all Slack Events API payloads.

    Handles:
      - url_verification: respond with challenge (no signature check needed)
      - event_callback:   verify signature, acknowledge immediately, process async
    """
    if args.get("type") == "url_verification":
        return _ok({"challenge": args.get("challenge", "")})

    if not _verify_slack_signature(args):
        logger.warning("Slack signature verification failed")
        return _error(401, "Invalid Slack signature")

    event = args.get("event") or {}
    team_id = args.get("team_id", "")

    # Background worker: do the actual processing
    if args.get("_event_worker"):
        return await _process_slack_event(event, team_id)

    # Acknowledge immediately (Slack requires <3 s response), spawn async worker
    async_url = os.environ.get("DO_SLACK_ASYNC_URL", "").strip()
    async_token = os.environ.get("DO_SLACK_ASYNC_TOKEN", "").strip()
    if async_url and async_token:
        base = _web_url_to_rest_url(async_url)
        invoke_url = f"{base}?blocking=false"
        payload = {**args, "_event_worker": 1}
        auth = (
            async_token
            if async_token.startswith("Bearer ") or async_token.startswith("Basic ")
            else f"Basic {async_token}"
        )

        def _do_invoke():
            try:
                with httpx.Client(timeout=5.0) as client:
                    client.post(
                        invoke_url,
                        json=payload,
                        headers={
                            "Content-Type": "application/json",
                            "Authorization": auth,
                        },
                    )
            except Exception as exc:
                logger.warning("Event async invoke failed: %s", exc)

        t = threading.Thread(target=_do_invoke, daemon=True)
        t.start()
        t.join(timeout=2.0)
    else:
        # No async config: run synchronously (may be slow, but works in dev)
        await _process_slack_event(event, team_id)

    return _ok({"status": "ok"})
