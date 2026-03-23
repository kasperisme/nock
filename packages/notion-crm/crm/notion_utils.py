import json
import logging
import time
from typing import Any, Dict, List, Optional

import httpx

from notion_client import NotionClient
from crm_logger import (
    log_page_operation,
)

from config import (
    AGENT_PROMPT_CACHE_TTL,
    GENERIC_SYSTEM_PROMPT,
    REGISTRY_DB_SCHEMA,
    STATUS_OPTION_ALIASES,
    _error,
    _ok,
    _load_local_skills,
)

logger = logging.getLogger(__name__)


def _property_key(notion_name: str) -> str:
    """Normalize Notion property name to property_key: 'Last Contact' -> 'property_last_contact'."""
    normalized = "".join(c if c.isalnum() else "_" for c in notion_name.strip()).strip(
        "_"
    )
    return f"property_{normalized.lower()}" if normalized else ""


def _resolve_settings_path(root: Optional[str] = None) -> str:
    """Build full path from root. Root must be provided in notion_connections.settings_path."""
    if not root or not root.strip():
        raise ValueError(
            "Settings root not configured. Set it in Integrations for this Notion workspace."
        )
    base = root.strip().rstrip("/")
    return f"{base}/Settings/Context"


def _resolve_database_settings_path(root: Optional[str] = None) -> str:
    """Build full path for database schema page: [root]/Settings/Database."""
    if not root or not root.strip():
        raise ValueError(
            "Settings root not configured. Set it in Integrations for this Notion workspace."
        )
    base = root.strip().rstrip("/")
    return f"{base}/Settings/Database"


_database_schema_cache: Optional[tuple] = None  # (schema_dict, expiry_time)


def _extract_property_meta(prop_name: str, prop_def: Dict[str, Any]) -> Dict[str, Any]:
    """
    Extract normalized metadata from a Notion property definition.
    Returns {name, type, options?, format?} for storage in the registry.
    """
    meta: Dict[str, Any] = {
        "name": prop_name,
        "type": prop_def.get("type", "rich_text"),
    }
    ptype = meta["type"]
    if ptype == "select":
        opts = (prop_def.get("select") or {}).get("options") or []
        meta["options"] = [o.get("name") for o in opts if o.get("name")]
    elif ptype == "multi_select":
        opts = (prop_def.get("multi_select") or {}).get("options") or []
        meta["options"] = [o.get("name") for o in opts if o.get("name")]
    elif ptype == "status":
        status_def = prop_def.get("status") or {}
        opts = status_def.get("options") or []
        meta["options"] = [o.get("name") for o in opts if o.get("name")]
        groups = status_def.get("groups") or []
        if groups:
            meta["groups"] = [g.get("name") for g in groups if g.get("name")]
    elif ptype == "number":
        num_def = prop_def.get("number") or {}
        fmt = num_def.get("format")
        if fmt:
            meta["format"] = fmt
    return meta


def _extract_rich_text_from_prop(prop_val: Any) -> str:
    """Extract plain text from a Notion property (rich_text or title)."""
    if prop_val is None:
        return ""
    rt = prop_val.get("rich_text") or prop_val.get("title") or []
    if not isinstance(rt, list):
        return str(prop_val)
    return "".join(
        t.get("plain_text", t.get("text", {}).get("content", "")) for t in rt
    )


