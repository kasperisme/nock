"""
Helpers to write Notion CRM audit logs into Supabase (public schema).
All writes are fire-and-forget — failures are logged but never raised to callers.

Uses httpx directly for Supabase REST API to avoid heavy supabase SDK (build memory).
"""

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

SCHEMA = "public"


def _supabase_headers(key: str) -> Dict[str, str]:
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Content-Profile": SCHEMA,
        "Accept-Profile": SCHEMA,
        "Prefer": "return=representation",
    }


def _post_rows(
    base_url: str, key: str, table: str, payload: Dict[str, Any] | List[Dict[str, Any]]
) -> Optional[List[Dict]]:
    """POST to Supabase REST API, return inserted row(s) or None."""
    url = f"{base_url.rstrip('/')}/rest/v1/{table}"
    try:
        with httpx.Client(timeout=10.0) as client:
            r = client.post(url, json=payload, headers=_supabase_headers(key))
            r.raise_for_status()
            return r.json() if r.content else []
    except Exception as exc:
        logger.warning("Supabase insert failed (%s): %s", table, exc)
        return None


def get_notion_connection(
    *,
    team_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Resolve Notion connection (access_token, settings_path) from notion_connections (Supabase).
    - team_id: look up slack_connections (with notion_connection_id pair) -> notion_connections.
    - user_id: API mode — look up notion_connections by Supabase auth UUID.
    Returns {"access_token": str, "settings_path": str | None} or None if not found.
    """
    base_url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_KEY")
    if not base_url or not key:
        return None

    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
        "Content-Profile": "public",
        "Accept-Profile": "public",
    }

    if team_id:
        try:
            slack_url = f"{base_url.rstrip('/')}/rest/v1/slack_connections"
            slack_params = {
                "slack_team_id": f"eq.{team_id}",
                "select": "notion_connection_id",
                "limit": "1",
            }
            with httpx.Client(timeout=5.0) as client:
                r = client.get(slack_url, params=slack_params, headers=headers)
                r.raise_for_status()
                slack_data = r.json()
                if not slack_data or len(slack_data) == 0:
                    return None
                notion_conn_id = slack_data[0].get("notion_connection_id")
                if not notion_conn_id:
                    return None
                notion_url = f"{base_url.rstrip('/')}/rest/v1/notion_connections"
                notion_params = {
                    "id": f"eq.{notion_conn_id}",
                    "select": "access_token,settings_path",
                    "limit": "1",
                }
                r2 = client.get(notion_url, params=notion_params, headers=headers)
                r2.raise_for_status()
                notion_data = r2.json()
                if notion_data and len(notion_data) > 0:
                    row = notion_data[0]
                    return {
                        "access_token": str(row["access_token"]),
                        "settings_path": row.get("settings_path"),
                    }
        except Exception as exc:
            logger.debug("Integration pair lookup for team_id=%s: %s", team_id, exc)
        return None

    if user_id:
        try:
            url = f"{base_url.rstrip('/')}/rest/v1/notion_connections"
            params = {
                "user_id": f"eq.{user_id}",
                "select": "access_token,settings_path",
                "order": "updated_at.desc",
                "limit": "1",
            }
            with httpx.Client(timeout=5.0) as client:
                r = client.get(url, params=params, headers=headers)
                r.raise_for_status()
                data = r.json()
                if data and len(data) > 0:
                    row = data[0]
                    return {
                        "access_token": str(row["access_token"]),
                        "settings_path": row.get("settings_path"),
                    }
        except Exception as exc:
            logger.debug("Notion connection lookup for user_id=%s: %s", user_id, exc)
        return None

    return None


def get_notion_access_token(
    *,
    team_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> Optional[str]:
    """
    Resolve Notion access_token from notion_connections (Supabase).
    - team_id: look up slack_connections for user_id, then notion_connections for that user.
      When team_id is present, always resolve via slack (ignore user_id from Slack payload).
    - user_id: use only when no team_id (API mode) — must be Supabase auth user UUID.
    Returns None if not found; no env fallback.
    """
    conn = get_notion_connection(team_id=team_id, user_id=user_id)
    return conn["access_token"] if conn else None


def _lookup_slack_connection_id(base_url: str, key: str, team_id: str) -> Optional[str]:
    """Resolve slack_team_id to slack_connections.id. Returns uuid string or None."""
    if not team_id or not base_url or not key:
        return None
    url = f"{base_url.rstrip('/')}/rest/v1/slack_connections"
    params = {"slack_team_id": f"eq.{team_id}", "select": "id", "limit": "1"}
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
        "Content-Profile": "public",
        "Accept-Profile": "public",
    }
    try:
        with httpx.Client(timeout=5.0) as client:
            r = client.get(url, params=params, headers=headers)
            r.raise_for_status()
            data = r.json()
            if data and len(data) > 0:
                return str(data[0]["id"])
    except Exception as exc:
        logger.debug("Slack connection lookup failed for team_id=%s: %s", team_id, exc)
    return None


def log_agent_run(
    *,
    prompt: str,
    model: str,
    response: str,
    iterations: int,
    tool_calls: List[Dict[str, Any]],
    success: bool = True,
    error: Optional[str] = None,
    slack_context: Optional[Dict] = None,
    slack_team_id: Optional[str] = None,
) -> Optional[str]:
    """
    Insert a row into notion_crm.agent_runs and child rows into
    notion_crm.agent_tool_calls. Returns the new agent_run id, or None on failure.
    When slack_team_id is provided (from slash command payload), looks up slack_connection_id
    and links the run to the Slack workspace.
    """
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_KEY")
    if not url or not key:
        return None

    run_row = {
        "prompt": prompt,
        "model": model,
        "response": response,
        "iterations": iterations,
        "tool_call_count": len(tool_calls),
        "success": success,
        "error": error,
        "slack_context": slack_context,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    if slack_team_id:
        slack_connection_id = _lookup_slack_connection_id(url, key, slack_team_id)
        if slack_connection_id:
            run_row["slack_connection_id"] = slack_connection_id
    data = _post_rows(url, key, "agent_runs", run_row)
    if not data:
        return None

    run_id: str = data[0]["id"]

    if tool_calls:
        call_rows = [
            {
                "agent_run_id": run_id,
                "tool_name": tc["tool"],
                "args": tc["args"],
                "result": {"summary": tc.get("result_summary", "")},
                "success": not str(tc.get("result_summary", "")).startswith('{"error"'),
            }
            for tc in tool_calls
        ]
        _post_rows(url, key, "agent_tool_calls", call_rows)

    return run_id


def log_help_request(
    *,
    slack_context: Optional[Dict] = None,
    slack_team_id: Optional[str] = None,
) -> None:
    """Log a slash command help request to agent_runs (minimal row)."""
    log_agent_run(
        prompt="help",
        model="slash-help",
        response="(help requested)",
        iterations=0,
        tool_calls=[],
        success=True,
        slack_context=slack_context,
        slack_team_id=slack_team_id,
    )


def log_system_prompt_request(
    *,
    slack_context: Optional[Dict] = None,
    slack_team_id: Optional[str] = None,
) -> None:
    """Log a slash command system-prompt request to agent_runs (minimal row)."""
    log_agent_run(
        prompt="system_prompt",
        model="slash-help",
        response="(system prompt requested)",
        iterations=0,
        tool_calls=[],
        success=True,
        slack_context=slack_context,
        slack_team_id=slack_team_id,
    )


def save_pending_reply(
    *,
    slack_team_id: str,
    slack_user_id: str,
    channel_id: str,
    tool_use_id: str = "",
) -> None:
    """
    Store the Slack channel (and ask_user tool_use_id) where the agent is waiting for a reply.
    Used when the agent asks a question via slash command; the Events handler
    checks this to route the user's next message back to the agent.
    tool_use_id is stored in openai_response_id (repurposed; unused for Anthropic).
    """
    base_url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_KEY")
    if not base_url or not key or not slack_team_id or not slack_user_id or not channel_id:
        return
    headers = _supabase_headers(key)
    headers["Prefer"] = "return=representation"
    try:
        with httpx.Client(timeout=5.0) as client:
            get_url = f"{base_url.rstrip('/')}/rest/v1/agent_conversation_sessions"
            get_params = {
                "slack_team_id": f"eq.{slack_team_id}",
                "slack_user_id": f"eq.{slack_user_id}",
                "select": "id",
                "limit": "1",
            }
            r_get = client.get(get_url, params=get_params, headers=headers)
            r_get.raise_for_status()
            existing = r_get.json() if r_get.content else []
            payload: Dict[str, Any] = {
                "pending_reply_channel_id": channel_id,
                "openai_response_id": tool_use_id,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            if existing:
                patch_url = f"{get_url}?id=eq.{existing[0]['id']}"
                client.patch(patch_url, json=payload, headers=headers)
            else:
                payload["slack_team_id"] = slack_team_id
                payload["slack_user_id"] = slack_user_id
                client.post(get_url, json=payload, headers=headers)
    except Exception as exc:
        logger.debug("Save pending reply failed: %s", exc)


def get_and_clear_pending_reply(
    *,
    slack_team_id: str,
    slack_user_id: str,
) -> Optional[tuple]:
    """
    Fetch and clear the pending reply state for (team, user).
    Returns (channel_id, tool_use_id) if a pending reply exists, else None.
    tool_use_id is the Anthropic tool_use id needed to inject the reply as a tool_result.
    """
    base_url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_KEY")
    if not base_url or not key or not slack_team_id or not slack_user_id:
        return None
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
        "Content-Profile": "public",
        "Accept-Profile": "public",
    }
    try:
        with httpx.Client(timeout=5.0) as client:
            url = f"{base_url.rstrip('/')}/rest/v1/agent_conversation_sessions"
            params = {
                "slack_team_id": f"eq.{slack_team_id}",
                "slack_user_id": f"eq.{slack_user_id}",
                "select": "id,pending_reply_channel_id,openai_response_id",
                "limit": "1",
            }
            r = client.get(url, params=params, headers=headers)
            r.raise_for_status()
            data = r.json()
            if not data or len(data) == 0:
                return None
            row = data[0]
            channel_id = row.get("pending_reply_channel_id")
            if not channel_id:
                return None
            tool_use_id = row.get("openai_response_id") or ""
            # Clear both fields
            patch_headers = {**headers, "Content-Type": "application/json"}
            patch_url = f"{url}?id=eq.{row['id']}"
            client.patch(
                patch_url,
                json={
                    "pending_reply_channel_id": None,
                    "openai_response_id": None,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
                headers=patch_headers,
            )
            return (str(channel_id), str(tool_use_id))
    except Exception as exc:
        logger.debug("Get and clear pending reply failed: %s", exc)
    return None


def get_agent_conversation(
    *,
    slack_team_id: Optional[str] = None,
    slack_user_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Fetch the agent conversation state for a (team, user) pair.
    Returns {id, message_history, updated_at} or None. id is the conversation_id.
    """
    base_url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_KEY")
    if not base_url or not key or not slack_team_id or not slack_user_id:
        return None
    url = f"{base_url.rstrip('/')}/rest/v1/agent_conversation_sessions"
    params = {
        "slack_team_id": f"eq.{slack_team_id}",
        "slack_user_id": f"eq.{slack_user_id}",
        "select": "id,message_history,openai_response_id,openai_conversation_id,updated_at",
        "limit": "1",
    }
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
        "Content-Profile": "public",
        "Accept-Profile": "public",
    }
    try:
        with httpx.Client(timeout=5.0) as client:
            r = client.get(url, params=params, headers=headers)
            r.raise_for_status()
            data = r.json()
            if data and len(data) > 0:
                return data[0]
    except Exception as exc:
        logger.debug("Get agent conversation failed: %s", exc)
    return None


