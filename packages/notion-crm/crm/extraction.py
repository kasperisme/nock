import json
import logging
import re
from typing import Any, Dict, List, Optional

import httpx
from anthropic import AsyncAnthropic

from notion_client import NotionClient

from config import (
    MODEL_EXTRACTION,
    SLACK_EXTRACTION_PROMPT,
    STATUS_OPTION_ALIASES,
)
from notion_utils import (
    _find_database_by_title,
    _get_database_context,
    _normalize_title_for_dedup,
    _resolve_select_status_value,
)

logger = logging.getLogger(__name__)


def _normalize_extraction(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize extraction to {actions: [...]}. Handles legacy single-object format."""
    if "error" in raw:
        return raw
    if "actions" in raw and isinstance(raw["actions"], list):
        return raw
    # Legacy single-object format
    if raw.get("target_database"):
        return {"actions": [raw]}
    return {"actions": [], "error": "No actions extracted"}


async def _run_extraction(
    text: str,
    anthropic_client: AsyncAnthropic,
    database_context: Optional[str] = None,
) -> Dict[str, Any]:
    """One LLM call to extract structured CRM actions from free-form text."""
    system_content = SLACK_EXTRACTION_PROMPT
    if database_context and database_context.strip():
        system_content = (
            f"{database_context.strip()}\n\n---\n\n{SLACK_EXTRACTION_PROMPT}"
        )

    response = await anthropic_client.messages.create(
        model=MODEL_EXTRACTION,
        max_tokens=4096,
        system=system_content,
        messages=[{"role": "user", "content": text}],
    )
    content = ""
    for block in response.content or []:
        if hasattr(block, "text"):
            content += block.text
        elif isinstance(block, dict) and block.get("type") == "text":
            content += block.get("text", "")
    content = content.strip()
    # Strip markdown code blocks if present
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*", "", content)
        content = re.sub(r"\s*```$", "", content)
    raw = json.loads(content)
    return _normalize_extraction(raw)


def _action_key(action: Dict[str, Any]) -> tuple:
    """Unique key for deduplication: (target_db, normalized_title)."""
    db = (action.get("target_database") or "").strip().lower()
    title = _normalize_title_for_dedup(str(action.get("title") or ""))
    return (db, title)


async def _execute_extraction(
    extraction: Dict[str, Any],
    notion: NotionClient,
) -> Dict[str, Any]:
    """Execute extracted plan: search existing first, then update or create."""
    databases = await notion.list_databases()

    target_db_title = extraction.get("target_database") or "Customers"
    target_db = _find_database_by_title(databases, target_db_title, notion)
    if not target_db:
        return {"error": f"Database not found: {target_db_title}"}

    target_id = target_db["id"]
    properties = dict(extraction.get("properties") or {})

    # Resolve relation: search for existing record and link
    search_in = extraction.get("search_in")
    search_query = extraction.get("search_query")
    link_property = extraction.get("link_property")

    if search_in and search_query and link_property:
        search_db = _find_database_by_title(databases, search_in, notion)
        if search_db:
            pages = await notion.query_database(
                search_db["id"],
                limit=5,
                title_search=search_query,
            )
            if pages:
                page_id = pages[0]["id"]
                properties[link_property] = [page_id]

    title = extraction.get("title") or "Untitled"
    db_schema = (await notion.get_database(target_id)).get("properties", {})
    formatted: Dict[str, Any] = {}
    for prop_name, value in properties.items():
        prop_def = db_schema.get(prop_name)
        if prop_def is None:
            continue
        prop_type = prop_def.get("type")
        if prop_type in ("select", "status"):
            resolved = _resolve_select_status_value(
                prop_def, value, STATUS_OPTION_ALIASES
            )
            if resolved is None:
                continue
            value = resolved
        formatted[prop_name] = notion.build_property_value(prop_type, value)

    # Search-before-create: if a record with this title exists, update instead
    existing = await notion.query_database(
        target_id, limit=1, title_search=title.strip()
    )
    try:
        if existing and existing[0]:
            page_id = existing[0]["id"]
            page_title = notion._get_page_title(existing[0])
            if _normalize_title_for_dedup(page_title) == _normalize_title_for_dedup(
                title
            ):
                await notion.update_page(page_id, formatted)
                return {
                    "success": True,
                    "page_id": page_id,
                    "title": title,
                    "database": target_db_title,
                    "updated": True,
                    "notion_url": f"https://www.notion.so/{page_id.replace('-', '')}",
                }
        page = await notion.create_page(
            target_id, title, formatted if formatted else None
        )
    except httpx.HTTPStatusError as exc:
        return {"error": _notion_error_msg(exc)}
    return {
        "success": True,
        "page_id": page["id"],
        "title": title,
        "database": target_db_title,
        "notion_url": f"https://www.notion.so/{page['id'].replace('-', '')}",
    }


def _notion_error_msg(exc: httpx.HTTPStatusError) -> str:
    try:
        return exc.response.json().get("message", str(exc))
    except Exception:
        return str(exc)


async def _run_slash_tier2(
    text: str,
    notion: NotionClient,
    anthropic_client: AsyncAnthropic,
    settings_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Tier 2: 1 LLM extraction → N actions executed → results posted to Slack."""
    database_context = await _get_database_context(notion, settings_path)
    extraction = await _run_extraction(text, anthropic_client, database_context)
    if "error" in extraction:
        return extraction
    actions = extraction.get("actions") or []
    if not actions:
        return {"error": "No actions extracted from text"}

    # Deduplicate: merge actions with same (target_database, title), keep last to preserve links
    seen: Dict[tuple, Dict[str, Any]] = {}
    for a in actions:
        k = _action_key(a)
        merged = seen.get(k)
        if merged:
            props = dict(merged.get("properties") or {})
            props.update(a.get("properties") or {})
            merged["properties"] = props
            if a.get("search_in") and a.get("link_property"):
                merged["search_in"] = a.get("search_in")
                merged["search_query"] = a.get("search_query")
                merged["link_property"] = a.get("link_property")
        else:
            seen[k] = dict(a)
    actions = list(seen.values())

    results: List[Dict[str, Any]] = []
    errors: List[str] = []
    for i, action in enumerate(actions):
        try:
            out = await _execute_extraction(action, notion)
            if out.get("success"):
                results.append(out)
            else:
                errors.append(out.get("error", f"Action {i + 1} failed"))
        except Exception as e:
            errors.append(f"Action {i + 1}: {e}")

    if not results and errors:
        return {"error": "; ".join(errors)}
    return {
        "success": True,
        "results": results,
        "errors": errors,
        "count": len(results),
    }
