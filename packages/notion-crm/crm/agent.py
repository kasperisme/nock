import json
import logging
import os
from typing import Any, Dict, List, Optional

import httpx
from anthropic import AsyncAnthropic

from notion_client import NotionClient
from crm_logger import (
    get_agent_conversation,
    log_agent_run,
    save_agent_conversation,
)

from config import (
    FEEDBACK_SYSTEM_ADDITION,
    MAX_ITERATIONS,
    MODEL_AGENT,
    PERPLEXITY_MODEL,
    PERPLEXITY_URL,
    REGULAR_AGENT_SYSTEM_ADDITION,
    SLASH_AGENT_SYSTEM_ADDITION,
    _error,
    _ok,
    _feedback_tools_for_claude,
    _notion_tools_for_claude,
)
from notion_utils import (
    _get_agent_system_prompt,
    _get_notion_client_and_settings,
    _normalize_title_for_dedup,
    _resolve_properties_for_notion,
    _resolve_settings_path,
)

logger = logging.getLogger(__name__)


async def _call_perplexity(query: str) -> Dict[str, Any]:
    api_key = os.environ.get("PERPLEXITY_API_KEY")
    if not api_key:
        return {"error": "PERPLEXITY_API_KEY is not set. Research is unavailable."}

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            PERPLEXITY_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": PERPLEXITY_MODEL,
                "messages": [{"role": "user", "content": query}],
                "max_tokens": 4096,
                "temperature": 0.2,
            },
        )
    if resp.status_code != 200:
        try:
            err = resp.json().get("error", {}).get("message", resp.text)
        except Exception:
            err = resp.text
        return {"error": f"Perplexity API error {resp.status_code}: {err}"}

    data = resp.json()
    choices = data.get("choices", [])
    if not choices:
        return {"error": "Perplexity returned no choices"}
    content = choices[0].get("message", {}).get("content", "")
    citations = data.get("citations", [])

    result: Dict[str, Any] = {"content": content}
    if citations:
        result["citations"] = citations
    return result


def _notion_url(page_id: str) -> str:
    """Return the canonical Notion page URL for a given page ID."""
    return "https://notion.so/" + page_id.replace("-", "")