async def _load_database_schema(
    notion: NotionClient, settings_path: Optional[str] = None
) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """
    Load database schema from Notion registry at [root]/Settings/database.
    Returns {db_name: {property_key: {name, type, options?}}}.
    """
    global _database_schema_cache
    now = time.time()
    if _database_schema_cache and _database_schema_cache[1] > now:
        return _database_schema_cache[0]

    path = _resolve_database_settings_path(settings_path)
    registry_db_id = await notion.find_page_by_path(path)
    if not registry_db_id:
        raise ValueError(
            f"Database schema not found at {path}. Run '/nock refresh settings' first."
        )

    try:
        reg_db = await notion.get_database(registry_db_id)
    except Exception:
        raise ValueError(
            f"Could not load registry at {path}. Run '/nock refresh settings' first."
        )

    props_def = reg_db.get("properties") or {}
    title_prop = next(
        (n for n, s in props_def.items() if s.get("type") == "title"),
        "Name",
    )
    props_col = "Properties" if "Properties" in props_def else None
    if not props_col:
        raise ValueError(
            f"Registry at {path} missing Properties column. Run '/nock refresh settings'."
        )

    rows = await notion.query_database(registry_db_id, limit=500)
    schema: Dict[str, Dict[str, str]] = {}
    for row in rows:
        props_data = row.get("properties") or {}
        name_val = props_data.get(title_prop) or props_data.get("Name") or {}
        db_name = (
            _extract_rich_text_from_prop(name_val)
            .strip()
            .lower()
            .replace(" ", "_")
            .replace("/", "_")
        )
        if not db_name:
            continue
        json_str = _extract_rich_text_from_prop(props_data.get(props_col) or {})
        if not json_str.strip():
            continue
        try:
            properties = json.loads(json_str)
            if isinstance(properties, dict):
                # Normalize: support both rich format {key: {name, type, options?}} and legacy {key: "Name"}
                normalized: Dict[str, Dict[str, Any]] = {}
                for k, v in properties.items():
                    if isinstance(v, dict) and v.get("name"):
                        normalized[k] = v
                    elif isinstance(v, str):
                        normalized[k] = {"name": v, "type": "rich_text"}
                    else:
                        continue
                schema[db_name] = normalized
        except json.JSONDecodeError:
            continue

    _database_schema_cache = (schema, now + AGENT_PROMPT_CACHE_TTL)
    return schema


async def _run_refresh_settings(
    notion: NotionClient, settings_path: Optional[str] = None
) -> str:
    """
    Pull all databases, build schema, store in Notion database at [root]/Settings/database.
    Uses a Notion database (not code block) so it can be filtered via the Notion API.
    """
    path = _resolve_database_settings_path(settings_path)
    dbs_raw = await notion.list_databases(force_refresh=True)

    # Build schema from each database
    databases: Dict[str, Dict[str, Any]] = {}
    for db in dbs_raw:
        db_id = db.get("id")
        db_title = notion._get_database_title(db)
        if not db_title and db_id:
            try:
                db = await notion.get_database(db_id)
                db_title = notion._get_database_title(db)
            except Exception:
                pass
        if not db_title or not db_id:
            continue
        db_key = db_title.strip().lower().replace(" ", "_").replace("/", "_")
        props_raw = db.get("properties") or {}
        properties: Dict[str, Dict[str, Any]] = {}
        for prop_name, prop_def in props_raw.items():
            key = _property_key(prop_name)
            if key:
                properties[key] = _extract_property_meta(prop_name, prop_def)
        databases[db_key] = {"id": db_id, "properties": properties}

    segments = path.split("/")
    settings_path_seg = "/".join(segments[:-1])
    db_title_seg = segments[-1]

    # Resolve settings page
    settings_id = await notion.find_page_by_path(settings_path_seg)
    if not settings_id:
        root_seg = segments[0]
        root_id = await notion.find_page_by_path(root_seg)
        if not root_id:
            raise ValueError(f"Root page '{root_seg}' not found. Create it first.")
        settings_page = await notion.create_child_page(root_id, "Settings")
        settings_id = settings_page["id"]

    # Delete existing registry database if present, then create fresh
    registry_db_id = await notion.find_page_by_path(path)
    if registry_db_id:
        try:
            await notion.delete_block(registry_db_id)
        except Exception:
            pass  # may already be gone or not a block

    reg_db = await notion.create_database(settings_id, db_title_seg, REGISTRY_DB_SCHEMA)
    registry_db_id = reg_db["id"]
    reg_schema = reg_db

    props_def = reg_schema.get("properties") or {}
    notion_id_prop = "Notion ID" if "Notion ID" in props_def else None
    props_prop = "Properties" if "Properties" in props_def else None
    if not notion_id_prop or not props_prop:
        raise ValueError(
            "Registry database missing required columns. Delete and run refresh again."
        )

    # Insert new rows (rich_text elements max 2000 chars each)
    def _rich_text(val: str) -> Dict:
        s = str(val)
        chunks = [s[i : i + 2000] for i in range(0, len(s), 2000)]
        return {
            "rich_text": [
                {"type": "text", "text": {"content": c, "link": None}} for c in chunks
            ]
        }

    for db_key, info in databases.items():
        props_json = json.dumps(info["properties"])
        await notion.create_page(
            registry_db_id,
            db_key,
            {
                notion_id_prop: _rich_text(info["id"]),
                props_prop: _rich_text(props_json),
            },
            _db=reg_schema,
        )

    global _database_schema_cache
    schema = {k: v["properties"] for k, v in databases.items()}
    _database_schema_cache = (schema, time.time() + AGENT_PROMPT_CACHE_TTL)

    return f"Refreshed. Stored {len(databases)} databases at {path}."


