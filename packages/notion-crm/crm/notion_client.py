import asyncio
import time
import httpx
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Persistent HTTP client (reused within same event loop)
# Reset when loop changes (serverless: each asyncio.run() = new loop)
# ---------------------------------------------------------------------------

_http_client: Optional[httpx.AsyncClient] = None
_http_client_loop_id: Optional[int] = None


async def close_http_client() -> None:
    global _http_client, _http_client_loop_id
    if _http_client is not None and not _http_client.is_closed:
        await _http_client.aclose()
    _http_client = None
    _http_client_loop_id = None


def _get_http_client() -> httpx.AsyncClient:
    global _http_client, _http_client_loop_id
    try:
        loop = asyncio.get_running_loop()
        loop_id = id(loop)
    except RuntimeError:
        loop_id = None
    if _http_client is not None and (
        _http_client.is_closed or _http_client_loop_id != loop_id
    ):
        _http_client = None
        _http_client_loop_id = None
    if _http_client is None:
        _http_client = httpx.AsyncClient(timeout=30.0)
        _http_client_loop_id = loop_id
    return _http_client


# ---------------------------------------------------------------------------
# TTL cache for slow-changing Notion data (database schemas and list)
# ---------------------------------------------------------------------------

_CACHE_TTL = 900  # 15 minutes
_cache: Dict[str, Tuple[Any, float]] = {}


def _cache_get(key: str) -> Optional[Any]:
    entry = _cache.get(key)
    if entry and (time.time() - entry[1]) < _CACHE_TTL:
        return entry[0]
    return None


def _cache_set(key: str, value: Any) -> None:
    _cache[key] = (value, time.time())