async def _execute_tool(
    notion: NotionClient,
    name: str,
    args: Dict[str, Any],
    settings_path: Optional[str] = None,
) -> Any:
    try:
        if name == "ask_user":
            return {"__ask_user__": args.get("question", "")}
        if name == "research":
            return await _call_perplexity(args.get("query", ""))
        if name == "get_all_databases":
            return await notion.list_databases()
        if name == "get_database":
            return await notion.get_database(args["database_id"])
        if name == "get_database_pages":
            db_id = args["database_id"]
            limit = args.get("limit")
            title_search = args.get("title_search")
            rel_prop = args.get("relation_filter_property")
            rel_page_id = args.get("relation_filter_page_id")
            filt = None
            if rel_prop and rel_page_id:
                filt = {
                    "property": rel_prop,
                    "relation": {"contains": rel_page_id},
                }
            pages = await notion.query_database(
                db_id,
                limit=limit,
                title_search=title_search,
                filter=filt,
            )
            for p in pages:
                if isinstance(p, dict) and p.get("id"):
                    p["notion_url"] = _notion_url(p["id"])
            return pages
        if name == "get_page":
            page = await notion.get_page(args["page_id"])
            if isinstance(page, dict) and page.get("id"):
                page["notion_url"] = _notion_url(page["id"])
            return page
        if name == "create_page":
            raw_props = args.get("properties") or {}
            db_id = args["database_id"]
            title = args.get("title") or "Untitled"
            # Search-before-create: avoid duplicates by updating existing if found
            existing = await notion.query_database(
                db_id, limit=1, title_search=title.strip()
            )
            if existing and existing[0]:
                page_title = notion._get_page_title(existing[0])
                if _normalize_title_for_dedup(page_title) == _normalize_title_for_dedup(
                    title
                ):
                    page_id = existing[0]["id"]
                    resolved = await _resolve_properties_for_notion(
                        notion, page_id, raw_props
                    )
                    if resolved:
                        await notion.update_page_properties(page_id, resolved)
                    return {
                        "updated_existing": True,
                        "page_id": page_id,
                        "notion_url": _notion_url(page_id),
                        "title": page_title,
                        "message": f"Record '{title}' already existed; updated instead of creating duplicate.",
                    }
            # No matching record — create new
            db = await notion.get_database(db_id)
            db_schema = db.get("properties", {})
            extra: Optional[Dict[str, Any]] = None
            if raw_props:
                extra = {}
                for prop_name, value in raw_props.items():
                    prop_def = db_schema.get(prop_name)
                    if prop_def is None:
                        raise ValueError(
                            f"Property '{prop_name}' not found. "
                            f"Available: {list(db_schema.keys())}"
                        )
                    extra[prop_name] = notion.build_property_value(
                        prop_def["type"], value
                    )
            page = await notion.create_page(db_id, title, extra, _db=db)
            page_id = page.get("id", "")
            if page_id:
                page["notion_url"] = _notion_url(page_id)
            return page
        if name == "update_page":
            props = await _resolve_properties_for_notion(
                notion, args["page_id"], args.get("properties") or {}
            )
            page = await notion.update_page_properties(args["page_id"], props)
            page_id = page.get("id", "") if isinstance(page, dict) else ""
            if page_id:
                page["notion_url"] = _notion_url(page_id)
            return page
        if name == "get_settings_context" and settings_path:
            path = _resolve_settings_path(settings_path)
            page_id = await notion.find_page_by_path(path)
            if not page_id:
                return {"error": f"Settings/Context page not found at {path}"}
            content = await notion.get_page_content_as_text(page_id)
            return {"content": content or ""}
        if name == "update_settings_context" and settings_path:
            path = _resolve_settings_path(settings_path)
            page_id = await notion.find_page_by_path(path)
            if not page_id:
                return {"error": f"Settings/Context page not found at {path}"}
            content = (args.get("content") or "").strip()
            if not content:
                return {"error": "Content is required and cannot be empty"}
            await notion.replace_page_content(page_id, content)
            import notion_utils as _notion_utils_mod
            _notion_utils_mod._agent_prompt_cache = None
            return {"success": True, "message": "Settings/Context updated"}
        return {"error": f"Unknown tool: {name}"}
    except httpx.HTTPStatusError as exc:
        try:
            detail = exc.response.json().get("message", str(exc))
        except Exception:
            detail = str(exc)
        return {"error": f"Notion API error {exc.response.status_code}: {detail}"}
    except Exception as exc:
        return {"error": str(exc)}



def _has_tool_result_content(msg: Dict[str, Any]) -> bool:
    content = msg.get("content", "")
    if not isinstance(content, list):
        return False
    return any(
        isinstance(b, dict) and b.get("type") == "tool_result"
        for b in content
    )