_agent_prompt_cache: Optional[tuple] = None  # (database + generic, expiry_time)


async def _get_agent_system_prompt(
    notion: NotionClient, settings_path: Optional[str] = None
) -> str:
    """Returns: skills + database list + settings context + GENERIC_SYSTEM_PROMPT."""
    global _agent_prompt_cache
    now = time.time()
    if _agent_prompt_cache and _agent_prompt_cache[1] > now:
        return _agent_prompt_cache[0]

    # Fetch database list and settings context in parallel
    path = _resolve_settings_path(settings_path)
    databases_text = ""
    notion_content = None

    try:
        databases = await notion.list_databases()
        if databases:
            db_lines = ["## Available Notion databases\n"]
            for db in databases:
                db_id = db.get("id", "")
                db_title = notion._get_database_title(db) or db_id
                db_lines.append(f"- **{db_title}** (id: `{db_id}`)")
            databases_text = "\n".join(db_lines)
    except Exception as e:
        logger.warning("Could not load database list for system prompt: %s", e)

    try:
        page_id = await notion.find_page_by_path(path)
        if page_id:
            notion_content = await notion.get_page_content_as_text(page_id)
    except Exception as e:
        logger.warning("Could not load agent prompt from Notion page: %s", e)

    # Order: local skills → database list → settings context → generic system prompt
    skills_text = _load_local_skills()
    parts = []
    if databases_text:
        parts.append(databases_text)
    if notion_content and notion_content.strip():
        parts.append(notion_content.strip())
    parts.append(GENERIC_SYSTEM_PROMPT)
    result = "\n\n---\n\n".join(parts)
    if skills_text:
        result = skills_text + result
    _agent_prompt_cache = (result, now + AGENT_PROMPT_CACHE_TTL)
    return result


_database_context_cache: Optional[tuple] = None  # (content, expiry_time)


async def _get_database_context(
    notion: NotionClient, settings_path: Optional[str] = None
) -> str:
    """Fetch database schema from [root]/Settings/database, format for Tier 2 extraction."""
    global _database_context_cache
    now = time.time()
    if _database_context_cache and _database_context_cache[1] > now:
        return _database_context_cache[0]

    try:
        schema = await _load_database_schema(notion, settings_path)
    except ValueError as e:
        logger.warning("Could not load database schema: %s", e)
        _database_context_cache = ("", now + AGENT_PROMPT_CACHE_TTL)
        return ""

    def _prop_display(prop_val: Any) -> str:
        if isinstance(prop_val, dict):
            name = prop_val.get("name", "?")
            ptype = prop_val.get("type", "")
            opts = prop_val.get("options")
            fmt = prop_val.get("format")
            if opts:
                return f"{name} ({ptype}: {', '.join(str(o) for o in opts[:8])}{'…' if len(opts) > 8 else ''})"
            if fmt and ptype == "number":
                return f"{name} ({ptype}, {fmt})"
            if ptype:
                return f"{name} ({ptype})"
            return name
        return str(prop_val)

    lines = ["Databases (from /nock refresh settings):"]
    for db_name, props in schema.items():
        parts = [_prop_display(v) for v in props.values()]
        lines.append(f"- {db_name}: {'; '.join(parts)}")
    content = "\n".join(lines)
    _database_context_cache = (content, now + AGENT_PROMPT_CACHE_TTL)
    return content


def _notion_error_msg(exc: httpx.HTTPStatusError) -> str:
    try:
        return exc.response.json().get("message", str(exc))
    except Exception:
        return str(exc)


def _resolve_property_name(
    schema: Dict[str, Dict[str, Any]], database_name: str, property_key: str
) -> Optional[str]:
    """Return the Notion property name for the given key. Supports rich and legacy schema format."""
    db_schema = schema.get(database_name.lower())
    if db_schema is None:
        return None
    val = db_schema.get(property_key.lower())
    if val is None:
        return None
    if isinstance(val, dict):
        return val.get("name")
    return str(val)  # legacy: key -> "Notion Name"


