#!/usr/bin/env python3
"""
E2E BDD tests for the CRM integration service.

INTEGRATION tests — call the real Anthropic API.
Notion, Supabase, and Slack are mocked to return realistic fake data.

Verifies:
  - /nock slash commands:  which Notion tools are called for a given prompt
  - /nock #agent mode:     full agent loop, research + ask_user behaviour
  - /nock #feedback mode:  feedback tools (get/update_settings_context)
  - /nock built-ins:       help, refresh settings, system prompt
  - Events:                scoring → correct response type (silent / clarification / confirmation)
  - Ask-user flow:         agent pauses and asks a question when context is ambiguous
  - Multi-turn:            user reply routes back to agent via tool_result continuation
  - Pending-reply routing: pending reply intercepts next message instead of scoring
  - Interaction buttons:   confirm/dismiss buttons fire the correct actions

Run:  python test_e2e_bdd.py
      pytest test_e2e_bdd.py -v -s   # -s shows tool-call traces

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  HOW TO ADD A TEST CASE
  ──────────────────────
  Slash command — pick the right table and add one line:

    SC("label", "command text", mode, must_call=["tool"], must_not_call=["tool"], ask_user=False)

    mode:           "basic" | "agent" | "feedback"
    must_call:      tool names that MUST appear in tool_calls_made
    must_not_call:  tool names that must NOT appear
    ask_user:       True if the agent should pause and ask a clarifying question

  Event — pick the right table and add one line:

    EV("label", "slack message text", expected_action)

    expected_action: "silent" | "clarification" | "confirmation"
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import asyncio
import importlib.util
import json
import os
import sys
import threading
import types
import unittest
from collections import namedtuple
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# ---------------------------------------------------------------------------
# Load .env
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).parent
_dotenv_path = SCRIPT_DIR / ".env"
if _dotenv_path.exists():
    from dotenv import load_dotenv
    load_dotenv(_dotenv_path)

# ---------------------------------------------------------------------------
# Scenario types
# ---------------------------------------------------------------------------

SC = namedtuple(
    "SC",
    ["label", "text", "mode", "must_call", "must_not_call", "ask_user"],
    defaults=[[], [], False],
)
EV = namedtuple("EV", ["label", "text", "expected_action"])

# ---------------------------------------------------------------------------
# ✏️  EDIT THESE TABLES to add / tune test cases
# ---------------------------------------------------------------------------

# ── Slash / basic mode (/nock <text>, no flags) ───────────────────────────
# Light agent (max_iterations=8). Should call Notion tools to create/update.
SLASH_BASIC = [
    SC(
        "add new company",
        "Add Acme Corp as a new company",
        "basic",
        must_call=["create_page"],
    ),
    SC(
        "update existing contact",
        "Mark Sarah at GlobalTech as qualified — she's the decision maker",
        "basic",
        must_call=["create_page"],
    ),
    SC(
        "log meeting note",
        "Had intro call with James from Umbrella Corp about the Pro plan",
        "basic",
        must_call=["create_page"],
    ),
    SC(
        "multiple entities",
        "Add BigCo as a company and John Smith as their primary contact",
        "basic",
        must_call=["create_page"],
        must_not_call=["research"],   # no research needed for a simple add
    ),
    SC(
        "deal stage update",
        "Move the BigCo deal to Negotiation — they're pushing back on price",
        "basic",
        must_call=["create_page"],
    ),
]

# ── Slash / #agent mode (/nock <text> #agent) ─────────────────────────────
# Full agent loop. May use research, ask_user, chained tool sequences.
SLASH_AGENT = [
    SC(
        "research and add",
        "Research Acme Corp and add them as a company with their latest funding info",
        "agent",
        must_call=["research", "create_page"],
    ),
    SC(
        "no research for known entity",
        "Add John Smith from our last call as a Contact",
        "agent",
        must_call=["create_page"],
        must_not_call=["research"],
    ),
    SC(
        "ask when ambiguous contact",
        "Add the contact I spoke with today",
        "agent",
        ask_user=True,    # no name provided → agent must ask
    ),
    SC(
        "find and update",
        "Find the Acme Corp company record and update their status to Active",
        "agent",
        must_call=["get_database_pages", "update_page"],
    ),
    SC(
        "chained lookup and create",
        "Create a deal for Acme Corp in Negotiation stage worth $50k",
        "agent",
        must_call=["create_page"],
    ),
]

# ── Slash / #feedback mode (/nock <text> #feedback) ───────────────────────
# Must read then write Settings/Context — no Notion create/update allowed.
SLASH_FEEDBACK = [
    SC(
        "update context preference",
        "Always log deal amounts in EUR by default",
        "feedback",
        must_call=["get_settings_context", "update_settings_context"],
        must_not_call=["create_page", "update_page"],
    ),
    SC(
        "add field guidance",
        "When someone says 'signed', set deal status to Closed-Won",
        "feedback",
        must_call=["get_settings_context", "update_settings_context"],
        must_not_call=["create_page"],
    ),
]

# ── Events ────────────────────────────────────────────────────────────────
# Tests CRM relevance scoring → correct Slack action.
EVENTS_SILENT = [
    EV("lunch invite",     "Anyone up for lunch? New ramen place around the corner.",         "silent"),
    EV("ci failure",       "CI is red on main — integration tests timing out. @team",         "silent"),
    EV("standup reminder", "Reminder: daily standup in 10 minutes.",                          "silent"),
    EV("friday message",   "Happy Friday everyone! Great week, see you Monday 🎉",            "silent"),
]

EVENTS_CLARIFICATION = [
    EV("vague meeting",    "Meeting scheduled for Monday at 3pm with the new contact.",        "clarification"),
    EV("unnamed chat",     "Had a good chat with someone about the project — seemed keen.",    "clarification"),
    # Danish: "Maybe at some point in the future we should make it so a service user belongs
    # to the enterprise model in our pricing structure."
    # Pricing/product discussion — no named customer, but relevant enough that a human
    # sales rep would want to decide whether to log it.
    # NOTE: this test calls _score_crm_relevance without workspace context, so the generic scorer
    # may return < 0.4 (silent).  The full scenario with context is in TestEventClarifyAndLogFlow.
    EV(
        "danish pricing idea",
        "Måske vi skal et tidspunkt i fremtiden gøre sådan at servicebruger er noget som hører til entreprise modellen i vores prisstruktur",
        "clarification",
    ),
    # Danish: "An idea could be to add campaign-level overview to the relevant places, such as
    # Meta, Google Ads and Klaviyo…" — product idea without an explicit "log it" instruction.
    # With product/backlog context this should land in clarification (worth asking), not silent.
    # NOTE: full scenario with context is in TestProductBacklogFlow.
    EV(
        "danish campaign idea",
        "En ide kunne være at vi tilføjede kampagne niveau oversigt til de steder hvor det kan være relevant, "
        "såsom meta, google ads og klaviyo for at få en detaljeret gennemgang og tillade at AI'en kan optimere på det.",
        "clarification",
    ),
]

EVENTS_CONFIRMATION = [
    EV("contract signed",  "Great news — Acme Corp just signed the contract!",                "confirmation"),
    EV("demo booked",      "Booked a demo with TechCorp for Thursday 2pm.",                   "confirmation"),
    EV("new lead",         "New lead from the website — Marcus from Umbrella Corp.",          "confirmation"),
    EV("deal stage",       "Moved BigCo to Negotiation — pushing back on price.",             "confirmation"),
    # Danish: "An idea could be to add campaign-level overview to the relevant places, such as
    # Meta, Google Ads and Klaviyo to get a detailed review and allow the AI to optimise on it.
    # This should be written into our product backlog."
    # Explicit action request ("skal skrives ind i vores product backlog") — with product
    # backlog context the scorer should treat this as a confirmation.
    # NOTE: full scenario with context is in TestProductBacklogFlow.
    EV(
        "danish backlog idea",
        "En ide kunne være at vi tilføjede kampagne niveau oversigt til de steder hvor det kan være relevant, "
        "såsom meta, google ads og klaviyo for at få en detaljeret gennemgang og tillade at AI'en kan optimere "
        "på det. Det skal skrives ind i vores product backlog",
        "confirmation",
    ),
]

# ---------------------------------------------------------------------------
# Boilerplate — no need to edit when adding test cases
# ---------------------------------------------------------------------------

CRM_DIR = SCRIPT_DIR / "packages" / "notion-crm" / "crm"
sys.path.insert(0, str(CRM_DIR))

# ── Mock heavy dependencies before loading the CRM module ─────────────────
# NOTE: anthropic is NOT mocked here — live tests call the real Anthropic API.
# Unit-level tests mock the specific functions that call LLM directly.

mock_logger = types.ModuleType("crm_logger")
mock_logger.log_agent_run = MagicMock()
mock_logger.log_page_operation = MagicMock()
mock_logger.log_help_request = MagicMock()
mock_logger.log_system_prompt_request = MagicMock()
mock_logger.get_agent_conversation = MagicMock(return_value=None)
mock_logger.save_agent_conversation = MagicMock(return_value=None)
mock_logger.get_and_clear_pending_reply = MagicMock(return_value=None)
mock_logger.save_pending_reply = MagicMock()
mock_logger.get_notion_connection = MagicMock(
    return_value={"access_token": "mock-token", "settings_path": "/mock/settings"}
)
mock_logger.get_notion_access_token = MagicMock(return_value="mock-token")
sys.modules["crm_logger"] = mock_logger

mock_notion_mod = types.ModuleType("notion_client")
mock_notion_mod.NotionClient = MagicMock()
mock_notion_mod.close_http_client = AsyncMock()
sys.modules["notion_client"] = mock_notion_mod

# Load the CRM package
CRM_MAIN = CRM_DIR / "__main__.py"
spec = importlib.util.spec_from_file_location("crm_main", CRM_MAIN)
crm_main = importlib.util.module_from_spec(spec)
spec.loader.exec_module(crm_main)

# Submodules are loaded as side-effects of exec_module — import them directly
import agent as _agent_mod
import slack_events as _slack_events_mod
import slack_interactions as _slack_interactions_mod
import slack_slash as _slack_slash_mod
import notion_utils as _notion_utils_mod
from config import GENERIC_SYSTEM_PROMPT

# Convenience aliases
_score_crm_relevance = _slack_events_mod._score_crm_relevance
_handle_message_event = _slack_events_mod._handle_message_event
EVENT_CONFIDENCE_THRESHOLD = _slack_events_mod.EVENT_CONFIDENCE_THRESHOLD
EVENT_BORDERLINE_LOW = _slack_events_mod.EVENT_BORDERLINE_LOW
_run_slash_command = _slack_slash_mod._run_slash_command
_handle_slack_interaction = crm_main._handle_slack_interaction


# ---------------------------------------------------------------------------
# Fake Notion client with realistic responses
# ---------------------------------------------------------------------------

def _make_fake_notion() -> MagicMock:
    """Build a NotionClient mock that returns sensible fake data."""
    notion = MagicMock()

    notion.list_databases = AsyncMock(return_value=[
        {"id": "db-companies", "title": "Companies"},
        {"id": "db-contacts",  "title": "Contacts"},
        {"id": "db-deals",     "title": "Deals"},
        {"id": "db-notes",     "title": "Notes"},
    ])

    _db_schema = {
        "properties": {
            "Name":   {"type": "title"},
            "Status": {"type": "select", "select": {"options": [
                {"name": "Active"}, {"name": "Prospect"},
                {"name": "Negotiation"}, {"name": "Closed-Won"},
            ]}},
            "Amount": {"type": "number"},
            "Stage":  {"type": "select", "select": {"options": [
                {"name": "Researching"}, {"name": "Negotiation"}, {"name": "Closed-Won"},
            ]}},
        }
    }
    notion.get_database = AsyncMock(return_value=_db_schema)
    notion.query_database = AsyncMock(return_value=[])   # no existing pages by default
    notion._get_page_title = MagicMock(return_value="Acme Corp")

    notion.create_page = AsyncMock(return_value={
        "id": "page-abc123",
        "url": "https://notion.so/page-abc123",
        "properties": {"Name": {"title": [{"plain_text": "Acme Corp"}]}},
    })
    notion.update_page_properties = AsyncMock(return_value={
        "id": "page-abc123",
        "url": "https://notion.so/page-abc123",
    })
    notion.get_page = AsyncMock(return_value={
        "id": "page-abc123",
        "properties": {"Name": {"title": [{"plain_text": "Acme Corp"}]}},
    })
    notion.build_property_value = MagicMock(return_value={"type": "rich_text"})
    notion.find_page_by_path = AsyncMock(return_value="settings-page-id")
    notion.get_page_content_as_text = AsyncMock(return_value="Current CRM context: keep entries concise.")
    notion.replace_page_content = AsyncMock(return_value=None)

    return notion


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run(coro):
    """Run a coroutine synchronously."""
    return asyncio.run(coro)


needs_api_key = unittest.skipUnless(
    os.environ.get("ANTHROPIC_API_KEY"),
    "ANTHROPIC_API_KEY not set — skipping live e2e tests",
)


async def _run_slash_e2e(
    text: str,
    mode: str,
    ask_user_tool_use_id: str = "",
    extra_args: dict | None = None,
) -> dict:
    """
    Run a slash command end-to-end with a real LLM but mocked Notion.
    Returns the parsed body dict from _run_slash_command.

    Patching strategy: agent.py and slack_slash.py import functions with
    `from notion_utils import X`, so we must patch the local references in
    each module (not just the notion_utils module itself).
    """
    fake_notion = _make_fake_notion()
    _cn_mock = MagicMock(return_value=(fake_notion, "/mock/Settings"))
    _sp_mock = AsyncMock(return_value=GENERIC_SYSTEM_PROMPT)

    with patch.object(_agent_mod, "_get_notion_client_and_settings", _cn_mock), \
         patch.object(_slack_slash_mod, "_get_notion_client_and_settings", _cn_mock), \
         patch.object(_agent_mod, "_get_agent_system_prompt", _sp_mock), \
         patch.object(_slack_slash_mod, "_get_agent_system_prompt", _sp_mock), \
         patch.object(_slack_slash_mod, "_post_slash_result", new=AsyncMock()):

        if mode == "agent":
            cmd_text = f"{text} #agent"
        elif mode == "feedback":
            cmd_text = f"{text} #feedback"
        else:
            cmd_text = text

        args = {
            "text":         cmd_text,
            "response_url": "https://hooks.slack.com/fake/response",
            "team_id":      "T001",
            "user_id":      "U001",
            "channel_id":   "C001",
            "_ask_user_tool_use_id": ask_user_tool_use_id,
            **(extra_args or {}),
        }
        result = await _run_slash_command(args)

    return json.loads(result.get("body", "{}"))


def _tools_called(body: dict) -> list[str]:
    """Extract list of tool names called from the agent response body."""
    return [tc["tool"] for tc in body.get("tool_calls_made", [])]


class _SyncThread:
    """Drop-in for threading.Thread that runs target() synchronously (for tests)."""
    def __init__(self, target=None, daemon=False, **_):
        self._target = target

    def start(self):
        if self._target:
            self._target()

    def join(self, timeout=None):
        pass


# ---------------------------------------------------------------------------
# Slash command tests (live LLM, mocked Notion)
# ---------------------------------------------------------------------------

def _make_slash_test(sc: SC):
    async def _run():
        body = await _run_slash_e2e(sc.text, sc.mode)
        return body, _tools_called(body), body.get("ask_user", False)

    def test(self):
        body, tools, asked = run(_run())
        print(
            f"\n  [{sc.label}]  mode={sc.mode}  tools={tools}"
            f"  ask_user={asked}  response={body.get('response', '')[:80]}"
        )

        for tool in sc.must_call:
            self.assertIn(
                tool, tools,
                f"Expected tool '{tool}' to be called for [{sc.label!r}].\n"
                f"  Tools called: {tools}\n"
                f"  Response: {body.get('response', '')[:200]}",
            )

        for tool in sc.must_not_call:
            self.assertNotIn(
                tool, tools,
                f"Tool '{tool}' must NOT be called for [{sc.label!r}].\n"
                f"  Tools called: {tools}",
            )

        if sc.ask_user:
            self.assertTrue(
                asked,
                f"Expected agent to ask a clarifying question for [{sc.label!r}].\n"
                f"  ask_user={asked}\n  Response: {body.get('response', '')[:200]}",
            )

    test.__doc__ = f"[{sc.mode}] {sc.text[:90]}"
    return test


def _build_slash_class(name: str, doc: str, scenarios: list) -> type:
    methods = {"__doc__": doc}
    for sc in scenarios:
        key = "test_" + sc.label.lower().replace(" ", "_").replace("-", "_")
        methods[key] = _make_slash_test(sc)
    return needs_api_key(type(name, (unittest.TestCase,), methods))


TestSlashBasic = _build_slash_class(
    "TestSlashBasic",
    "Scenario: basic /nock commands call Notion create/update tools",
    SLASH_BASIC,
)

TestSlashAgent = _build_slash_class(
    "TestSlashAgent",
    "Scenario: #agent mode supports research, ask_user, and chained lookups",
    SLASH_AGENT,
)

TestSlashFeedback = _build_slash_class(
    "TestSlashFeedback",
    "Scenario: #feedback mode reads then writes Settings/Context only",
    SLASH_FEEDBACK,
)


# ---------------------------------------------------------------------------
# Slash built-in tests (no LLM — no API key required)
# ---------------------------------------------------------------------------

class TestSlashBuiltins(unittest.TestCase):
    """Scenario: /nock built-in subcommands return immediately without calling Claude."""

    def _run_builtin(self, text: str) -> dict:
        fake_notion = _make_fake_notion()
        with patch.object(_notion_utils_mod, "_get_notion_client_and_settings", return_value=(fake_notion, "/mock")), \
             patch.object(_notion_utils_mod, "_get_agent_system_prompt", new=AsyncMock(return_value="prompt")), \
             patch.object(_notion_utils_mod, "_run_refresh_settings", new=AsyncMock(return_value="Settings refreshed — 3 databases")), \
             patch.object(_slack_slash_mod, "_post_slash_result", new=AsyncMock()):
            args = {
                "text":         text,
                "response_url": "https://hooks.slack.com/fake/response",
                "team_id":      "T001",
                "user_id":      "U001",
                "channel_id":   "C001",
            }
            result = run(_run_slash_command(args))
        return json.loads(result.get("body", "{}"))

    def test_help(self):
        """/nock help returns help status without LLM call."""
        self.assertEqual(self._run_builtin("help").get("status"), "help")

    def test_help_question_mark(self):
        """/nock ? returns help status."""
        self.assertEqual(self._run_builtin("?").get("status"), "help")

    def test_help_dash_h(self):
        """/nock -h returns help status."""
        self.assertEqual(self._run_builtin("-h").get("status"), "help")

    def test_refresh_settings(self):
        """/nock refresh settings returns refresh_settings status."""
        self.assertEqual(self._run_builtin("refresh settings").get("status"), "refresh_settings")

    def test_system_prompt(self):
        """/nock system prompt returns system_prompt status."""
        self.assertEqual(self._run_builtin("system prompt").get("status"), "system_prompt")

    def test_show_system_prompt_alias(self):
        """/nock show system prompt is also a built-in."""
        self.assertEqual(self._run_builtin("show system prompt").get("status"), "system_prompt")

    def test_missing_text_returns_400(self):
        """/nock with no text returns a 400 error."""
        with patch.object(_notion_utils_mod, "_get_notion_client_and_settings", return_value=(_make_fake_notion(), "/mock")), \
             patch.object(_slack_slash_mod, "_post_slash_result", new=AsyncMock()):
            result = run(_run_slash_command({
                "text":         "",
                "response_url": "https://hooks.slack.com/fake/response",
                "team_id":      "T001",
                "user_id":      "U001",
            }))
        self.assertEqual(result.get("statusCode"), 400)

    def test_missing_response_url_returns_400(self):
        """/nock without a response_url and no _reply_channel returns 400."""
        result = run(_run_slash_command({
            "text":    "Add Acme Corp",
            "team_id": "T001",
            "user_id": "U001",
        }))
        self.assertEqual(result.get("statusCode"), 400)


# ---------------------------------------------------------------------------
# Event scoring + action tests (live LLM)
# ---------------------------------------------------------------------------

def _make_event_test(ev: EV):
    async def _run():
        score, reason, entities = await _score_crm_relevance(ev.text)
        if score < EVENT_BORDERLINE_LOW:
            action = "silent"
        elif score < EVENT_CONFIDENCE_THRESHOLD:
            action = "clarification"
        else:
            action = "confirmation"
        return score, reason, entities, action

    def test(self):
        score, reason, entities, action = run(_run())
        print(
            f"\n  [{ev.label}]  score={score:.2f}  expected={ev.expected_action}"
            f"  actual={action}  entities={entities}  reason='{reason}'"
        )
        self.assertEqual(
            action, ev.expected_action,
            f"[{ev.label!r}]  score={score:.2f}  expected={ev.expected_action!r}"
            f"  got={action!r}\n  reason: {reason}",
        )

    test.__doc__ = f"[{ev.expected_action}] {ev.text[:90]}"
    return test


def _build_event_class(name: str, doc: str, scenarios: list) -> type:
    methods = {"__doc__": doc}
    for ev in scenarios:
        key = "test_" + ev.label.lower().replace(" ", "_").replace("-", "_")
        methods[key] = _make_event_test(ev)
    return needs_api_key(type(name, (unittest.TestCase,), methods))


TestEventsSilent = _build_event_class(
    "TestEventsSilent",
    "Scenario: off-topic messages score < EVENT_BORDERLINE_LOW → no Slack message",
    EVENTS_SILENT,
)

TestEventsClarification = _build_event_class(
    "TestEventsClarification",
    "Scenario: ambiguous messages score in [BORDERLINE_LOW, THRESHOLD) → clarification question",
    EVENTS_CLARIFICATION,
)

TestEventsConfirmation = _build_event_class(
    "TestEventsConfirmation",
    "Scenario: clearly CRM-relevant messages score >= THRESHOLD → confirmation prompt",
    EVENTS_CONFIRMATION,
)


# ---------------------------------------------------------------------------
# Ask-user flow tests (live LLM)
# ---------------------------------------------------------------------------

@needs_api_key
class TestAskUserFlow(unittest.TestCase):
    """
    Scenario: when context is ambiguous the agent calls ask_user, pending_reply is saved,
    and the user's follow-up reply continues the agent to completion.
    """

    def _slash(self, text: str, mode: str = "agent", tool_use_id: str = "") -> dict:
        return run(_run_slash_e2e(text, mode, ask_user_tool_use_id=tool_use_id))

    def test_ambiguous_command_triggers_ask_user(self):
        """An ambiguous command without a contact name should make the agent ask."""
        body = self._slash("Add the person from today's meeting")
        print(f"\n  [ask_user trigger]  ask_user={body.get('ask_user')}  response={body.get('response','')[:80]}")
        self.assertTrue(
            body.get("ask_user", False),
            f"Expected ask_user=True for ambiguous prompt.\n"
            f"  Response: {body.get('response','')[:200]}",
        )
        self.assertIn("ask_user_tool_use_id", body)
        self.assertTrue(body["ask_user_tool_use_id"], "ask_user_tool_use_id must be non-empty")

    def test_ask_user_saves_pending_reply(self):
        """When ask_user fires, save_pending_reply must be called with the right channel."""
        mock_logger.save_pending_reply.reset_mock()
        body = self._slash("Add the person from today's meeting")
        if not body.get("ask_user"):
            self.skipTest("First turn didn't trigger ask_user")

        mock_logger.save_pending_reply.assert_called_once()
        kwargs = mock_logger.save_pending_reply.call_args.kwargs
        self.assertEqual(kwargs.get("channel_id", ""), "C001")

    def test_reply_continues_agent_to_create_page(self):
        """
        After an ask_user pause, supplying a _ask_user_tool_use_id continues
        the agent — it should now call create_page without asking again.
        """
        body1 = self._slash("Add the person from today's meeting")
        if not body1.get("ask_user"):
            self.skipTest("First turn didn't trigger ask_user — skip continuation test")

        tool_use_id = body1.get("ask_user_tool_use_id", "fake-id")

        # Second turn: user replies with the missing context
        body2 = self._slash(
            "Her name is Alice Johnson from Initech",
            mode="agent",
            tool_use_id=tool_use_id,
        )
        tools = _tools_called(body2)
        print(
            f"\n  [ask_user reply]  ask_user={body2.get('ask_user')}"
            f"  tools={tools}  response={body2.get('response','')[:80]}"
        )

        self.assertFalse(
            body2.get("ask_user", False),
            "Agent should not ask again after receiving the user's reply",
        )
        self.assertIn(
            "create_page", tools,
            f"Agent should call create_page after receiving the contact name.\n  Tools: {tools}",
        )

    def test_non_ambiguous_command_does_not_ask(self):
        """A fully specified command should NOT trigger ask_user."""
        body = self._slash("Add Alice Johnson from Initech as a Contact")
        self.assertFalse(
            body.get("ask_user", False),
            f"A fully specified command should not ask.\n  Response: {body.get('response','')[:200]}",
        )
        self.assertIn("create_page", _tools_called(body))


# ---------------------------------------------------------------------------
# Pending-reply routing tests (unit-level — no API key required)
# ---------------------------------------------------------------------------

class TestPendingReplyRouting(unittest.TestCase):
    """
    Scenario: a pending_reply in the store routes the next message from the same
    user/channel back to the agent as a tool_result continuation instead of
    going through CRM relevance scoring.
    """

    @patch.object(_slack_events_mod, "_score_crm_relevance", new_callable=AsyncMock)
    @patch.object(_slack_events_mod, "_run_slash_command", new_callable=AsyncMock)
    def test_pending_reply_routes_to_agent(self, mock_slash, mock_score):
        """If pending_reply exists, the message goes to the agent, not the scorer."""
        # get_and_clear_pending_reply returns a tuple (channel_id, tool_use_id)
        mock_logger.get_and_clear_pending_reply.return_value = ("C001", "tu-123")
        mock_slash.return_value = {"statusCode": 200, "body": '{"response":"done"}'}

        event = {"text": "Alice Johnson", "channel": "C001", "ts": "1.0", "user": "U001"}
        run(_handle_message_event(event, team_id="T001"))

        mock_score.assert_not_called()
        mock_slash.assert_called_once()
        call_args = mock_slash.call_args[0][0]
        self.assertIn("Alice Johnson", call_args.get("text", ""))
        self.assertEqual(call_args.get("_ask_user_tool_use_id", ""), "tu-123")

    @patch.object(_slack_events_mod, "_score_crm_relevance", new_callable=AsyncMock)
    def test_no_pending_reply_falls_through_to_scorer(self, mock_score):
        """If no pending_reply, the message is scored for CRM relevance normally."""
        mock_logger.get_and_clear_pending_reply.return_value = None
        mock_score.return_value = (0.1, "not relevant", [])

        event = {"text": "Happy Friday!", "channel": "C001", "ts": "1.0"}
        run(_handle_message_event(event, team_id="T001"))

        mock_score.assert_called_once()
        self.assertEqual(mock_score.call_args[0][0], "Happy Friday!")

    @patch.object(_slack_events_mod, "_score_crm_relevance", new_callable=AsyncMock)
    @patch.object(_slack_events_mod, "_run_slash_command", new_callable=AsyncMock)
    def test_pending_reply_wrong_channel_does_not_intercept(self, mock_slash, mock_score):
        """A pending_reply for a different channel must not intercept the current message."""
        # get_and_clear_pending_reply returns (channel_id, tool_use_id)
        mock_logger.get_and_clear_pending_reply.return_value = ("C999", "tu-456")
        mock_score.return_value = (0.1, "not relevant", [])

        event = {"text": "Some message", "channel": "C001", "ts": "1.0"}
        run(_handle_message_event(event, team_id="T001"))

        mock_slash.assert_not_called()
        mock_score.assert_called_once()

    @patch.object(_slack_events_mod, "_score_crm_relevance", new_callable=AsyncMock)
    def test_bot_message_skips_scoring_and_routing(self, mock_score):
        """Bot messages are dropped before any routing or scoring."""
        mock_logger.get_and_clear_pending_reply.return_value = None

        event = {"bot_id": "B001", "text": "Acme signed!", "channel": "C001", "ts": "1.0"}
        run(_handle_message_event(event, team_id="T001"))

        mock_score.assert_not_called()

    @patch.object(_slack_events_mod, "_score_crm_relevance", new_callable=AsyncMock)
    def test_message_subtype_skips_scoring(self, mock_score):
        """Edited/deleted message subtypes are ignored entirely."""
        mock_logger.get_and_clear_pending_reply.return_value = None

        event = {"subtype": "message_changed", "text": "Updated", "channel": "C001", "ts": "1.0"}
        run(_handle_message_event(event, team_id="T001"))

        mock_score.assert_not_called()


# ---------------------------------------------------------------------------
# Interaction button tests (unit-level — no API key required)
# ---------------------------------------------------------------------------

class TestInteractionButtons(unittest.TestCase):
    """
    Scenario: clicking confirmation/clarification buttons fires the correct
    downstream action — confirm/yes → slash command; dismiss/no → delete message.
    """

    def _run_interaction(self, action_id: str, msg_text: str = "Acme signed the deal") -> None:
        """Build a block_actions payload and run the interaction handler."""
        payload = {
            "type":    "block_actions",
            "actions": [{"action_id": action_id, "block_id": "b1", "value": msg_text}],
            "message": {"text": msg_text, "ts": "1700000001.000001"},
            "container": {"channel_id": "C001", "message_ts": "1700000002.000001"},
            "channel":   {"id": "C001"},
            "response_url": "https://hooks.slack.com/actions/fake",
            "team":  {"id": "T001"},
            "user":  {"id": "U001"},
        }
        args = {"_interaction_payload": payload}

        # Clear async-invoke env vars so the handler takes the local thread path,
        # then use _SyncThread so the background thread executes synchronously.
        with patch.dict(os.environ, {"DO_SLACK_ASYNC_URL": "", "DO_SLACK_ASYNC_TOKEN": ""}), \
             patch("slack_interactions.threading.Thread", _SyncThread), \
             patch("slack_interactions.httpx.Client") as mock_http:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {"ok": True}
            mock_http.return_value.__enter__.return_value.post = MagicMock(return_value=mock_resp)
            mock_http.return_value.__enter__.return_value.get = MagicMock(return_value=mock_resp)
            run(_handle_slack_interaction(args))

    @patch.object(_slack_interactions_mod, "_run_interaction_worker", new_callable=AsyncMock)
    def test_confirm_log_runs_interaction_worker(self, mock_worker):
        """'Log it' must trigger _run_interaction_worker with the original message text."""
        mock_worker.return_value = {"statusCode": 200, "body": '{"response":"done"}'}
        self._run_interaction("event_confirm_log", "Acme signed the deal")
        mock_worker.assert_called_once()
        call_text = mock_worker.call_args[0][0].get("text", "")
        self.assertIn("Acme", call_text)

    @patch.object(_slack_interactions_mod, "_run_interaction_worker", new_callable=AsyncMock)
    def test_dismiss_does_not_run_worker(self, mock_worker):
        """'Dismiss' must NOT trigger _run_interaction_worker."""
        self._run_interaction("event_dismiss", "Some message")
        mock_worker.assert_not_called()

    @patch.object(_slack_interactions_mod, "_run_interaction_worker", new_callable=AsyncMock)
    def test_clarify_yes_runs_interaction_worker(self, mock_worker):
        """'Yes, log it' clarification must trigger _run_interaction_worker."""
        mock_worker.return_value = {"statusCode": 200, "body": '{"response":"done"}'}
        self._run_interaction("event_clarify_yes", "Had a call with someone")
        mock_worker.assert_called_once()

    @patch.object(_slack_interactions_mod, "_run_interaction_worker", new_callable=AsyncMock)
    def test_clarify_no_does_not_run_worker(self, mock_worker):
        """'No thanks' clarification must NOT trigger _run_interaction_worker."""
        self._run_interaction("event_clarify_no", "Some message")
        mock_worker.assert_not_called()

    @patch.object(_slack_interactions_mod, "_run_interaction_worker", new_callable=AsyncMock)
    def test_confirm_passes_message_text_to_slash(self, mock_worker):
        """The worker receives the original message text as the slash command prompt."""
        mock_worker.return_value = {"statusCode": 200, "body": '{"response":"done"}'}
        self._run_interaction("event_confirm_log", "TechCorp signed the enterprise deal")
        mock_worker.assert_called_once()
        worker_args = mock_worker.call_args[0][0]
        self.assertIn("TechCorp", worker_args.get("text", ""))


# ---------------------------------------------------------------------------
# Event → clarification → confirm → page created (full end-to-end flow)
# ---------------------------------------------------------------------------

_DANISH_PRICING_MSG = (
    "Måske vi skal et tidspunkt i fremtiden gøre sådan at servicebruger er noget "
    "som hører til entreprise modellen i vores prisstruktur"
)

_DANISH_BACKLOG_MSG = (
    "En ide kunne være at vi tilføjede kampagne niveau oversigt til de steder hvor det kan være relevant, "
    "såsom meta, google ads og klaviyo for at få en detaljeret gennemgang og tillade at AI'en kan optimere "
    "på det. Det skal skrives ind i vores product backlog"
)

# Same idea but without the explicit "write it to the product backlog" instruction.
_DANISH_CAMPAIGN_IDEA_MSG = (
    "En ide kunne være at vi tilføjede kampagne niveau oversigt til de steder hvor det kan være relevant, "
    "såsom meta, google ads og klaviyo for at få en detaljeret gennemgang og tillade at AI'en kan optimere på det."
)


def _setup_notion_context_mock(databases: list, settings_content: str = "") -> None:
    """
    Configure the global NotionClient mock so that _load_workspace_context will
    return a context string built from `databases` and `settings_content`.

    This replicates what happens in prod when the Notion token is valid and the
    workspace has a Settings/Context page.
    """
    inst = mock_notion_mod.NotionClient.return_value
    inst.list_databases = AsyncMock(return_value=databases)
    inst.find_page_by_path = AsyncMock(
        return_value="settings-page-id" if settings_content else None
    )
    inst.get_page_content_as_text = AsyncMock(return_value=settings_content)


async def _load_context_via_prod_path(
    databases: list, settings_content: str = ""
) -> str:
    """
    Call the real _load_workspace_context with a mocked Notion client —
    the same code path that runs in production on every incoming Slack message.
    """
    _slack_events_mod._workspace_context_cache.clear()
    mock_logger.get_notion_connection.return_value = {
        "access_token": "tok-test",
        "settings_path": "/mock/CRM",
    }
    _setup_notion_context_mock(databases, settings_content)
    return await _slack_events_mod._load_workspace_context("T001")


@needs_api_key
class TestEventClarifyAndLogFlow(unittest.TestCase):
    """
    Scenario: A vague/borderline message arrives in Slack, triggers a clarification
    question (not a silent drop, not an immediate confirmation), the user clicks
    "Yes, log it", and a CRM page is created.

    Full flow:
      1. Message scores in [BORDERLINE_LOW, THRESHOLD) → clarification
      2. User clicks event_clarify_yes → _run_interaction_worker fires
      3. Slash command runs → create_page is called
    """

    # ── Step 1: scoring ──────────────────────────────────────────────────

    def test_danish_pricing_message_reacts_with_product_context(self):
        """
        With workspace context that registers product/pricing databases, the scorer
        must not silently drop this message (score must be ≥ EVENT_BORDERLINE_LOW).

        Whether it lands in the clarification or confirmation band depends on how
        prominently the workspace context mentions pricing — both are valid "react"
        outcomes.  The key assertion is: the message is NOT silent.

        Context is loaded via the real _load_workspace_context code path (mocked Notion),
        the same way it runs in production on every incoming Slack message.
        """
        context = run(_load_context_via_prod_path(
            databases=[
                {"id": "db-1", "title": "Companies"},
                {"id": "db-2", "title": "Product Ideas"},
                {"id": "db-3", "title": "Pricing Tiers"},
            ],
            settings_content=(
                "Log pricing and product discussions as Notes or Product Ideas.\n"
                "Track enterprise customers and service models in the Companies database."
            ),
        ))
        score, reason, entities = run(
            _score_crm_relevance(_DANISH_PRICING_MSG, context)
        )
        print(
            f"\n  [danish pricing + context]  score={score:.2f}  entities={entities}"
            f"  reason='{reason}'"
        )
        self.assertGreaterEqual(
            score, EVENT_BORDERLINE_LOW,
            f"score {score:.2f} is below the borderline floor — message was silently dropped "
            f"even though workspace context includes product/pricing databases.\n  reason: {reason}",
        )

    def test_danish_pricing_message_is_silent_without_context(self):
        """
        Without workspace context (a generic CRM workspace with no product databases),
        the same message should score below EVENT_BORDERLINE_LOW and be silently dropped.
        The scorer has no basis to consider pricing ideas as relevant.
        """
        score, reason, entities = run(_score_crm_relevance(_DANISH_PRICING_MSG))
        print(
            f"\n  [danish pricing, no context]  score={score:.2f}  entities={entities}"
            f"  reason='{reason}'"
        )
        self.assertLess(
            score, EVENT_BORDERLINE_LOW,
            f"score {score:.2f} is ≥ borderline floor — message was not silently dropped even "
            f"without workspace context.\n  reason: {reason}",
        )

    # ── Step 2: clarification question is posted ─────────────────────────

    @patch.object(_slack_events_mod, "_post_event_confirmation", new_callable=AsyncMock)
    @patch.object(_slack_events_mod, "_post_clarification_question", new_callable=AsyncMock)
    @patch.object(_slack_events_mod, "_score_crm_relevance", new_callable=AsyncMock)
    @patch.dict(os.environ, {"SLACK_BOT_TOKEN": "xoxb-test-token"})
    def test_danish_pricing_message_posts_clarification_not_confirmation(
        self, mock_score, mock_clarify, mock_confirm
    ):
        """
        Given a score in the clarification band, the event handler must call
        _post_clarification_question, not _post_event_confirmation.

        The score is mocked here — we are testing routing behaviour, not the
        scorer.  The scorer's actual behaviour for this message is covered by
        test_danish_pricing_message_scores_in_clarification_band.
        """
        mock_score.return_value = (
            (EVENT_BORDERLINE_LOW + EVENT_CONFIDENCE_THRESHOLD) / 2,  # mid-band
            "Pricing/product discussion — worth asking",
            [],
        )

        run(_handle_message_event(
            {"text": _DANISH_PRICING_MSG, "channel": "C001", "ts": "1.0"},
            team_id="T001",
        ))

        mock_clarify.assert_called_once()
        mock_confirm.assert_not_called()

        # Clarification was posted to the right channel and thread
        call_kwargs = mock_clarify.call_args.kwargs
        self.assertEqual(call_kwargs.get("channel"), "C001")
        self.assertEqual(call_kwargs.get("thread_ts"), "1.0")

    # ── Step 3: user clicks "Yes, log it" → interaction worker fires ────

    @patch.object(_slack_interactions_mod, "_run_interaction_worker", new_callable=AsyncMock)
    def test_confirm_button_fires_interaction_worker_with_original_text(self, mock_worker):
        """
        When the user clicks 'Yes, log it' on the clarification prompt, the
        interaction handler must invoke _run_interaction_worker with the
        original Danish message as the text.  Whether the agent then creates a
        page or asks for more context depends on the CRM system prompt — that
        behaviour is covered by test_confirm_creates_page_when_asked_to_log.
        """
        mock_worker.return_value = {"statusCode": 200, "body": '{"response": "done"}'}

        payload = {
            "type":    "block_actions",
            "actions": [{"action_id": "event_clarify_yes", "block_id": "b1", "value": _DANISH_PRICING_MSG}],
            "message": {"text": _DANISH_PRICING_MSG, "ts": "1700000001.000001"},
            "container": {"channel_id": "C001", "message_ts": "1700000002.000001"},
            "channel":   {"id": "C001"},
            "response_url": "https://hooks.slack.com/actions/fake",
            "team":  {"id": "T001"},
            "user":  {"id": "U001"},
        }
        with patch.dict(os.environ, {"DO_SLACK_ASYNC_URL": "", "DO_SLACK_ASYNC_TOKEN": ""}), \
             patch("slack_interactions.threading.Thread", _SyncThread), \
             patch("slack_interactions.httpx.Client") as mock_http:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {"ok": True}
            mock_http.return_value.__enter__.return_value.post = MagicMock(return_value=mock_resp)
            mock_http.return_value.__enter__.return_value.get = MagicMock(return_value=mock_resp)
            run(_handle_slack_interaction({"_interaction_payload": payload}))

        mock_worker.assert_called_once()
        worker_text = mock_worker.call_args[0][0].get("text", "")
        self.assertIn("servicebruger", worker_text)
        self.assertIn("entreprise", worker_text)

    def test_confirm_creates_page_when_asked_to_log(self):
        """
        When the user confirms AND the slash command is phrased as an explicit
        log request ("Log this idea: …"), the agent must call create_page.

        This is the intended end-state after a clarification flow: the message
        text arrives at the agent with enough context to take action.

        NOTE: for the raw Danish message without a "log this" prefix the agent
        responds conversationally, which means the system prompt should be
        updated to instruct the agent to create a Note record for any confirmed
        pricing/product ideas. Until then, this test uses an explicit prefix.
        """
        explicit_prompt = f"Log this product idea as a Note: {_DANISH_PRICING_MSG}"
        body = run(_run_slash_e2e(explicit_prompt, "basic"))
        tools = _tools_called(body)
        asked = body.get("ask_user", False)
        print(
            f"\n  [log explicit]  tools={tools}  ask_user={asked}"
            f"  response={body.get('response', '')[:80]}"
        )

        self.assertTrue(
            "create_page" in tools or asked,
            f"Expected create_page or ask_user for an explicit log request.\n"
            f"  Tools called: {tools}\n"
            f"  ask_user: {asked}\n"
            f"  Response: {body.get('response', '')[:300]}",
        )


# ---------------------------------------------------------------------------
# Product backlog message → confirmation → page created (full end-to-end flow)
# ---------------------------------------------------------------------------

@needs_api_key
class TestProductBacklogFlow(unittest.TestCase):
    """
    Scenario: A Danish message explicitly requests that an idea be written into
    the product backlog ("Det skal skrives ind i vores product backlog").

    Unlike the vague pricing idea, this message contains an explicit action verb,
    so with workspace context that includes a product backlog database it should
    score in the confirmation band (not clarification, not silent).

    Full flow:
      1. Message scores ≥ EVENT_CONFIDENCE_THRESHOLD with product backlog context
      2. Message scores < EVENT_BORDERLINE_LOW without context (generic CRM workspace)
      3. With score in confirmation band, event handler posts a confirmation (not clarification)
      4. Agent creates a page in the Product Backlog database when asked to log it
    """

    # ── Step 1: scoring with product backlog context ──────────────────────

    def test_backlog_message_scores_confirmation_with_context(self):
        """
        With workspace context that includes a Product Backlog database, the scorer
        must return score ≥ EVENT_CONFIDENCE_THRESHOLD — i.e. a confirmation, not
        just a clarification question.

        The message contains an explicit instruction ("skal skrives ind i vores
        product backlog"), which a sales/product rep would treat as a direct action.
        """
        context = run(_load_context_via_prod_path(
            databases=[
                {"id": "db-1", "title": "Companies"},
                {"id": "db-2", "title": "Product Backlog"},
                {"id": "db-3", "title": "Product Ideas"},
            ],
            settings_content=(
                "Log product ideas and backlog items in the Product Backlog database.\n"
                "Track campaign integrations (Meta, Google Ads, Klaviyo) as Product Backlog entries."
            ),
        ))
        score, reason, entities = run(
            _score_crm_relevance(_DANISH_BACKLOG_MSG, context)
        )
        print(
            f"\n  [backlog + context]  score={score:.2f}  entities={entities}"
            f"  reason='{reason}'"
        )
        self.assertGreaterEqual(
            score, EVENT_CONFIDENCE_THRESHOLD,
            f"score {score:.2f} is below the confirmation threshold — message was not treated "
            f"as an explicit action even though workspace context includes Product Backlog.\n"
            f"  reason: {reason}",
        )

    def test_backlog_message_is_silent_without_context(self):
        """
        Without workspace context, the scorer has no basis to treat product backlog
        ideas as CRM-relevant. The message should be silently dropped.
        """
        score, reason, entities = run(_score_crm_relevance(_DANISH_BACKLOG_MSG))
        print(
            f"\n  [backlog, no context]  score={score:.2f}  entities={entities}"
            f"  reason='{reason}'"
        )
        self.assertLess(
            score, EVENT_BORDERLINE_LOW,
            f"score {score:.2f} is ≥ borderline floor — message was not silently dropped even "
            f"without workspace context.\n  reason: {reason}",
        )

    # ── Step 2: confirmation posted (not clarification) ───────────────────

    @patch.object(_slack_events_mod, "_post_event_confirmation", new_callable=AsyncMock)
    @patch.object(_slack_events_mod, "_post_clarification_question", new_callable=AsyncMock)
    @patch.object(_slack_events_mod, "_score_crm_relevance", new_callable=AsyncMock)
    @patch.dict(os.environ, {"SLACK_BOT_TOKEN": "xoxb-test-token"})
    def test_backlog_message_posts_confirmation_not_clarification(
        self, mock_score, mock_clarify, mock_confirm
    ):
        """
        Given a score above EVENT_CONFIDENCE_THRESHOLD, the event handler must call
        _post_event_confirmation, not _post_clarification_question.
        """
        mock_score.return_value = (
            EVENT_CONFIDENCE_THRESHOLD + 0.05,
            "Explicit product backlog action request",
            [],
        )

        run(_handle_message_event(
            {"text": _DANISH_BACKLOG_MSG, "channel": "C001", "ts": "2.0"},
            team_id="T001",
        ))

        mock_confirm.assert_called_once()
        mock_clarify.assert_not_called()

        call_kwargs = mock_confirm.call_args.kwargs
        self.assertEqual(call_kwargs.get("channel"), "C001")
        self.assertEqual(call_kwargs.get("thread_ts"), "2.0")

    # ── Step 3: user confirms → interaction worker fires with original text ──

    @patch.object(_slack_interactions_mod, "_run_interaction_worker", new_callable=AsyncMock)
    def test_confirm_button_fires_worker_with_original_backlog_text(self, mock_worker):
        """
        When the user clicks 'Yes, log it' on the confirmation prompt, the interaction
        handler must invoke _run_interaction_worker with the original Danish message.
        """
        mock_worker.return_value = {"statusCode": 200, "body": '{"response": "done"}'}

        payload = {
            "type":    "block_actions",
            "actions": [{"action_id": "event_confirm_log", "block_id": "b1", "value": _DANISH_BACKLOG_MSG}],
            "message": {"text": _DANISH_BACKLOG_MSG, "ts": "1700000003.000001"},
            "container": {"channel_id": "C001", "message_ts": "1700000004.000001"},
            "channel":   {"id": "C001"},
            "response_url": "https://hooks.slack.com/actions/fake",
            "team":  {"id": "T001"},
            "user":  {"id": "U001"},
        }
        with patch.dict(os.environ, {"DO_SLACK_ASYNC_URL": "", "DO_SLACK_ASYNC_TOKEN": ""}), \
             patch("slack_interactions.threading.Thread", _SyncThread), \
             patch("slack_interactions.httpx.Client") as mock_http:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {"ok": True}
            mock_http.return_value.__enter__.return_value.post = MagicMock(return_value=mock_resp)
            mock_http.return_value.__enter__.return_value.get = MagicMock(return_value=mock_resp)
            run(_handle_slack_interaction({"_interaction_payload": payload}))

        mock_worker.assert_called_once()
        worker_text = mock_worker.call_args[0][0].get("text", "")
        self.assertIn("product backlog", worker_text.lower())
        self.assertIn("klaviyo", worker_text.lower())

    # ── Vague variant (no explicit "log it" instruction) ──────────────────

    def test_campaign_idea_reacts_with_product_context(self):
        """
        The same idea without the explicit backlog instruction must still NOT be
        silently dropped when workspace context includes a Product Backlog database.
        It should score ≥ EVENT_BORDERLINE_LOW (clarification or confirmation band).
        """
        context = run(_load_context_via_prod_path(
            databases=[
                {"id": "db-1", "title": "Companies"},
                {"id": "db-2", "title": "Product Backlog"},
            ],
            settings_content=(
                "Log product ideas and backlog items in the Product Backlog database.\n"
                "Track campaign integrations (Meta, Google Ads, Klaviyo) as Product Backlog entries."
            ),
        ))
        score, reason, entities = run(
            _score_crm_relevance(_DANISH_CAMPAIGN_IDEA_MSG, context)
        )
        print(
            f"\n  [campaign idea + context]  score={score:.2f}  entities={entities}"
            f"  reason='{reason}'"
        )
        self.assertGreaterEqual(
            score, EVENT_BORDERLINE_LOW,
            f"score {score:.2f} is below the borderline floor — vague campaign idea was silently "
            f"dropped even though workspace context includes Product Backlog.\n  reason: {reason}",
        )

    def test_campaign_idea_is_silent_without_context(self):
        """
        Without workspace context the vague campaign idea should be silently dropped —
        no named customer, no deal signal, no explicit action request.
        """
        score, reason, entities = run(_score_crm_relevance(_DANISH_CAMPAIGN_IDEA_MSG))
        print(
            f"\n  [campaign idea, no context]  score={score:.2f}  entities={entities}"
            f"  reason='{reason}'"
        )
        self.assertLess(
            score, EVENT_BORDERLINE_LOW,
            f"score {score:.2f} is ≥ borderline floor — vague campaign idea was not silently "
            f"dropped without workspace context.\n  reason: {reason}",
        )

    @patch.object(_slack_events_mod, "_post_event_confirmation", new_callable=AsyncMock)
    @patch.object(_slack_events_mod, "_post_clarification_question", new_callable=AsyncMock)
    @patch.object(_slack_events_mod, "_score_crm_relevance", new_callable=AsyncMock)
    @patch.dict(os.environ, {"SLACK_BOT_TOKEN": "xoxb-test-token"})
    def test_campaign_idea_posts_clarification_not_confirmation(
        self, mock_score, mock_clarify, mock_confirm
    ):
        """
        Given a score in the clarification band (no explicit action verb), the
        event handler must ask a question rather than auto-confirm.
        """
        mock_score.return_value = (
            (EVENT_BORDERLINE_LOW + EVENT_CONFIDENCE_THRESHOLD) / 2,
            "Product idea — worth asking whether to log",
            [],
        )

        run(_handle_message_event(
            {"text": _DANISH_CAMPAIGN_IDEA_MSG, "channel": "C001", "ts": "3.0"},
            team_id="T001",
        ))

        mock_clarify.assert_called_once()
        mock_confirm.assert_not_called()

        call_kwargs = mock_clarify.call_args.kwargs
        self.assertEqual(call_kwargs.get("channel"), "C001")
        self.assertEqual(call_kwargs.get("thread_ts"), "3.0")

    # ── Step 4: agent creates a page in Product Backlog ───────────────────

    def test_explicit_backlog_request_creates_page(self):
        """
        When the slash command explicitly asks to log the idea and the workspace
        has a Product Backlog database, the agent must call create_page.
        """
        explicit_prompt = f"Log this in our Product Backlog: {_DANISH_BACKLOG_MSG}"

        # Build a notion mock that includes the Product Backlog database so the
        # agent can find it and create a page without needing to ask_user.
        fake_notion = _make_fake_notion()
        fake_notion.list_databases = AsyncMock(return_value=[
            {"id": "db-companies",      "title": "Companies"},
            {"id": "db-contacts",       "title": "Contacts"},
            {"id": "db-product-backlog","title": "Product Backlog"},
        ])

        _cn_mock = MagicMock(return_value=(fake_notion, "/mock/Settings"))
        _sp_mock = AsyncMock(return_value=GENERIC_SYSTEM_PROMPT)

        with patch.object(_agent_mod, "_get_notion_client_and_settings", _cn_mock), \
             patch.object(_slack_slash_mod, "_get_notion_client_and_settings", _cn_mock), \
             patch.object(_agent_mod, "_get_agent_system_prompt", _sp_mock), \
             patch.object(_slack_slash_mod, "_get_agent_system_prompt", _sp_mock), \
             patch.object(_slack_slash_mod, "_post_slash_result", new=AsyncMock()):
            args = {
                "text": explicit_prompt,
                "response_url": "https://hooks.slack.com/fake/response",
                "team_id": "T001",
                "user_id": "U001",
                "channel_id": "C001",
            }
            result = run(_run_slash_command(args))

        body = json.loads(result.get("body", "{}"))
        tools = _tools_called(body)
        asked = body.get("ask_user", False)
        print(
            f"\n  [backlog explicit]  tools={tools}  ask_user={asked}"
            f"  response={body.get('response', '')[:80]}"
        )

        self.assertIn(
            "create_page", tools,
            f"Expected create_page for an explicit backlog log request with Product Backlog DB present.\n"
            f"  Tools called: {tools}\n"
            f"  ask_user: {asked}\n"
            f"  Response: {body.get('response', '')[:300]}",
        )


# ---------------------------------------------------------------------------
# Unit tests for _load_workspace_context (no API key required)
# ---------------------------------------------------------------------------

class TestWorkspaceContextLoading(unittest.TestCase):
    """
    Unit tests for _load_workspace_context — no Anthropic API key needed.

    These tests call the real function with a mocked Notion client and verify
    the format and content of the returned context string, plus caching behaviour.
    """

    def setUp(self):
        # Clear the TTL cache before every test so tests are independent
        _slack_events_mod._workspace_context_cache.clear()
        # Ensure get_notion_connection returns a valid connection
        mock_logger.get_notion_connection.return_value = {
            "access_token": "tok-unit",
            "settings_path": "/CRM/Settings/Context",
        }

    def tearDown(self):
        _slack_events_mod._workspace_context_cache.clear()

    def test_includes_database_names(self):
        """Returned context must mention the database names."""
        _setup_notion_context_mock(
            databases=[
                {"id": "db-1", "title": "Companies"},
                {"id": "db-2", "title": "Deals"},
            ],
        )
        ctx = run(_slack_events_mod._load_workspace_context("T-unit"))
        self.assertIn("Companies", ctx)
        self.assertIn("Deals", ctx)

    def test_includes_settings_context_content(self):
        """Returned context must include text from the Settings/Context page."""
        _setup_notion_context_mock(
            databases=[{"id": "db-1", "title": "Companies"}],
            settings_content="Track pricing discussions as Product Ideas.",
        )
        ctx = run(_slack_events_mod._load_workspace_context("T-unit"))
        self.assertIn("Track pricing discussions", ctx)

    def test_empty_when_no_connection(self):
        """Returns empty string when no Notion connection exists for the team."""
        mock_logger.get_notion_connection.return_value = None
        _setup_notion_context_mock(databases=[], settings_content="")
        ctx = run(_slack_events_mod._load_workspace_context("T-no-connection"))
        self.assertEqual(ctx, "")

    def test_result_is_cached(self):
        """Second call with the same team_id should not hit Notion again."""
        _setup_notion_context_mock(
            databases=[{"id": "db-1", "title": "Companies"}],
        )
        inst = mock_notion_mod.NotionClient.return_value

        run(_slack_events_mod._load_workspace_context("T-cache"))
        run(_slack_events_mod._load_workspace_context("T-cache"))

        # list_databases should only have been called once despite two context loads
        self.assertEqual(inst.list_databases.call_count, 1)

    def test_cache_is_per_team(self):
        """Different team IDs get different cache entries."""
        _setup_notion_context_mock(
            databases=[{"id": "db-1", "title": "Companies"}],
        )
        ctx1 = run(_slack_events_mod._load_workspace_context("T-alpha"))
        # Change what Notion returns before the second call
        _setup_notion_context_mock(
            databases=[{"id": "db-2", "title": "Investors"}],
        )
        ctx2 = run(_slack_events_mod._load_workspace_context("T-beta"))

        self.assertIn("Companies", ctx1)
        self.assertIn("Investors", ctx2)
        self.assertNotIn("Investors", ctx1)

    def test_format_has_databases_section(self):
        """Context string must start with the 'Databases in this workspace:' prefix."""
        _setup_notion_context_mock(
            databases=[{"id": "db-1", "title": "Pipeline"}],
        )
        ctx = run(_slack_events_mod._load_workspace_context("T-fmt"))
        self.assertIn("Databases in this workspace:", ctx)

    def test_format_has_crm_instructions_section(self):
        """When Settings/Context content is present, the label must appear."""
        _setup_notion_context_mock(
            databases=[],
            settings_content="Some CRM instructions here.",
        )
        ctx = run(_slack_events_mod._load_workspace_context("T-instr"))
        self.assertIn("CRM instructions:", ctx)
        self.assertIn("Some CRM instructions here.", ctx)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main(verbosity=2)