def save_agent_conversation(
    *,
    message_history: Optional[List[Dict[str, Any]]] = None,
    openai_response_id: Optional[str] = None,
    openai_conversation_id: Optional[str] = None,
    slack_team_id: Optional[str] = None,
    slack_user_id: Optional[str] = None,
) -> Optional[str]:
    """
    Upsert agent conversation state for (team, user) pair.
    Uses openai_conversation_id (Conversations API) or openai_response_id for chaining.
    Returns conversation id (row id) or None.
    """
    base_url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_KEY")
    if not base_url or not key or not slack_team_id or not slack_user_id:
        return None
    payload: Dict[str, Any] = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if openai_response_id is not None:
        payload["openai_response_id"] = openai_response_id
    if openai_conversation_id is not None:
        payload["openai_conversation_id"] = openai_conversation_id
    if message_history is not None:
        trimmed = message_history[-40:] if len(message_history) > 40 else message_history
        payload["message_history"] = trimmed
    headers = _supabase_headers(key)
    headers["Prefer"] = "return=representation"
    try:
        with httpx.Client(timeout=10.0) as client:
            get_url = f"{base_url.rstrip('/')}/rest/v1/agent_conversation_sessions"
            get_params = {
                "slack_team_id": f"eq.{slack_team_id}",
                "slack_user_id": f"eq.{slack_user_id}",
                "select": "id",
                "limit": "1",
            }
            r_get = client.get(get_url, params=get_params, headers=headers)
            r_get.raise_for_status()
            existing = r_get.json() if r_get.content else []
            if existing:
                patch_url = f"{get_url}?id=eq.{existing[0]['id']}"
                r = client.patch(patch_url, json=payload, headers=headers)
                r.raise_for_status()
                data = r.json() if r.content else []
                if data:
                    return str(data[0]["id"])
            else:
                payload["slack_team_id"] = slack_team_id
                payload["slack_user_id"] = slack_user_id
                r = client.post(get_url, json=payload, headers=headers)
                r.raise_for_status()
                data = r.json() if r.content else []
                if data:
                    return str(data[0]["id"])
    except Exception as exc:
        logger.warning("Save agent conversation failed: %s", exc)
    return None


def log_page_operation(
    *,
    operation: str,
    success: bool = True,
    error: Optional[str] = None,
    page_id: Optional[str] = None,
    database_id: Optional[str] = None,
    database_name: Optional[str] = None,
    property_key: Optional[str] = None,
    properties: Optional[Dict] = None,
) -> None:
    """Insert a row into notion_crm.page_operations. Failures are silently swallowed."""
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_KEY")
    if not url or not key:
        return

    row = {
        "operation": operation,
        "success": success,
        "error": error,
        "page_id": page_id,
        "database_id": database_id,
        "database_name": database_name,
        "property_key": property_key,
        "properties": properties,
    }
    _post_rows(url, key, "page_operations", row)