def _extract_plain_text(rich_text: list) -> str:
    return "".join(block.get("plain_text", "") for block in rich_text)


def _get_select_status_options(prop_def: Dict) -> List[str]:
    """Extract valid option names from select or status property schema."""
    prop_type = prop_def.get("type")
    if prop_type == "select":
        opts = prop_def.get("select") or {}
        return [o["name"] for o in opts.get("options", []) if o.get("name")]
    if prop_type == "status":
        opts = prop_def.get("status") or {}
        return [o["name"] for o in opts.get("options", []) if o.get("name")]
    return []


def _resolve_select_status_value(
    prop_def: Dict, value: Any, aliases: Dict[str, str]
) -> Optional[str]:
    """Resolve value to a valid select/status option. Returns None if no match."""
    if not value:
        return None
    val = str(value).strip()
    options = _get_select_status_options(prop_def)
    if not options:
        return val  # No options defined, pass through (might be dynamic select)
    options_lower = {o.lower(): o for o in options}
    # Exact match (case-insensitive)
    if val.lower() in options_lower:
        return options_lower[val.lower()]
    # Alias match
    alias = aliases.get(val) or aliases.get(val.title())
    if alias and alias.lower() in options_lower:
        return options_lower[alias.lower()]
    return None


async def _resolve_properties_for_notion(
    notion: NotionClient, page_id: str, properties: Dict[str, Any]
) -> Dict[str, Any]:
    """Resolve select/status values against database schema; returns dict safe for Notion API."""
    if not properties:
        return properties
    try:
        page = await notion.get_page(page_id)
        db_id = page.get("parent") and page["parent"].get("database_id")
        if not db_id:
            return properties
        db = await notion.get_database(db_id)
        schema = db.get("properties", {})
        resolved: Dict[str, Any] = {}
        for prop_name, value in properties.items():
            prop_def = schema.get(prop_name)
            if prop_def and prop_def.get("type") in ("select", "status"):
                r = _resolve_select_status_value(prop_def, value, STATUS_OPTION_ALIASES)
                if r is not None:
                    resolved[prop_name] = r
            else:
                resolved[prop_name] = value
        return resolved
    except Exception as e:
        logger.warning("Could not resolve select/status options: %s", e)
        return properties


def _find_database_by_title(
    databases: List[Dict], title: str, notion: NotionClient
) -> Optional[Dict]:
    """Find a database by its title (case-insensitive partial match)."""
    target = title.lower().strip()
    for db in databases:
        db_title = (notion._get_database_title(db) or "").lower()
        if target in db_title or db_title in target:
            return db
    return None


def _normalize_title_for_dedup(s: str) -> str:
    """Normalize title for deduplication: lowercase, collapse whitespace, strip punctuation."""
    if not s:
        return ""
    t = " ".join(s.strip().lower().split())
    # Strip common trailing punctuation that doesn't change identity (e.g. "Acme Corp." vs "Acme Corp")
    for p in (".", ",", ";", ":"):
        if t.endswith(p):
            t = t[:-1].strip()
    return t


def _get_notion_client_and_settings(
    args: Optional[Dict] = None,
) -> tuple:
    """Resolve Notion client and settings_path from notion_connections (by team_id or user_id)."""
    from crm_logger import get_notion_connection
    args = args or {}
    team_id = args.get("team_id") or (args.get("slack_context") or {}).get("team_id")
    user_id = args.get("user_id")
    conn = get_notion_connection(team_id=team_id, user_id=user_id)
    if not conn:
        raise ValueError(
            "No Notion connection. Connect Slack and Notion in the app (/integrations)."
        )
    return NotionClient(api_key=conn["access_token"]), conn.get("settings_path")


async def _handle_list_databases(client: NotionClient) -> Dict:
    try:
        databases = await client.list_databases()
        log_page_operation(operation="list_databases", success=True)
        return _ok(
            {
                "count": len(databases),
                "databases": [
                    {
                        "id": db["id"],
                        "title": _extract_plain_text(db.get("title", [])),
                        "url": db.get("url"),
                    }
                    for db in databases
                ],
            }
        )
    except httpx.HTTPStatusError as exc:
        err = _notion_error_msg(exc)
        log_page_operation(operation="list_databases", success=False, error=err)
        return _error(exc.response.status_code, err)


