import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

import httpx
from anthropic import AsyncAnthropic

from crm_logger import (
    get_and_clear_pending_reply,
    log_agent_run,
    log_help_request,
    log_system_prompt_request,
    save_agent_conversation,
    save_pending_reply,
)

from config import (
    MODEL_AGENT,
    SLACK_HELP,
    _error,
    _ok,
)
from notion_utils import (
    _get_agent_system_prompt,
    _get_notion_client_and_settings,
    _run_refresh_settings,
)
from agent import _run_agent
from extraction import _run_slash_tier2

logger = logging.getLogger(__name__)


async def _post_to_slack(
    response_url: str, text: str, blocks: Optional[List[Dict]] = None
) -> None:
    """POST message to Slack response_url (slash command delayed response)."""
    payload: Dict[str, Any] = {"text": text}
    if blocks:
        payload["blocks"] = blocks
    async with httpx.AsyncClient(timeout=10.0) as client:
        await client.post(response_url, json=payload)


async def _post_slash_result(
    args: Dict, text: str, blocks: Optional[List[Dict]] = None
) -> None:
    """Post to response_url, chat.update (_interaction_update), or chat.postMessage (_reply_channel)."""
    upd = args.get("_interaction_update")
    if upd:
        channel = upd.get("channel", "")
        message_ts = upd.get("message_ts", "")
        thread_ts = upd.get("thread_ts")
        token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
        if channel and message_ts and token:
            async with httpx.AsyncClient(timeout=10.0) as client:
                headers = {
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                }
                r = await client.post(
                    "https://slack.com/api/chat.update",
                    headers=headers,
                    json={"channel": channel, "ts": message_ts, "text": text},
                )
                resp = r.json() if r.status_code == 200 else {}
                if not resp.get("ok"):
                    logger.warning("chat.update failed: %s", r.text[:300])
                    # Fallback: post as new message in thread
                    post_payload: Dict[str, Any] = {"channel": channel, "text": text}
                    if thread_ts:
                        post_payload["thread_ts"] = thread_ts
                    r2 = await client.post(
                        "https://slack.com/api/chat.postMessage",
                        headers=headers,
                        json=post_payload,
                    )
                    if r2.status_code != 200 or not r2.json().get("ok"):
                        logger.warning(
                            "chat.postMessage fallback failed: %s", r2.text[:300]
                        )
        return

    # Event-reply path: post via chat.postMessage in the original channel/thread
    reply_channel = args.get("_reply_channel")
    if reply_channel:
        token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
        if token:
            post_payload = {"channel": reply_channel, "text": text}
            thread_ts = args.get("_reply_thread_ts")
            if thread_ts:
                post_payload["thread_ts"] = thread_ts
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.post(
                    "https://slack.com/api/chat.postMessage",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                    json=post_payload,
                )
                if r.status_code != 200 or not r.json().get("ok"):
                    logger.warning("chat.postMessage (reply) failed: %s", r.text[:300])
        return

    await _post_to_slack(args.get("response_url", ""), text, blocks)


def _format_slack_success(result: Dict) -> str:
    """Format single or multiple CRM update results for Slack."""
    results = result.get("results")
    if results:
        lines = [
            f"• {r.get('title', 'Created')} ({r.get('database', '')})\n<{r.get('notion_url', '')}|Open in Notion>"
            for r in results
        ]
        msg = (
            f"*CRM updated* ({len(results)} item{'s' if len(results) > 1 else ''})\n"
            + "\n".join(lines)
        )
        errors = result.get("errors") or []
        if errors:
            msg += "\n\n:warning: Partial: " + "; ".join(errors)
        return msg
    url = result.get("notion_url", "")
    title = result.get("title", "Created")
    db = result.get("database", "")
    return f"*CRM updated*\n• {title} ({db})\n<{url}|Open in Notion>"


def _format_slack_error(err: str) -> str:
    return f":x: *CRM error*: {err}"


def _web_url_to_rest_url(web_url: str) -> str:
    """Convert DO Functions web URL to REST API URL for blocking=false invoke."""
    base = web_url.split("?")[0].rstrip("/")
    if "/api/v1/web/" not in base:
        return base
    base = base.replace("/api/v1/web/", "/api/v1/namespaces/", 1)
    base = re.sub(r"(/namespaces/[^/]+)/", r"\1/actions/", base, count=1)
    return base


