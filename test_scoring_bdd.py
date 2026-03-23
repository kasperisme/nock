#!/usr/bin/env python3
"""
BDD calibration tests for _score_crm_relevance.

INTEGRATION tests — call the real Anthropic API.
Requires: ANTHROPIC_API_KEY (from .env or environment).
Run:      python test_scoring_bdd.py
          pytest test_scoring_bdd.py -v -s   # -s shows scores inline

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  HOW TO ADD A TEST CASE
  ──────────────────────
  Pick the right table below and add one line:

    E("short label",  "The Slack message text",  min_score, max_score),

  Typical intervals:
    Clearly relevant  →  (0.7, 1.0)
    Borderline        →  (0.35, 0.75)
    Clearly off-topic →  (0.0, 0.4)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import asyncio
import importlib.util
import os
import sys
import types
import unittest
from collections import namedtuple
from pathlib import Path
from unittest.mock import MagicMock

# Load .env from test directory (code/crm/.env) before imports
SCRIPT_DIR = Path(__file__).parent
_dotenv_path = SCRIPT_DIR / ".env"
if _dotenv_path.exists():
    from dotenv import load_dotenv
    load_dotenv(_dotenv_path)

# ---------------------------------------------------------------------------
# Event definition type
# ---------------------------------------------------------------------------

E = namedtuple("E", ["label", "text", "min_score", "max_score"])

# ---------------------------------------------------------------------------
# ✏️  EDIT THESE TABLES to add / tune test cases
# ---------------------------------------------------------------------------

HIGH_CONFIDENCE = [
    # Messages that are clearly about deals, contacts, or sales activity.
    # Expected: score in [min, max] — both must be >= THRESHOLD (0.7)
    E("contract signed",     "Great news — Acme Corp just signed the contract. Deal is closed!",                            0.7, 1.0),
    E("demo booked",         "Just booked a demo with TechCorp for Thursday 2pm — they want to see enterprise features.",   0.7, 1.0),
    E("proposal sent",       "Sent the proposal to Globex this morning — €80k for the annual plan. Waiting to hear back.", 0.7, 1.0),
    E("follow-up after call","Just off a call with Sarah at Initech — she's the decision maker. Following up next Tuesday.",0.7, 1.0),
    E("deal stage update",   "Moved BigCo to Negotiation stage — they're pushing back on price, need to loop in legal.",   0.7, 1.0),
    E("new lead",            "New lead from the website — Marcus from Umbrella Corp, interested in the Growth plan.",       0.7, 1.0),
]

LOW_CONFIDENCE = [
    # Messages with no CRM signal — should score well below the trigger threshold.
    # Expected: score in [min, max] — both must be < THRESHOLD (0.7)
    E("lunch invite",        "Anyone up for lunch today? There's a new ramen place around the corner.",                    0.0, 0.4),
    E("ci failure",          "CI is red on main — looks like the integration tests are timing out again. @team please look.", 0.0, 0.4),
    E("standup reminder",    "Reminder: daily standup in 10 minutes. Link in the channel description.",                    0.0, 0.4),
    E("office logistics",    "The coffee machine on the 2nd floor is out of order until tomorrow.",                        0.0, 0.4),
    E("friday message",      "Happy Friday everyone! Great week, see you all Monday 🎉",                                   0.0, 0.4),
]

BORDERLINE = [
    # Weak or indirect CRM signals — ambiguous enough that the model may land
    # anywhere in the middle band. Tune these intervals when adjusting the prompt.
    E("unnamed meeting",     "Meeting scheduled for Monday at 3pm with the new contact.",                                  0.2, 0.75),
    E("vague project chat",  "Had a good chat with John about the project — he seemed interested.",                        0.2, 0.75),
    E("internal pricing q",  "What's our current pricing for the Pro tier? Someone asked me today.",                       0.35, 0.9),
    E("call recap no deal",  "Just got off the phone with Emma — covered the roadmap and Q3 plans.",                       0.2, 0.75),
]

# ---------------------------------------------------------------------------
# Boilerplate below — no need to edit when adding test cases
# ---------------------------------------------------------------------------

CRM_DIR = SCRIPT_DIR / "packages" / "notion-crm" / "crm"
sys.path.insert(0, str(CRM_DIR))

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
    return_value={"access_token": "mock-token", "settings_path": None}
)
mock_logger.get_notion_access_token = MagicMock(return_value="mock-token")
sys.modules["crm_logger"] = mock_logger

mock_notion = types.ModuleType("notion_client")
mock_notion.NotionClient = MagicMock()
sys.modules["notion_client"] = mock_notion

CRM_MAIN = CRM_DIR / "__main__.py"
spec = importlib.util.spec_from_file_location("crm_main", CRM_MAIN)
crm_main = importlib.util.module_from_spec(spec)
spec.loader.exec_module(crm_main)

_score_crm_relevance = crm_main._score_crm_relevance
THRESHOLD = crm_main.EVENT_CONFIDENCE_THRESHOLD


def _run_score(text: str) -> tuple:
    return asyncio.run(_score_crm_relevance(text))


needs_api_key = unittest.skipUnless(
    os.environ.get("ANTHROPIC_API_KEY"),
    "ANTHROPIC_API_KEY not set — skipping live scoring tests",
)


def _make_test(event: E):
    """Return a test method that asserts event.text scores within [min, max]."""
    def test(self):
        s, reason, entities = _run_score(event.text)
        print(f"\n  [{event.label}]  score={s:.2f}  entities={entities}  reason='{reason}'")
        self.assertGreaterEqual(
            s, event.min_score,
            f"score {s:.2f} < {event.min_score}  |  {event.label!r}\n  reason: {reason}",
        )
        self.assertLessEqual(
            s, event.max_score,
            f"score {s:.2f} > {event.max_score}  |  {event.label!r}\n  reason: {reason}",
        )
    test.__doc__ = f"[{event.min_score}, {event.max_score}]  {event.text[:80]}"
    return test


def _build_test_class(name: str, scenario_label: str, events: list[E]) -> type:
    """Dynamically create a TestCase class with one method per event."""
    methods = {}
    for event in events:
        method_name = "test_" + event.label.lower().replace(" ", "_").replace("-", "_")
        methods[method_name] = _make_test(event)
    methods["__doc__"] = f"Scenario: {scenario_label}"
    return needs_api_key(type(name, (unittest.TestCase,), methods))


TestHighConfidence = _build_test_class(
    "TestHighConfidence",
    "Clearly CRM-relevant messages must score >= 0.7",
    HIGH_CONFIDENCE,
)

TestLowConfidence = _build_test_class(
    "TestLowConfidence",
    "Off-topic messages must score <= 0.4",
    LOW_CONFIDENCE,
)

TestBorderline = _build_test_class(
    "TestBorderline",
    "Ambiguous messages should land in the middle band",
    BORDERLINE,
)


if __name__ == "__main__":
    unittest.main(verbosity=2)