async def _handle_get_database(args: Dict, client: NotionClient) -> Dict:
    database_id = args.get("database_id")
    if not database_id:
        return _error(400, "Missing: database_id")

    try:
        result = await client.get_database(database_id)
        log_page_operation(
            operation="get_database", success=True, database_id=database_id
        )
        return _ok(result)
    except httpx.HTTPStatusError as exc:
        err = _notion_error_msg(exc)
        log_page_operation(
            operation="get_database", success=False, error=err, database_id=database_id
        )
        return _error(exc.response.status_code, err)


async def _handle_get_database_pages(args: Dict, client: NotionClient) -> Dict:
    database_id = args.get("database_id")
    if not database_id:
        return _error(400, "Missing: database_id")

    limit: Optional[int] = None
    raw_limit = args.get("limit")
    if raw_limit is not None:
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError):
            return _error(400, "Invalid: limit must be an integer")

    try:
        pages = await client.query_database(database_id, limit=limit)
        log_page_operation(
            operation="get_database_pages", success=True, database_id=database_id
        )
        return _ok({"count": len(pages), "pages": pages})
    except httpx.HTTPStatusError as exc:
        err = _notion_error_msg(exc)
        log_page_operation(
            operation="get_database_pages",
            success=False,
            error=err,
            database_id=database_id,
        )
        return _error(exc.response.status_code, err)


async def _handle_update_page(args: Dict, client: NotionClient) -> Dict:
    page_id = args.get("page_id")
    properties = args.get("properties")
    if not page_id:
        return _error(400, "Missing: page_id")
    if not properties or not isinstance(properties, dict):
        return _error(400, "Missing or invalid: properties (must be a dict)")

    properties = await _resolve_properties_for_notion(client, page_id, properties)
    try:
        await client.update_page_properties(page_id, properties)
        log_page_operation(
            operation="update_page",
            success=True,
            page_id=page_id,
            properties=properties,
        )
        logger.info("Updated page %s properties: %s", page_id, list(properties.keys()))
        return _ok(
            {
                "success": True,
                "page_id": page_id,
                "updated_properties": list(properties.keys()),
                "notion_page_url": f"https://www.notion.so/{page_id.replace('-', '')}",
            }
        )
    except httpx.HTTPStatusError as exc:
        err = _notion_error_msg(exc)
        log_page_operation(
            operation="update_page", success=False, error=err, page_id=page_id
        )
        return _error(exc.response.status_code, err)
    except ValueError as exc:
        log_page_operation(
            operation="update_page", success=False, error=str(exc), page_id=page_id
        )
        return _error(400, str(exc))


async def _handle_update_page_by_key(
    args: Dict, client: NotionClient, settings_path: Optional[str] = None
) -> Dict:
    page_id = args.get("page_id")
    database_name = args.get("database_name")
    property_key = args.get("property_key")
    property_value = args.get("property_value")

    if not page_id:
        return _error(400, "Missing: page_id")
    if not database_name:
        return _error(400, "Missing: database_name")
    if not property_key:
        return _error(400, "Missing: property_key")
    if property_value is None:
        return _error(400, "Missing: property_value")

    try:
        schema = await _load_database_schema(client, settings_path)
    except ValueError as e:
        return _error(400, str(e))

    db = database_name.lower()
    if db not in schema:
        return _error(
            400,
            (
                f"Unknown database_name '{database_name}'. "
                f"Valid options: {list(schema.keys())}"
            ),
        )

    notion_prop_name = _resolve_property_name(schema, database_name, property_key)
    if notion_prop_name is None:
        db_schema = schema[db]
        return _error(
            400,
            (
                f"Unknown property_key '{property_key}' for database '{database_name}'. "
                f"Valid keys: {list(db_schema.keys())}"
            ),
        )

    try:
        await client.update_page_properties(page_id, {notion_prop_name: property_value})
        log_page_operation(
            operation="update_page_by_key",
            success=True,
            page_id=page_id,
            database_name=database_name,
            property_key=property_key,
            properties={notion_prop_name: property_value},
        )
        logger.info(
            "Updated page %s: %s (%s) -> %s",
            page_id,
            notion_prop_name,
            property_key,
            property_value,
        )
        return _ok(
            {
                "success": True,
                "page_id": page_id,
                "updated_properties": [notion_prop_name],
                "notion_page_url": f"https://www.notion.so/{page_id.replace('-', '')}",
            }
        )
    except httpx.HTTPStatusError as exc:
        err = _notion_error_msg(exc)
        log_page_operation(
            operation="update_page_by_key",
            success=False,
            error=err,
            page_id=page_id,
            database_name=database_name,
            property_key=property_key,
        )
        return _error(exc.response.status_code, err)
    except ValueError as exc:
        log_page_operation(
            operation="update_page_by_key",
            success=False,
            error=str(exc),
            page_id=page_id,
            database_name=database_name,
            property_key=property_key,
        )
        return _error(400, str(exc))