async def _run_slash_command(args: Dict) -> Dict:
    """Handle Slack slash command: Tier 2 (fast), #agent (full loop), or #feedback (update context)."""
    text = (args.get("text") or args.get("prompt") or "").strip()
    response_url = args.get("response_url", "").strip()
    use_feedback = "#feedback" in text.lower()
    use_agent = "#agent" in text.lower()
    is_slack = (
        args.get("response_url", "").startswith("https://hooks.slack.com/")
        or bool(args.get("_reply_channel"))
    )

    if not response_url and not args.get("_interaction_update") and not args.get("_reply_channel"):
        return _error(400, "Missing response_url (required for slash commands)")

    if not text:
        return _error(
            400,
            "Missing text. Usage: /nock <your note>, /nock <note> #agent, or /nock <feedback> #feedback",
        )

    # Ensure agent_conversation_sessions has a row for this user on every slash command
    slack_team_id = args.get("team_id") or (args.get("slack_context") or {}).get(
        "team_id"
    )
    slack_user_id = args.get("user_id") or (args.get("slack_context") or {}).get(
        "user_id"
    )
    if slack_team_id and slack_user_id:
        sid = save_agent_conversation(
            slack_team_id=slack_team_id,
            slack_user_id=slack_user_id,
        )
        if not sid:
            logger.warning(
                "save_agent_conversation failed for team=%s user=%s (check SUPABASE_* env)",
                slack_team_id,
                slack_user_id,
            )
    else:
        logger.warning(
            "Skipping agent_conversation_sessions: missing team_id or user_id (team=%s user=%s)",
            bool(slack_team_id),
            bool(slack_user_id),
        )

    if text.lower() in ("help", "h", "?", "-h", "--help"):
        log_help_request(
            slack_context={
                "team_id": args.get("team_id"),
                "team_domain": args.get("team_domain"),
                "user_id": args.get("user_id"),
                "channel_id": args.get("channel_id"),
            },
            slack_team_id=args.get("team_id"),
        )
        try:
            await _post_slash_result(args, SLACK_HELP)
        except Exception as e:
            logger.warning("Failed to post help to Slack: %s", e)
        return _ok({"status": "help"})

    if text.lower() in ("refresh settings", "refresh"):
        try:
            notion, settings_path = _get_notion_client_and_settings(args)
        except ValueError as exc:
            await _post_slash_result(args, _format_slack_error(str(exc)))
            return _ok({"status": "error"})
        try:
            msg = await _run_refresh_settings(notion, settings_path)
            log_agent_run(
                prompt="refresh settings",
                model=MODEL_AGENT,
                response=msg,
                iterations=0,
                tool_calls=[],
                success=True,
                slack_context={
                    "team_id": args.get("team_id"),
                    "team_domain": args.get("team_domain"),
                    "user_id": args.get("user_id"),
                    "channel_id": args.get("channel_id"),
                },
                slack_team_id=args.get("team_id"),
            )
            await _post_slash_result(args, f":white_check_mark: *{msg}*")
        except Exception as e:
            logger.warning("Refresh settings failed: %s", e)
            await _post_slash_result(args, _format_slack_error(str(e)))
        return _ok({"status": "refresh_settings"})

    if text.lower() in (
        "system prompt",
        "prompt",
        "get prompt",
        "show prompt",
        "show system prompt",
    ):
        log_system_prompt_request(
            slack_context={
                "team_id": args.get("team_id"),
                "team_domain": args.get("team_domain"),
                "user_id": args.get("user_id"),
                "channel_id": args.get("channel_id"),
            },
            slack_team_id=args.get("team_id"),
        )
        try:
            notion, settings_path = _get_notion_client_and_settings(args)
        except ValueError as exc:
            await _post_slash_result(args, _format_slack_error(str(exc)))
            return _ok({"status": "error"})
        try:
            prompt_content = await _get_agent_system_prompt(notion, settings_path)
            # Slack message limit ~40k; truncate with note if very long
            if len(prompt_content) > 3500:
                prompt_content = prompt_content[:3500] + "\n\n... _(truncated)_"
            await _post_slash_result(
                args,
                f"*Agent system prompt*\n\n```\n{prompt_content}\n```",
            )
        except Exception as e:
            logger.warning("Failed to fetch system prompt: %s", e)
            await _post_slash_result(
                args, _format_slack_error(f"Could not fetch prompt: {e}")
            )
        return _ok({"status": "system_prompt"})

    if use_agent:
        text = re.sub(r"#agent\b", "", text, flags=re.IGNORECASE).strip()
    if use_feedback:
        text = re.sub(r"#feedback\b", "", text, flags=re.IGNORECASE).strip()

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        await _post_slash_result(
            args, _format_slack_error("ANTHROPIC_API_KEY is not set")
        )
        return _ok({"status": "error", "message": "ANTHROPIC_API_KEY not set"})

    try:
        notion, settings_path = _get_notion_client_and_settings(args)
    except ValueError as exc:
        await _post_slash_result(args, _format_slack_error(str(exc)))
        return _error(500, str(exc))

    anthropic_client = AsyncAnthropic(api_key=api_key)

    # Agent path: full (#agent/#feedback) or light (simple slash with limited iterations)
    agent_args = {
        **args,
        "prompt": text,
        "is_slack": is_slack,
        "use_agent": use_agent and not use_feedback,
        "use_feedback": use_feedback,
        "max_iterations": (
            8 if not (use_agent or use_feedback) else None
        ),  # Light agent for simple slash
        "slack_context": {
            "team_id": args.get("team_id"),
            "team_domain": args.get("team_domain"),
            "user_id": args.get("user_id"),
            "user_name": args.get("user_name"),
            "channel_id": args.get("channel_id"),
        },
    }
    result = await _run_agent(agent_args)
    body = json.loads(result.get("body", "{}"))
    if result.get("statusCode", 0) == 200:
        resp_text = body.get("response", "Done.")
        asked_question = body.get("ask_user", False)
        ask_user_tool_use_id = body.get("ask_user_tool_use_id", "")
        if use_feedback:
            prefix = "*Context updated*"
        elif use_agent or asked_question:
            prefix = "*CRM agent*"
        else:
            prefix = "*CRM*"
        if args.get("_interaction_update") and not asked_question:
            prefix = f"{prefix}\n:white_check_mark: Completed."
        await _post_slash_result(args, f"{prefix}\n{resp_text}")

        # If agent asked a question, save pending_reply so the Events handler can route the reply
        if asked_question and slack_team_id and slack_user_id:
            channel_id = args.get("channel_id") or args.get("_reply_channel", "")
            if channel_id:
                save_pending_reply(
                    slack_team_id=slack_team_id,
                    slack_user_id=slack_user_id,
                    channel_id=channel_id,
                    tool_use_id=ask_user_tool_use_id,
                )
                logger.info(
                    "Pending reply saved for team=%s user=%s channel=%s",
                    slack_team_id, slack_user_id, channel_id,
                )
    else:
        await _post_slash_result(
            args, _format_slack_error(body.get("error", "Unknown error"))
        )
    return result