class NotionClient:
    BASE_URL = "https://api.notion.com/v1"
    NOTION_VERSION = "2022-06-28"

    def __init__(self, api_key: str):
        if not api_key:
            raise ValueError("api_key is required")
        self.api_key = api_key
        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Notion-Version": self.NOTION_VERSION,
            "Content-Type": "application/json",
        }

    async def get_page(self, page_id: str) -> Dict:
        client = _get_http_client()
        resp = await client.get(
            f"{self.BASE_URL}/pages/{page_id}",
            headers=self.headers,
        )
        resp.raise_for_status()
        return resp.json()

    async def update_page(self, page_id: str, properties: Dict) -> Dict:
        client = _get_http_client()
        resp = await client.patch(
            f"{self.BASE_URL}/pages/{page_id}",
            headers=self.headers,
            json={"properties": properties},
        )
        resp.raise_for_status()
        return resp.json()

    def _extract_rich_text_content(self, value: Any) -> str:
        """Extract plain text from value. Handles agent passing Notion API structures."""
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            rt = value.get("rich_text") or value.get("text")
            if isinstance(rt, list) and rt:
                first = rt[0]
                if isinstance(first, dict):
                    inner = first.get("text") or first
                    if isinstance(inner, dict) and "content" in inner:
                        return inner["content"]
            if "content" in value:
                return value["content"]
        return str(value)

    def build_property_value(self, prop_type: str, value: Any) -> Dict:
        """Build a Notion property object for the given type and value."""
        if prop_type == "email":
            return {"email": value}
        if prop_type == "select":
            return {"select": {"name": value} if value else None}
        if prop_type == "multi_select":
            names = (
                [v.strip() for v in value.split(",")]
                if isinstance(value, str)
                else value
            )
            return {"multi_select": [{"name": n} for n in names]}
        if prop_type == "date":
            if isinstance(value, str):
                return {"date": {"start": value}}
            if isinstance(value, dict):
                return {"date": value}
            return {"date": None}
        if prop_type == "rich_text":
            content = self._extract_rich_text_content(value)
            return {"rich_text": [{"text": {"content": content}}]}
        if prop_type == "title":
            content = self._extract_rich_text_content(value)
            return {"title": [{"text": {"content": content}}]}
        if prop_type == "checkbox":
            if isinstance(value, bool):
                return {"checkbox": value}
            return {"checkbox": str(value).lower() in ("true", "1", "yes")}
        if prop_type == "url":
            return {"url": value}
        if prop_type == "phone_number":
            return {"phone_number": value}
        if prop_type == "number":
            return {"number": float(value)}
        if prop_type == "relation":
            ids = (
                [v.strip() for v in value.split(",")]
                if isinstance(value, str)
                else value
            )
            return {"relation": [{"id": i} for i in ids]}
        if prop_type == "people":
            ids = (
                [v.strip() for v in value.split(",")]
                if isinstance(value, str)
                else value
            )
            return {"people": [{"object": "user", "id": i} for i in ids]}
        if prop_type == "status":
            return {"status": {"name": value}}

        raise ValueError(f"Unsupported property type: '{prop_type}'")

    async def update_page_properties(
        self,
        page_id: str,
        updates: Dict[str, Any],
    ) -> Dict:
        """
        Update one or more page properties.
        Property types are auto-detected by fetching the current page.
        """
        page = await self.get_page(page_id)
        existing = page.get("properties", {})

        properties: Dict[str, Any] = {}
        for prop_name, value in updates.items():
            prop = existing.get(prop_name)
            if prop is None:
                raise ValueError(
                    f"Property '{prop_name}' not found on page '{page_id}'. "
                    f"Available: {list(existing.keys())}"
                )
            prop_type = prop["type"]
            properties[prop_name] = self.build_property_value(prop_type, value)

        return await self.update_page(page_id, properties)

    async def list_databases(self, force_refresh: bool = False) -> List[Dict]:
        """Return all databases the integration has access to."""
        if not force_refresh:
            cached = _cache_get("list_databases")
            if cached is not None:
                return cached

        results = []
        client = _get_http_client()
        for filter_val in ("data_source", "database"):
            body = {
                "filter": {"value": filter_val, "property": "object"},
                "page_size": 100,
            }
            try:
                while True:
                    resp = await client.post(
                        f"{self.BASE_URL}/search",
                        headers=self.headers,
                        json=body,
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    results.extend(data.get("results", []))
                    if not data.get("has_more"):
                        break
                    body["start_cursor"] = data["next_cursor"]
                if results:
                    break
            except Exception:
                results = []
                continue

        if not results:
            body = {"page_size": 100}
            while True:
                resp = await client.post(
                    f"{self.BASE_URL}/search",
                    headers=self.headers,
                    json=body,
                )
                resp.raise_for_status()
                data = resp.json()
                for r in data.get("results", []):
                    if r.get("object") in ("database", "data_source"):
                        results.append(r)
                if not data.get("has_more"):
                    break
                body["start_cursor"] = data["next_cursor"]

        _cache_set("list_databases", results)
        return results

    async def get_database(self, database_id: str) -> Dict:
        cache_key = f"db:{database_id}"
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached

        client = _get_http_client()
        resp = await client.get(
            f"{self.BASE_URL}/databases/{database_id}",
            headers=self.headers,
        )
        resp.raise_for_status()
        result = resp.json()
        _cache_set(cache_key, result)
        return result

    def _get_database_title(self, db: Dict) -> str:
        """Extract title from a database object. Title is at top-level or in properties."""
        title_arr = db.get("title")
        if isinstance(title_arr, list):
            return "".join(
                t.get("plain_text", t.get("text", {}).get("content", ""))
                for t in title_arr
            )
        for prop in (db.get("properties") or {}).values():
            if prop.get("type") == "title":
                tt = prop.get("title") or []
                return "".join(
                    t.get("plain_text", t.get("text", {}).get("content", ""))
                    for t in tt
                )
        return ""

    async def create_child_page(self, parent_page_id: str, title: str) -> Dict:
        """Create a child page under parent. Returns the new page object."""
        client = _get_http_client()
        resp = await client.post(
            f"{self.BASE_URL}/pages",
            headers=self.headers,
            json={
                "parent": {"page_id": parent_page_id},
                "properties": {
                    "title": {"title": [{"text": {"content": title}}]},
                },
            },
        )
        resp.raise_for_status()
        return resp.json()

    async def create_database(
        self, parent_page_id: str, title: str, property_schema: Dict[str, Any]
    ) -> Dict:
        """Create a database as child of page. Returns database object."""
        client = _get_http_client()
        resp = await client.post(
            f"{self.BASE_URL}/databases",
            headers=self.headers,
            json={
                "parent": {"type": "page_id", "page_id": parent_page_id},
                "title": [{"type": "text", "text": {"content": title}}],
                "properties": property_schema,
            },
        )
        resp.raise_for_status()
        return resp.json()

    async def append_code_block(
        self, parent_block_id: str, code: str, language: str = "json"
    ) -> Dict:
        """Append a code block to parent. Returns API response."""
        client = _get_http_client()
        chunk_size = 2000
        rich_text = []
        for i in range(0, len(code), chunk_size):
            chunk = code[i : i + chunk_size]
            rich_text.append(
                {
                    "type": "text",
                    "text": {"content": chunk, "link": None},
                }
            )
        resp = await client.patch(
            f"{self.BASE_URL}/blocks/{parent_block_id}/children",
            headers=self.headers,
            json={
                "children": [
                    {
                        "type": "code",
                        "code": {
                            "rich_text": rich_text,
                            "language": language,
                        },
                    }
                ],
            },
        )
        resp.raise_for_status()
        return resp.json()

    async def delete_block(self, block_id: str) -> None:
        """Archive (soft delete) a block."""
        client = _get_http_client()
        resp = await client.patch(
            f"{self.BASE_URL}/blocks/{block_id}",
            headers=self.headers,
            json={"archived": True},
        )
        resp.raise_for_status()

    async def replace_page_content(self, page_id: str, content: str) -> None:
        """
        Replace all block content of a page with the given text.
        Deletes existing child blocks, then appends a new paragraph block with the content.
        Content is chunked to respect Notion's 2000-char limit per rich_text item.
        """
        client = _get_http_client()
        blocks = await self.get_block_children(page_id)
        for b in blocks:
            await self.delete_block(b["id"])
        chunk_size = 2000
        rich_text = []
        for i in range(0, len(content), chunk_size):
            chunk = content[i : i + chunk_size]
            rich_text.append({"type": "text", "text": {"content": chunk, "link": None}})
        resp = await client.patch(
            f"{self.BASE_URL}/blocks/{page_id}/children",
            headers=self.headers,
            json={
                "children": [
                    {"type": "paragraph", "paragraph": {"rich_text": rich_text}}
                ]
            },
        )
        resp.raise_for_status()

    async def query_database(
        self,
        database_id: str,
        limit: Optional[int] = None,
        filter: Optional[Dict] = None,
        sorts: Optional[List[Dict]] = None,
        title_search: Optional[str] = None,
    ) -> List[Dict]:
        """Query pages in a database, respecting an optional limit.

        title_search: filters by the title/name property using a contains match.
        Uses the cached database schema to determine the title property name.
        """
        if title_search and not filter:
            db = await self.get_database(database_id)
            title_prop = next(
                (
                    name
                    for name, schema in db.get("properties", {}).items()
                    if schema["type"] == "title"
                ),
                "Name",
            )
            filter = {"property": title_prop, "title": {"contains": title_search}}

        results = []
        body: Dict[str, Any] = {"page_size": min(limit or 100, 100)}
        if filter:
            body["filter"] = filter
        if sorts:
            body["sorts"] = sorts

        client = _get_http_client()
        while True:
            resp = await client.post(
                f"{self.BASE_URL}/databases/{database_id}/query",
                headers=self.headers,
                json=body,
            )
            resp.raise_for_status()
            data = resp.json()
            results.extend(data.get("results", []))
            if limit and len(results) >= limit:
                return results[:limit]
            if not data.get("has_more"):
                break
            body["start_cursor"] = data["next_cursor"]
        return results

    async def search_pages(self, query: str = "", limit: int = 20) -> List[Dict]:
        """Search for pages. Returns list of page objects."""
        results = []
        body: Dict[str, Any] = {
            "filter": {"value": "page", "property": "object"},
            "page_size": min(limit, 100),
        }
        if query:
            body["query"] = query
        client = _get_http_client()
        while True:
            resp = await client.post(
                f"{self.BASE_URL}/search",
                headers=self.headers,
                json=body,
            )
            resp.raise_for_status()
            data = resp.json()
            results.extend(data.get("results", []))
            if len(results) >= limit or not data.get("has_more"):
                break
            body["start_cursor"] = data["next_cursor"]
        return results[:limit]

    async def get_block_children(self, block_id: str) -> List[Dict]:
        """Get child blocks of a page or block."""
        results = []
        client = _get_http_client()
        cursor = None
        while True:
            params: Dict[str, Any] = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            resp = await client.get(
                f"{self.BASE_URL}/blocks/{block_id}/children",
                headers=self.headers,
                params=params,
            )
            resp.raise_for_status()
            data = resp.json()
            results.extend(data.get("results", []))
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
        return results

    def _extract_text_from_block(self, block: Dict) -> str:
        """Extract plain text from a block. Returns empty string if no text."""
        block_type = block.get("type")
        type_data = block.get(block_type)
        if not type_data:
            return ""
        rich_text = type_data.get("rich_text") or type_data.get("text") or []
        if not isinstance(rich_text, list):
            return ""
        return "".join(
            rt.get("plain_text", rt.get("text", {}).get("content", ""))
            for rt in rich_text
        )

    async def get_page_content_as_text(self, page_id: str) -> str:
        """Fetch all blocks recursively and return concatenated plain text."""
        parts: List[str] = []

        async def _collect(bid: str) -> None:
            blocks = await self.get_block_children(bid)
            for b in blocks:
                text = self._extract_text_from_block(b)
                if text:
                    parts.append(text)
                    parts.append("\n")
                if b.get("has_children") and b.get("type") not in (
                    "child_page",
                    "child_database",
                ):
                    await _collect(b["id"])

        await _collect(page_id)
        return "".join(parts).strip()

    def _get_page_title(self, page: Dict) -> str:
        """Extract title from a page object (from properties)."""
        for prop in (page.get("properties") or {}).values():
            if prop.get("type") == "title":
                tt = prop.get("title") or []
                return "".join(
                    t.get("plain_text", t.get("text", {}).get("content", ""))
                    for t in tt
                )
        return ""

    def _get_child_page_title(self, block: Dict) -> str:
        """Extract title from a child_page block."""
        cp = block.get("child_page") or block.get("child_database") or {}
        return cp.get("title", "")

    async def find_page_by_path(self, path: str) -> Optional[str]:
        """
        Find a page by path like "CRM/settings/agent".
        Returns page_id or None. Traverses: search for first segment, get children, find by title.
        """
        segments = [s.strip() for s in path.split("/") if s.strip()]
        if not segments:
            return None

        # Search for root page
        pages = await self.search_pages(query=segments[0], limit=10)
        root = None
        for p in pages:
            if self._get_page_title(p).strip().lower() == segments[0].lower():
                root = p
                break
        if not root:
            return None
        if len(segments) == 1:
            return root["id"]

        current_id = root["id"]
        for seg in segments[1:]:
            blocks = await self.get_block_children(current_id)
            found = None
            for b in blocks:
                if b.get("type") in ("child_page", "child_database"):
                    title = self._get_child_page_title(b)
                    if title.strip().lower() == seg.lower():
                        found = b
                        break
            if not found:
                return None
            current_id = found["id"]

        return current_id

    async def create_page(
        self,
        database_id: str,
        title: str,
        properties: Optional[Dict[str, Any]] = None,
        _db: Optional[Dict] = None,
    ) -> Dict:
        """
        Create a new page in a database.
        `title` sets the title property (auto-detected from schema).
        `properties` can contain additional property values already formatted for the Notion API.
        `_db` can be passed to avoid a redundant get_database call if already fetched.
        """
        db = _db or await self.get_database(database_id)
        title_prop_name = next(
            (
                name
                for name, schema in db.get("properties", {}).items()
                if schema["type"] == "title"
            ),
            "Name",
        )
        payload: Dict[str, Any] = {
            "parent": {"database_id": database_id},
            "properties": {
                title_prop_name: {"title": [{"text": {"content": title}}]},
            },
        }
        if properties:
            payload["properties"].update(properties)

        client = _get_http_client()
        resp = await client.post(
            f"{self.BASE_URL}/pages",
            headers=self.headers,
            json=payload,
        )
        resp.raise_for_status()
        return resp.json()
