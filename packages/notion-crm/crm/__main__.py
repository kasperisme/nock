"""
Notion CRM — single Digital Ocean Function.

Routes by body parameters:
  prompt  → Agent mode (Claude function-calling agent)
  action  → API mode (databases or pages handlers)
  response_url → Slack slash command (Tier 2 extraction or #agent full loop)

Slash command: /nock <text>
  - Add #agent for full agent loop
  - Add #feedback to update Settings/Context with agent-adjusted feedback
  - Otherwise: 1× LLM extraction, 1× search, 1× create (~3–5s)
"""

import asyncio
import base64
import json
import logging
import os
import threading
from typing import Dict
from urllib.parse import parse_qs

import httpx

from config import _error, _ok
from notion_utils import _dispatch_api
from agent import _run_agent
from slack_slash import _run_slash_command, _web_url_to_rest_url
from slack_events import _handle_slack_event, _verify_slack_signature
from slack_interactions import _handle_slack_interaction, _run_interaction_worker
from notion_client import close_http_client

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def _run(coro):
    """Wrap a coroutine with HTTP client cleanup so the global AsyncClient is closed
    before the event loop exits, preventing 'Event loop is closed' GC errors."""
    try:
        return await coro
    finally:
        await close_http_client()


def _parse_args(args: Dict) -> Dict:
    """Parse request body from __ow_body (legacy) or http.body (Digital Ocean web: raw)."""
    body_str = None
    raw = args.get("__ow_body")
    if raw:
        try:
            body_str = base64.b64decode(raw).decode("utf-8")
        except Exception:
            body_str = raw if isinstance(raw, str) else raw.decode("utf-8")
    else:
        http = args.get("http") or {}
        body = http.get("body", "")
        if body:
            if http.get("isBase64Encoded"):
                try:
                    body_str = base64.b64decode(body).decode("utf-8")
                except Exception:
                    body_str = body
            else:
                body_str = body if isinstance(body, str) else ""
    if body_str is not None:
        try:
            try:
                parsed = json.loads(body_str)
                out = {**args, **parsed, "__ow_body_raw": body_str}
                if parsed.get("type") == "block_actions":
                    out["_interaction_payload"] = parsed
                return out
            except json.JSONDecodeError:
                pass
            parsed = parse_qs(body_str, keep_blank_values=True)
            form = {k: (v[0] if v else "") for k, v in parsed.items()}
            out = {**args, **form, "__ow_body_raw": body_str}
            if "payload" in form and form["payload"]:
                try:
                    out["_interaction_payload"] = json.loads(form["payload"])
                except json.JSONDecodeError:
                    pass
            return out
        except Exception:
            pass
    return args


def _check_auth(args: Dict) -> bool:
    secret = os.environ.get("API_SECRET")
    if not secret:
        return True
    headers = args.get("__ow_headers") or (args.get("http") or {}).get("headers") or {}
    auth = headers.get("authorization", "")
    return auth == f"Bearer {secret}"


def main(args: Dict) -> Dict:
    args = _parse_args(args)

    # Slack Interactivity (block_actions from Log it / Dismiss buttons)
    payload = args.get("_interaction_payload")
    if payload and payload.get("type") == "block_actions":
        if not _verify_slack_signature(args):
            return _error(401, "Invalid Slack signature")
        return asyncio.run(_run(_handle_slack_interaction(args)))

    # Slack Events API (url_verification + event_callback) — uses signature auth, not Bearer
    event_api_type = args.get("type")
    if event_api_type in ("url_verification", "event_callback"):
        return asyncio.run(_run(_handle_slack_event(args)))

    # Interaction worker (async invoke from Log it button — must complete in separate process)
    if args.get("_interaction_worker"):
        logger.info("Interaction worker invoked")
        async_token = os.environ.get("DO_SLACK_ASYNC_TOKEN", "").strip()
        worker_token = (args.get("_worker_token") or "").strip()
        if async_token and worker_token == async_token:
            return asyncio.run(_run(_run_interaction_worker(args)))
        logger.warning(
            "Interaction worker auth failed: token_present=%s async_token_set=%s",
            bool(worker_token),
            bool(async_token),
        )
        return _error(401, "Unauthorized")

    # Skip Bearer auth for Slack slash commands (Slack doesn't send Authorization)
    response_url = args.get("response_url") or ""
    is_slack = response_url.startswith("https://hooks.slack.com/")
    if not is_slack and not _check_auth(args):
        return _error(401, "Unauthorized")

    # Keep-alive ping
    if args.get("action") == "ping":
        return _ok({"status": "ok"})

    # Slack slash command: response_url present → Tier 2 or #agent
    if args.get("response_url"):
        # If this is the background worker (async self-invoke), run the work
        if args.get("_slash_worker"):
            return asyncio.run(_run(_run_slash_command(args)))
        # Else: respond immediately and invoke ourselves async to avoid Slack's 3s timeout
        async_url = os.environ.get("DO_SLACK_ASYNC_URL", "").strip()
        async_token = os.environ.get("DO_SLACK_ASYNC_TOKEN", "").strip()
        if async_url and async_token:
            base = _web_url_to_rest_url(async_url)
            invoke_url = f"{base}?blocking=false"
            payload = {**args, "_slash_worker": 1}
            auth = (
                async_token
                if async_token.startswith("Bearer ") or async_token.startswith("Basic ")
                else f"Basic {async_token}"
            )
            try:
                # Must wait for REST API - returns activation ID in <1s when blocking=false.
                # Fire in thread but join with 2.5s timeout so we respond to Slack in time.
                result = {"ok": False, "error": None}

                def _do_invoke():
                    try:
                        with httpx.Client(timeout=5.0) as client:
                            r = client.post(
                                invoke_url,
                                json=payload,
                                headers={
                                    "Content-Type": "application/json",
                                    "Authorization": auth,
                                },
                            )
                            result["ok"] = r.status_code in (200, 202)
                            if not result["ok"]:
                                result["error"] = (
                                    f"HTTP {r.status_code}: {r.text[:200]}"
                                )
                    except Exception as e:
                        result["error"] = str(e)

                t = threading.Thread(target=_do_invoke, daemon=True)
                t.start()
                t.join(timeout=2.5)
                if result["ok"]:
                    return _ok(
                        {
                            "response_type": "ephemeral",
                            "text": "Processing your CRM update… I'll post the result when done.",
                        }
                    )
                if result["error"]:
                    logger.warning("Async invoke failed: %s", result["error"])
            except Exception as e:
                logger.warning("Async invoke failed: %s", e)
        # No async config or invoke failed: run synchronously (may timeout)
        return asyncio.run(_run(_run_slash_command(args)))

    # Agent: prompt present → run agent
    if args.get("prompt"):
        return asyncio.run(_run(_run_agent({**args, "is_slack": False, "use_agent": True})))

    # API: action present → run databases/pages
    if args.get("action"):
        return asyncio.run(_run(_dispatch_api(args)))

    return _error(400, "Missing required parameter: prompt or action")