async def _handle_create_page(args: Dict, client: NotionClient) -> Dict:
    database_id = args.get("database_id")
    title = args.get("title")
    properties = args.get("properties")

    if not database_id:
        return _error(400, "Missing: database_id")
    if not title:
        return _error(400, "Missing: title")

    extra: Optional[Dict] = None
    if properties and isinstance(properties, dict):
        try:
            db = await client.get_database(database_id)
        except httpx.HTTPStatusError as exc:
            return _error(exc.response.status_code, _notion_error_msg(exc))

        db_props = db.get("properties", {})
        extra = {}
        for prop_name, value in properties.items():
            prop_schema = db_props.get(prop_name)
            if prop_schema is None:
                return _error(
                    400,
                    (
                        f"Property '{prop_name}' not found in database '{database_id}'. "
                        f"Available: {list(db_props.keys())}"
                    ),
                )
            extra[prop_name] = client.build_property_value(prop_schema["type"], value)

    try:
        page = await client.create_page(database_id, title, extra)
        page_id = page["id"]
        log_page_operation(
            operation="create_page",
            success=True,
            page_id=page_id,
            database_id=database_id,
            properties=properties,
        )
        logger.info("Created page %s in database %s", page_id, database_id)
        return _ok(
            {
                "success": True,
                "page_id": page_id,
                "notion_page_url": f"https://www.notion.so/{page_id.replace('-', '')}",
                "page": page,
            }
        )
    except httpx.HTTPStatusError as exc:
        err = _notion_error_msg(exc)
        log_page_operation(
            operation="create_page",
            success=False,
            error=err,
            database_id=database_id,
            properties=properties,
        )
        return _error(exc.response.status_code, err)


async def _handle_get_page(args: Dict, client: NotionClient) -> Dict:
    page_id = args.get("page_id")
    if not page_id:
        return _error(400, "Missing: page_id")

    try:
        result = await client.get_page(page_id)
        log_page_operation(operation="get_page", success=True, page_id=page_id)
        return _ok(result)
    except httpx.HTTPStatusError as exc:
        err = _notion_error_msg(exc)
        log_page_operation(
            operation="get_page", success=False, error=err, page_id=page_id
        )
        return _error(exc.response.status_code, err)


async def _handle_get_schema(
    args: Dict, client: NotionClient, settings_path: Optional[str] = None
) -> Dict:
    database_name = args.get("database_name")
    if not database_name:
        return _error(400, "Missing: database_name")

    try:
        full_schema = await _load_database_schema(client, settings_path)
    except ValueError as e:
        return _error(404, str(e))

    schema = full_schema.get(database_name.lower())
    if schema is None:
        return _error(
            404,
            (
                f"No schema for '{database_name}'. "
                f"Available: {list(full_schema.keys())}"
            ),
        )
    return _ok(schema)


async def _dispatch_api(args: Dict) -> Dict:
    action = args.get("action")
    if not action:
        return _error(400, "Missing required parameter: action")

    try:
        client, settings_path = _get_notion_client_and_settings(args)
    except ValueError as exc:
        return _error(500, str(exc))

    # Databases
    if action == "list_databases":
        return await _handle_list_databases(client)
    if action == "get_database":
        return await _handle_get_database(args, client)
    if action == "get_database_pages":
        return await _handle_get_database_pages(args, client)

    # Pages
    if action == "update_page":
        return await _handle_update_page(args, client)
    if action == "update_page_by_key":
        return await _handle_update_page_by_key(args, client, settings_path)
    if action == "create_page":
        return await _handle_create_page(args, client)
    if action == "get_page":
        return await _handle_get_page(args, client)
    if action == "get_schema":
        return await _handle_get_schema(args, client, settings_path)

    return _error(
        400,
        (
            f"Unknown action '{action}'. "
            "Valid: list_databases, get_database, get_database_pages, "
            "update_page, update_page_by_key, create_page, get_page, get_schema"
        ),
    )