def _sanitize_message_history(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Ensure every assistant message with tool_use blocks is immediately followed by a
    user message containing matching tool_results. Drop any pair (or trailing assistant
    message) where the pairing is missing or incomplete — these indicate a previously
    corrupted conversation state and would cause a 400 from the Anthropic API.

    Also strips leading user messages whose content is all tool_result blocks — these
    appear when a [-N:] slice cuts off the assistant tool_use that preceded them.
    """
    # Strip any leading user messages that only contain tool_result blocks.
    # They are orphaned because the matching assistant tool_use was sliced away.
    while messages and messages[0].get("role") == "user" and _has_tool_result_content(messages[0]):
        logger.warning("Dropping orphaned leading tool_result user message from history")
        messages = messages[1:]

    sanitized: List[Dict[str, Any]] = []
    i = 0
    while i < len(messages):
        msg = messages[i]
        if msg.get("role") == "assistant":
            content = msg.get("content", "")
            tool_use_ids: List[str] = []
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        tool_use_ids.append(block.get("id", ""))
            if tool_use_ids:
                # Must be immediately followed by a user message with ALL tool_results
                if i + 1 < len(messages):
                    next_msg = messages[i + 1]
                    next_content = next_msg.get("content", []) if next_msg.get("role") == "user" else []
                    result_ids = {
                        b.get("tool_use_id")
                        for b in (next_content if isinstance(next_content, list) else [])
                        if isinstance(b, dict) and b.get("type") == "tool_result"
                    }
                    if result_ids.issuperset(set(tool_use_ids)):
                        sanitized.append(msg)
                        sanitized.append(next_msg)
                        i += 2
                        continue
                # Trailing assistant message with no reply yet — this is a pending ask_user
                # whose tool_result will be injected on the next turn. Keep it.
                if i == len(messages) - 1:
                    sanitized.append(msg)
                    i += 1
                    continue
                # Unpaired mid-history — drop this assistant message (and skip the next if it's a user message)
                logger.warning(
                    "Dropping unpaired tool_use message from history (ids=%s)", tool_use_ids
                )
                if i + 1 < len(messages) and messages[i + 1].get("role") == "user":
                    i += 2  # skip both the bad assistant msg and its incomplete user msg
                else:
                    i += 1
                continue
        sanitized.append(msg)
        i += 1
    return sanitized


async def _run_agent(args: Dict) -> Dict:
    prompt = (args.get("prompt") or "").strip()
    if not prompt:
        return _error(400, "Missing required parameter: prompt")

    slack_context = args.get("slack_context")
    slack_team_id = args.get("team_id") or ((slack_context or {}).get("team_id"))

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return _error(500, "ANTHROPIC_API_KEY is not set")

    try:
        notion, settings_path = _get_notion_client_and_settings(args)
    except ValueError as exc:
        return _error(500, str(exc))

    anthropic_client = AsyncAnthropic(api_key=api_key)

    base = await _get_agent_system_prompt(notion, settings_path)
    is_slack = args.get("is_slack", False)
    use_feedback = args.get("use_feedback", False)
    max_iterations = args.get("max_iterations") or MAX_ITERATIONS
    if use_feedback:
        agent_addition = FEEDBACK_SYSTEM_ADDITION
        tools_claude = _feedback_tools_for_claude()
    else:
        agent_addition = (
            SLASH_AGENT_SYSTEM_ADDITION if is_slack else REGULAR_AGENT_SYSTEM_ADDITION
        )
        tools_claude = _notion_tools_for_claude()
    system_prompt = base + "\n\n---\n\n" + agent_addition

    user_content = prompt
    if slack_context:
        user_content += f"\n\nSlack context:\n{json.dumps(slack_context, indent=2)}"

    # Resolve (team, user) from slash command / request — all agents reuse conversation by this key
    slack_user_id = args.get("user_id") or ((slack_context or {}).get("user_id"))
    tool_calls_made: List[Dict[str, Any]] = []
    iterations = 0

    # Load previous message history for multi-turn (Claude uses messages, not conversation IDs)
    message_history: List[Dict[str, Any]] = []
    if slack_team_id and slack_user_id:
        existing = get_agent_conversation(
            slack_team_id=slack_team_id, slack_user_id=slack_user_id
        )
        if existing and existing.get("message_history"):
            hist = existing["message_history"]
            if isinstance(hist, list):
                message_history = _sanitize_message_history(hist[-20:])

    # Build messages: history + new user message
    # If this invocation is a reply to an ask_user question, inject as tool_result
    # so the message alternation (user/assistant/user) stays valid for the API.
    ask_user_tool_use_id = (args.get("_ask_user_tool_use_id") or "").strip()
    messages: List[Dict[str, Any]] = []
    for m in message_history:
        messages.append({"role": m["role"], "content": m["content"]})
    if ask_user_tool_use_id:
        messages.append({
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": ask_user_tool_use_id, "content": prompt}],
        })
    else:
        messages.append({"role": "user", "content": user_content})

    last_response_id: Optional[str] = None

    def _save_message_history(msgs: List[Dict[str, Any]]) -> None:
        """Persist message history for next turn."""
        if slack_team_id and slack_user_id and msgs:
            save_agent_conversation(
                message_history=msgs,
                slack_team_id=slack_team_id,
                slack_user_id=slack_user_id,
            )

    try:
        while iterations < max_iterations:
            iterations += 1

            create_kwargs: Dict[str, Any] = {
                "model": MODEL_AGENT,
                "max_tokens": 8192,
                "system": system_prompt,
                "messages": messages,
                "tools": tools_claude,
                "tool_choice": {"type": "auto"},
            }

            response = await anthropic_client.messages.create(**create_kwargs)
            last_response_id = getattr(response, "id", None) or ""

            # Extract content blocks from response (Anthropic SDK returns objects)
            raw_blocks = response.content or []
            content_blocks: List[Dict[str, Any]] = []
            for b in raw_blocks:
                if isinstance(b, dict):
                    content_blocks.append(b)
                elif hasattr(b, "model_dump"):
                    content_blocks.append(b.model_dump())
                else:
                    content_blocks.append(b)

            # Collect tool_use blocks
            tool_uses: List[Dict[str, Any]] = []
            text_parts: List[str] = []
            for block in content_blocks:
                b = block if isinstance(block, dict) else {}
                if b.get("type") == "tool_use":
                    tool_uses.append(b)
                elif b.get("type") == "text":
                    text_parts.append(b.get("text", ""))

            if not tool_uses:
                # Final text response
                output_text = "".join(text_parts).strip()
                log_agent_run(
                    prompt=prompt,
                    model=MODEL_AGENT,
                    response=output_text,
                    iterations=iterations,
                    tool_calls=tool_calls_made,
                    success=True,
                    slack_context=slack_context,
                    slack_team_id=slack_team_id,
                )
                # Append assistant + user (final) to history for next turn
                new_history = message_history + [
                    {"role": "user", "content": user_content},
                    {"role": "assistant", "content": output_text},
                ]
                _save_message_history(new_history)
                return _ok(
                    {
                        "response": output_text,
                        "iterations": iterations,
                        "tool_calls_made": tool_calls_made,
                    }
                )

            # Execute tools and build tool_result blocks
            tool_results: List[Dict[str, Any]] = []
            for tu in tool_uses:
                name = tu.get("name", "")
                tool_input = tu.get("input") or {}
                tool_use_id = tu.get("id", "")
                if isinstance(tool_input, str):
                    try:
                        tool_input = json.loads(tool_input)
                    except json.JSONDecodeError:
                        tool_input = {}
                logger.info("Agent calling tool %s with args %s", name, tool_input)
                result = await _execute_tool(
                    notion, name, tool_input, settings_path=settings_path
                )
                result_json = json.dumps(result, default=str)
                tool_calls_made.append(
                    {
                        "tool": name,
                        "args": tool_input,
                        "result_summary": result_json[:300],
                    }
                )
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": result_json,
                    }
                )

            # Check if any tool result is an ask_user signal
            ask_question: Optional[str] = None
            for tr in tool_results:
                try:
                    r = json.loads(tr.get("content", "{}"))
                    if isinstance(r, dict) and "__ask_user__" in r:
                        ask_question = r["__ask_user__"]
                        break
                except Exception:
                    pass

            if ask_question:
                # Find the tool_use_id so the reply can be injected as a proper tool_result
                ask_user_tu_id = ""
                for tu in tool_uses:
                    if tu.get("name") == "ask_user":
                        ask_user_tu_id = tu.get("id", "")
                        break
                # Save history with only the ask_user tool_use block in the assistant message.
                # Other tool_use blocks are stripped — they have no corresponding tool_results
                # saved in history, which would cause a 400 on the next API call.
                # The user's reply is injected as the tool_result for ask_user on the next turn.
                ask_user_content_blocks = [
                    b for b in content_blocks
                    if not (
                        isinstance(b, dict)
                        and b.get("type") == "tool_use"
                        and b.get("name") != "ask_user"
                    )
                ]
                new_history = message_history + [
                    {"role": "user", "content": user_content},
                    {"role": "assistant", "content": ask_user_content_blocks},
                ]
                _save_message_history(new_history)
                return _ok({
                    "response": ask_question,
                    "ask_user": True,
                    "ask_user_tool_use_id": ask_user_tu_id,
                })

            # Append assistant message (with tool_use) + user message (with tool_result)
            assistant_content = content_blocks
            messages.append({"role": "assistant", "content": assistant_content})
            messages.append({"role": "user", "content": tool_results})

        last_error = (
            f"Agent exceeded maximum iterations ({max_iterations}) without finishing."
        )
        log_agent_run(
            prompt=prompt,
            model=MODEL_AGENT,
            response="",
            iterations=iterations,
            tool_calls=tool_calls_made,
            success=False,
            error=last_error,
            slack_context=slack_context,
            slack_team_id=slack_team_id,
        )
        return _error(500, last_error)

    except Exception as exc:
        last_error = str(exc)
        log_agent_run(
            prompt=prompt,
            model=MODEL_AGENT,
            response="",
            iterations=iterations,
            tool_calls=tool_calls_made,
            success=False,
            error=last_error,
            slack_context=slack_context,
            slack_team_id=slack_team_id,
        )
        return _error(500, last_error)
