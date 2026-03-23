#!/usr/bin/env python3
"""
BDD-style unit tests for Slack event subscription auto-handling.

Scenarios cover:
  - Message filtering (bots, subtypes, missing fields)
  - CRM relevance scoring and confidence threshold
  - Thread confirmation posting behaviour
  - Event type dispatch
  - Slack signature verification
  - url_verification challenge/response
  - Worker vs. async-invoke routing
  - Slack Interactivity (block_actions: Log it / Dismiss)

Run:  python test_events_bdd.py  (or pytest test_events_bdd.py -v)
"""

import asyncio
import base64
import hashlib
import hmac
import importlib.util
import json
import os
import sys
import types
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call

# ---------------------------------------------------------------------------
# Lightweight BDD helpers
# ---------------------------------------------------------------------------

class _Step:
    """Records Given / When / Then step labels for readable test output."""

    def __init__(self, label: str):
        self.label = label

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass


def given(description: str) -> _Step:
    return _Step(f"Given {description}")


def when(description: str) -> _Step:
    return _Step(f"When {description}")


def then(description: str) -> _Step:
    return _Step(f"Then {description}")


# ---------------------------------------------------------------------------
# Mock heavy deps before loading crm.__main__
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).parent
CRM_DIR = SCRIPT_DIR / "packages" / "notion-crm" / "crm"
sys.path.insert(0, str(CRM_DIR))

mock_anthropic = types.ModuleType("anthropic")
mock_anthropic.AsyncAnthropic = MagicMock()
sys.modules["anthropic"] = mock_anthropic

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

# Convenience aliases
_handle_message_event = crm_main._handle_message_event
_process_slack_event = crm_main._process_slack_event
_handle_slack_event = crm_main._handle_slack_event
_handle_slack_interaction = crm_main._handle_slack_interaction
_verify_slack_signature = crm_main._verify_slack_signature
_post_event_confirmation = crm_main._post_event_confirmation
_post_clarification_question = crm_main._post_clarification_question
EVENT_CONFIDENCE_THRESHOLD = crm_main.EVENT_CONFIDENCE_THRESHOLD
EVENT_BORDERLINE_LOW = crm_main.EVENT_BORDERLINE_LOW


def run(coro):
    """Run a coroutine synchronously."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Scenario 1 – Message filtering: bot messages are silently dropped
# ---------------------------------------------------------------------------

class TestBotMessageFiltering(unittest.TestCase):
    """Scenario: Bot messages are silently dropped without scoring."""

    @patch.object(crm_main, "_score_crm_relevance", new_callable=AsyncMock)
    def test_bot_message_skips_scoring(self, mock_score):
        """Bot messages must never reach the CRM relevance scorer."""
        with given("a Slack message event that has a bot_id field"):
            event = {
                "bot_id": "B12345",
                "text": "We just closed the Acme deal!",
                "channel": "C001",
                "ts": "1700000000.000100",
            }

        with when("the message event handler processes it"):
            run(_handle_message_event(event, team_id="T001"))

        with then("no CRM scoring is performed"):
            mock_score.assert_not_called()

    @patch.object(crm_main, "_score_crm_relevance", new_callable=AsyncMock)
    def test_message_subtype_skips_scoring(self, mock_score):
        """Edited/deleted messages (subtypes) must be ignored."""
        with given("a Slack message event with a subtype (e.g. message_changed)"):
            event = {
                "subtype": "message_changed",
                "text": "Updated: we signed the contract",
                "channel": "C001",
                "ts": "1700000000.000200",
            }

        with when("the message event handler processes it"):
            run(_handle_message_event(event, team_id="T001"))

        with then("no CRM scoring is performed"):
            mock_score.assert_not_called()

    @patch.object(crm_main, "_score_crm_relevance", new_callable=AsyncMock)
    def test_empty_text_skips_scoring(self, mock_score):
        """Messages without text content are not worth scoring."""
        with given("a Slack message event with empty text"):
            event = {"text": "", "channel": "C001", "ts": "1700000000.000300"}

        with when("the message event handler processes it"):
            run(_handle_message_event(event, team_id="T001"))

        with then("no CRM scoring is performed"):
            mock_score.assert_not_called()

    @patch.object(crm_main, "_score_crm_relevance", new_callable=AsyncMock)
    def test_missing_channel_skips_scoring(self, mock_score):
        """Messages without a channel cannot generate a thread reply."""
        with given("a Slack message event missing the channel field"):
            event = {"text": "Signed the contract with Acme", "ts": "1700000000.000400"}

        with when("the message event handler processes it"):
            run(_handle_message_event(event, team_id="T001"))

        with then("no CRM scoring is performed"):
            mock_score.assert_not_called()

    @patch.object(crm_main, "_score_crm_relevance", new_callable=AsyncMock)
    def test_missing_ts_skips_scoring(self, mock_score):
        """Messages without a timestamp cannot anchor a thread reply."""
        with given("a Slack message event missing the ts field"):
            event = {"text": "Signed the contract with Acme", "channel": "C001"}

        with when("the message event handler processes it"):
            run(_handle_message_event(event, team_id="T001"))

        with then("no CRM scoring is performed"):
            mock_score.assert_not_called()


# ---------------------------------------------------------------------------
# Scenario 2 – Confidence threshold: low-score messages are silent
# ---------------------------------------------------------------------------

class TestConfidenceThreshold(unittest.TestCase):
    """Scenario: Only messages above the confidence threshold trigger a confirmation."""

    @patch.object(crm_main, "_post_clarification_question", new_callable=AsyncMock)
    @patch.object(crm_main, "_post_event_confirmation", new_callable=AsyncMock)
    @patch.object(crm_main, "_score_crm_relevance", new_callable=AsyncMock)
    def test_score_below_borderline_is_silent(self, mock_score, mock_confirm, mock_clarify):
        """A score below EVENT_BORDERLINE_LOW must result in no Slack message at all."""
        with given("a message that scores below the borderline floor"):
            mock_score.return_value = (EVENT_BORDERLINE_LOW - 0.01, "not relevant", [])
            event = {"text": "Hey, what time is standup?", "channel": "C001", "ts": "1.0"}

        with when("the message event handler processes it"):
            run(_handle_message_event(event, team_id="T001"))

        with then("neither confirmation nor clarification is posted"):
            mock_confirm.assert_not_called()
            mock_clarify.assert_not_called()

    @patch.dict(os.environ, {"SLACK_BOT_TOKEN": "xoxb-test-token"}, clear=False)
    @patch.object(crm_main, "_post_event_confirmation", new_callable=AsyncMock)
    @patch.object(crm_main, "_score_crm_relevance", new_callable=AsyncMock)
    def test_score_at_threshold_triggers_confirmation(self, mock_score, mock_post):
        """A score exactly at the threshold should trigger a confirmation."""
        with given("a message that scores exactly at the threshold"):
            mock_score.return_value = (
                EVENT_CONFIDENCE_THRESHOLD,
                "Mentions a deal",
                ["Acme Corp"],
            )
            event = {"text": "Acme Corp signed the proposal", "channel": "C001", "ts": "1.0"}

        with when("the message event handler processes it"):
            run(_handle_message_event(event, team_id="T001"))

        with then("a confirmation is posted to the thread"):
            mock_post.assert_called_once()

    @patch.dict(os.environ, {"SLACK_BOT_TOKEN": "xoxb-test-token"}, clear=False)
    @patch.object(crm_main, "_post_event_confirmation", new_callable=AsyncMock)
    @patch.object(crm_main, "_score_crm_relevance", new_callable=AsyncMock)
    def test_high_score_triggers_confirmation(self, mock_score, mock_post):
        """A clearly CRM-relevant message should always trigger a confirmation."""
        with given("a message that scores well above the threshold"):
            mock_score.return_value = (0.92, "Deal signed with named company", ["BigCo"])
            event = {
                "text": "BigCo just signed! Contract is done.",
                "channel": "C002",
                "ts": "1700000001.000001",
            }

        with when("the message event handler processes it"):
            run(_handle_message_event(event, team_id="T001"))

        with then("a confirmation is posted to the correct channel and thread"):
            mock_post.assert_called_once()
            _, kwargs = mock_post.call_args
            self.assertEqual(kwargs.get("channel") or mock_post.call_args[0][1], "C002")

    @patch.dict(os.environ, {"SLACK_BOT_TOKEN": ""}, clear=False)
    @patch.object(crm_main, "_post_event_confirmation", new_callable=AsyncMock)
    @patch.object(crm_main, "_score_crm_relevance", new_callable=AsyncMock)
    def test_high_score_no_bot_token_no_confirmation(self, mock_score, mock_post):
        """Without a bot token the system cannot post; confirmation must be skipped."""
        with given("a high-scoring message but SLACK_BOT_TOKEN is not configured"):
            mock_score.return_value = (0.95, "Clearly a deal", ["Globex"])
            event = {
                "text": "Globex deal is closed.",
                "channel": "C003",
                "ts": "1700000002.000001",
            }

        with when("the message event handler processes it"):
            run(_handle_message_event(event, team_id="T001"))

        with then("no confirmation is attempted"):
            mock_post.assert_not_called()


# ---------------------------------------------------------------------------
# Scenario 3 – Borderline zone: clarifying question
# ---------------------------------------------------------------------------

class TestBorderlineClarification(unittest.TestCase):
    """Scenario: Messages in the borderline band trigger a clarifying question, not a confirmation."""

    @patch.dict(os.environ, {"SLACK_BOT_TOKEN": "xoxb-test-token"}, clear=False)
    @patch.object(crm_main, "_post_clarification_question", new_callable=AsyncMock)
    @patch.object(crm_main, "_post_event_confirmation", new_callable=AsyncMock)
    @patch.object(crm_main, "_score_crm_relevance", new_callable=AsyncMock)
    def test_borderline_score_asks_clarification_not_confirmation(self, mock_score, mock_confirm, mock_clarify):
        """A borderline score must trigger a clarification question, not a direct confirmation."""
        with given("a message that scores in the borderline band"):
            mock_score.return_value = (0.55, "Possibly sales-related", ["John"])
            event = {"text": "Had a chat with John about the project", "channel": "C001", "ts": "1.0"}

        with when("the message event handler processes it"):
            run(_handle_message_event(event, team_id="T001"))

        with then("a clarification question is posted and no confirmation"):
            mock_clarify.assert_called_once()
            mock_confirm.assert_not_called()

    @patch.dict(os.environ, {"SLACK_BOT_TOKEN": "xoxb-test-token"}, clear=False)
    @patch.object(crm_main, "_post_clarification_question", new_callable=AsyncMock)
    @patch.object(crm_main, "_post_event_confirmation", new_callable=AsyncMock)
    @patch.object(crm_main, "_score_crm_relevance", new_callable=AsyncMock)
    def test_borderline_floor_boundary_asks_clarification(self, mock_score, mock_confirm, mock_clarify):
        """A score exactly at EVENT_BORDERLINE_LOW must trigger clarification."""
        with given("a message scoring exactly at the borderline floor"):
            mock_score.return_value = (EVENT_BORDERLINE_LOW, "Weak signal", [])
            event = {"text": "Meeting Monday with someone", "channel": "C001", "ts": "1.0"}

        with when("the message event handler processes it"):
            run(_handle_message_event(event, team_id="T001"))

        with then("a clarification question is posted"):
            mock_clarify.assert_called_once()
            mock_confirm.assert_not_called()

    @patch.dict(os.environ, {"SLACK_BOT_TOKEN": "xoxb-test-token"}, clear=False)
    @patch.object(crm_main, "_post_clarification_question", new_callable=AsyncMock)
    @patch.object(crm_main, "_post_event_confirmation", new_callable=AsyncMock)
    @patch.object(crm_main, "_score_crm_relevance", new_callable=AsyncMock)
    def test_clarification_receives_entities_and_reason(self, mock_score, mock_confirm, mock_clarify):
        """Entities and reason from the scorer must be forwarded to the clarification post."""
        with given("a borderline message with extracted entities"):
            reason = "Mentions a person and a project"
            entities = ["Emma", "ProjectX"]
            mock_score.return_value = (0.5, reason, entities)
            event = {"text": "Emma called about ProjectX", "channel": "C005", "ts": "2.0"}

        with when("the message event handler processes it"):
            run(_handle_message_event(event, team_id="T001"))

        with then("the clarification post receives the correct entities and reason"):
            mock_clarify.assert_called_once()
            args, kwargs = mock_clarify.call_args
            all_args = list(args) + list(kwargs.values())
            self.assertIn(reason, all_args)
            self.assertIn(entities, all_args)

    @patch.dict(os.environ, {"SLACK_BOT_TOKEN": ""}, clear=False)
    @patch.object(crm_main, "_post_clarification_question", new_callable=AsyncMock)
    @patch.object(crm_main, "_score_crm_relevance", new_callable=AsyncMock)
    def test_borderline_no_bot_token_is_silent(self, mock_score, mock_clarify):
        """Without a bot token the clarification cannot be posted."""
        with given("a borderline message but SLACK_BOT_TOKEN is not configured"):
            mock_score.return_value = (0.55, "Weak signal", ["Someone"])
            event = {"text": "Spoke with someone today", "channel": "C001", "ts": "1.0"}

        with when("the message event handler processes it"):
            run(_handle_message_event(event, team_id="T001"))

        with then("no clarification is attempted"):
            mock_clarify.assert_not_called()


def test_clarification_block_has_yes_and_no_buttons():
    """The clarification block must have Yes/No action buttons with correct action_ids."""
    sent_payloads = []

    class FakeResponse:
        status_code = 200
        text = '{"ok": true}'
        def json(self): return {"ok": True}

    class FakeClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *_): pass
        async def post(self, url, **kwargs):
            sent_payloads.append(kwargs.get("json", {}))
            return FakeResponse()

    with patch("httpx.AsyncClient", return_value=FakeClient()):
        asyncio.run(
            _post_clarification_question(
                bot_token="xoxb-test",
                channel="C001",
                thread_ts="1.0",
                message_text="Had a chat with John",
                reason="Possibly sales-related",
                entities=["John"],
            )
        )

    assert sent_payloads, "No payload sent"
    blocks = sent_payloads[0].get("blocks", [])
    action_blocks = [b for b in blocks if b.get("type") == "actions"]
    assert action_blocks, "No actions block found"
    action_ids = [e["action_id"] for e in action_blocks[0]["elements"]]
    assert "event_clarify_yes" in action_ids
    assert "event_clarify_no" in action_ids


# ---------------------------------------------------------------------------
# Scenario 4 – Confirmation message content
# ---------------------------------------------------------------------------

class TestConfirmationContent(unittest.TestCase):
    """Scenario: Confirmation message includes entities, reason, and action buttons."""

    @patch.dict(os.environ, {"SLACK_BOT_TOKEN": "xoxb-test-token"}, clear=False)
    @patch.object(crm_main, "_post_event_confirmation", new_callable=AsyncMock)
    @patch.object(crm_main, "_score_crm_relevance", new_callable=AsyncMock)
    def test_confirmation_receives_correct_entities_and_reason(self, mock_score, mock_post):
        """Entities and reason extracted by the scorer are forwarded to the confirmation."""
        with given("a message scored with specific entities and a reason"):
            reason = "Mentions a named company and deal stage"
            entities = ["TechCorp", "Sarah"]
            mock_score.return_value = (0.85, reason, entities)
            event = {
                "text": "Sarah from TechCorp signed the contract today.",
                "channel": "C010",
                "ts": "1700000010.000001",
            }

        with when("the message event handler processes it"):
            run(_handle_message_event(event, team_id="T001"))

        with then("the confirmation post receives the scorer's entities and reason"):
            mock_post.assert_called_once()
            args, kwargs = mock_post.call_args
            # Support both positional and keyword argument styles
            all_args = list(args) + list(kwargs.values())
            self.assertIn(reason, all_args)
            self.assertIn(entities, all_args)

    def test_confirmation_block_contains_log_and_dismiss_buttons(self):
        """The posted block must have Log it / Dismiss action buttons."""
        captured = {}

        async def _capture_post(*args, **kwargs):
            captured.update(kwargs if kwargs else {})

        with given("a confirmation is about to be posted via the real builder"):
            # We test the block structure by patching httpx at the network layer
            import unittest.mock as mock

            sent_payloads = []

            class FakeResponse:
                status_code = 200
                text = '{"ok": true}'

                def json(self):
                    return {"ok": True}

            class FakeClient:
                async def __aenter__(self):
                    return self

                async def __aexit__(self, *_):
                    pass

                async def post(self, url, **kwargs):
                    sent_payloads.append(kwargs.get("json", {}))
                    return FakeResponse()

        with when("_post_event_confirmation is called directly"):
            with patch("httpx.AsyncClient", return_value=FakeClient()):
                run(
                    _post_event_confirmation(
                        bot_token="xoxb-test",
                        channel="C001",
                        thread_ts="1.0",
                        message_text="Deal signed",
                        reason="Company and deal language detected",
                        entities=["Acme"],
                    )
                )

        with then("the payload includes Log it and Dismiss action buttons"):
            self.assertTrue(sent_payloads, "No payload was sent to Slack")
            payload = sent_payloads[0]
            blocks = payload.get("blocks", [])
            action_blocks = [b for b in blocks if b.get("type") == "actions"]
            self.assertTrue(action_blocks, "No actions block found")
            elements = action_blocks[0]["elements"]
            action_ids = [e["action_id"] for e in elements]
            self.assertIn("event_confirm_log", action_ids)
            self.assertIn("event_dismiss", action_ids)

    def test_confirmation_entity_fallback_when_no_entities(self):
        """When no entities are extracted, fallback text must be 'this conversation'."""
        sent_payloads = []

        class FakeResponse:
            status_code = 200
            text = '{"ok": true}'

            def json(self):
                return {"ok": True}

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                pass

            async def post(self, url, **kwargs):
                sent_payloads.append(kwargs.get("json", {}))
                return FakeResponse()

        with given("a confirmation is posted with an empty entities list"):
            pass

        with when("_post_event_confirmation runs"):
            with patch("httpx.AsyncClient", return_value=FakeClient()):
                run(
                    _post_event_confirmation(
                        bot_token="xoxb-test",
                        channel="C001",
                        thread_ts="1.0",
                        message_text="Anything",
                        reason="some reason",
                        entities=[],
                    )
                )

        with then("the fallback text refers to 'this conversation'"):
            self.assertTrue(sent_payloads)
            text_field = sent_payloads[0].get("text", "")
            self.assertIn("this conversation", text_field)


# ---------------------------------------------------------------------------
# Scenario 5 – Event type dispatch
# ---------------------------------------------------------------------------

class TestEventTypeDispatch(unittest.TestCase):
    """Scenario: Only supported event types reach their handler."""

    @patch.object(crm_main, "_handle_message_event", new_callable=AsyncMock)
    def test_message_event_dispatched_to_handler(self, mock_handler):
        """Events of type 'message' must be routed to _handle_message_event."""
        with given("an event_callback payload containing a message event"):
            event = {"type": "message", "text": "Hello", "channel": "C001", "ts": "1.0"}

        with when("_process_slack_event dispatches the event"):
            run(_process_slack_event(event, team_id="T001"))

        with then("the message handler is invoked once with the event"):
            mock_handler.assert_called_once_with(event, "T001")

    @patch.object(crm_main, "_handle_message_event", new_callable=AsyncMock)
    def test_unknown_event_type_not_dispatched(self, mock_handler):
        """Unknown event types must be silently ignored (no handler invoked)."""
        with given("an event of an unsupported type (e.g. reaction_added)"):
            event = {"type": "reaction_added", "reaction": "thumbsup"}

        with when("_process_slack_event dispatches the event"):
            run(_process_slack_event(event, team_id="T001"))

        with then("the message handler is NOT invoked"):
            mock_handler.assert_not_called()

    @patch.object(crm_main, "_handle_message_event", new_callable=AsyncMock)
    def test_member_joined_event_not_dispatched(self, mock_handler):
        """member_joined_channel events must be ignored."""
        with given("a member_joined_channel event"):
            event = {"type": "member_joined_channel", "user": "U123", "channel": "C001"}

        with when("_process_slack_event dispatches the event"):
            run(_process_slack_event(event, team_id="T001"))

        with then("the message handler is NOT invoked"):
            mock_handler.assert_not_called()


# ---------------------------------------------------------------------------
# Scenario 6 – url_verification challenge/response
# ---------------------------------------------------------------------------

class TestUrlVerification(unittest.TestCase):
    """Scenario: Slack url_verification events must echo back the challenge."""

    def test_url_verification_returns_challenge(self):
        """The challenge value must be echoed back verbatim in the response."""
        with given("a url_verification payload from Slack"):
            args = {"type": "url_verification", "challenge": "3eZbrw1aBm2rZgRNFdxV2595E9CY3gmdALWMmHkvFXO"}

        with when("the Slack event handler receives it"):
            result = run(_handle_slack_event(args))

        with then("the response body contains the exact challenge value"):
            body = json.loads(result.get("body", "{}"))
            self.assertEqual(body.get("challenge"), args["challenge"])
            self.assertEqual(result.get("statusCode"), 200)

    def test_url_verification_does_not_check_signature(self):
        """url_verification must succeed even without a signing secret configured."""
        with given("a url_verification payload and no SLACK_SIGNING_SECRET set"):
            args = {"type": "url_verification", "challenge": "abc123"}

        with when("the Slack event handler receives it without any signature headers"):
            with patch.dict(os.environ, {"SLACK_SIGNING_SECRET": ""}, clear=False):
                result = run(_handle_slack_event(args))

        with then("the response is 200 with the challenge"):
            self.assertEqual(result.get("statusCode"), 200)
            body = json.loads(result.get("body", "{}"))
            self.assertEqual(body.get("challenge"), "abc123")


# ---------------------------------------------------------------------------
# Scenario 7 – Slack signature verification
# ---------------------------------------------------------------------------

class TestSignatureVerification(unittest.TestCase):
    """Scenario: Invalid or missing signatures must result in a 401 rejection."""

    def _make_signed_args(self, secret: str, body_dict: dict) -> dict:
        """Build a properly signed fake Slack request."""
        body_str = json.dumps(body_dict)
        body_b64 = base64.b64encode(body_str.encode()).decode()
        timestamp = "1609459200"
        sig_base = f"v0:{timestamp}:{body_str}".encode()
        sig = "v0=" + hmac.new(secret.encode(), sig_base, hashlib.sha256).hexdigest()
        return {
            "__ow_body": body_b64,
            "__ow_headers": {
                "x-slack-request-timestamp": timestamp,
                "x-slack-signature": sig,
            },
        }

    def test_valid_signature_is_accepted(self):
        """A correctly signed request must pass verification."""
        with given("a request signed with the correct SLACK_SIGNING_SECRET"):
            secret = "my-signing-secret"
            args = self._make_signed_args(secret, {"dummy": "payload"})

        with when("the signature is verified"):
            with patch.dict(os.environ, {"SLACK_SIGNING_SECRET": secret}, clear=False):
                result = _verify_slack_signature(args)

        with then("verification passes"):
            self.assertTrue(result)

    def test_wrong_secret_is_rejected(self):
        """A request signed with the wrong secret must fail verification."""
        with given("a request signed with the wrong secret"):
            args = self._make_signed_args("wrong-secret", {"dummy": "payload"})

        with when("the signature is verified against the real secret"):
            with patch.dict(os.environ, {"SLACK_SIGNING_SECRET": "real-secret"}, clear=False):
                result = _verify_slack_signature(args)

        with then("verification fails"):
            self.assertFalse(result)

    def test_tampered_body_is_rejected(self):
        """Modifying the body after signing must invalidate the signature."""
        with given("a valid signed request whose body is then tampered"):
            secret = "signing-secret"
            args = self._make_signed_args(secret, {"original": "body"})
            # Tamper by replacing the body
            args["__ow_body"] = base64.b64encode(b'{"tampered": "body"}').decode()

        with when("the signature is verified"):
            with patch.dict(os.environ, {"SLACK_SIGNING_SECRET": secret}, clear=False):
                result = _verify_slack_signature(args)

        with then("verification fails"):
            self.assertFalse(result)

    def test_missing_secret_disables_checking(self):
        """With no SLACK_SIGNING_SECRET configured, all requests pass (dev mode)."""
        with given("no SLACK_SIGNING_SECRET is set in the environment"):
            args = {"__ow_headers": {}, "__ow_body": "anything"}

        with when("the signature is verified"):
            with patch.dict(os.environ, {"SLACK_SIGNING_SECRET": ""}, clear=False):
                result = _verify_slack_signature(args)

        with then("verification passes (dev mode bypass)"):
            self.assertTrue(result)

    @patch.object(crm_main, "_process_slack_event", new_callable=AsyncMock)
    def test_event_callback_with_invalid_signature_returns_401(self, mock_process):
        """An event_callback with a bad signature must be rejected with 401."""
        with given("an event_callback payload with an invalid signature"):
            args = {
                "type": "event_callback",
                "event": {"type": "message", "text": "hi", "channel": "C1", "ts": "1.0"},
                "team_id": "T001",
                "__ow_headers": {
                    "x-slack-request-timestamp": "1609459200",
                    "x-slack-signature": "v0=invalidsignature",
                },
                "__ow_body": base64.b64encode(b"body").decode(),
            }

        with when("the Slack event handler processes it"):
            with patch.dict(os.environ, {"SLACK_SIGNING_SECRET": "real-secret"}, clear=False):
                result = run(_handle_slack_event(args))

        with then("a 401 Unauthorized response is returned and event is not processed"):
            self.assertEqual(result.get("statusCode"), 401)
            mock_process.assert_not_called()


# ---------------------------------------------------------------------------
# Scenario 8 – Worker vs. async-invoke routing
# ---------------------------------------------------------------------------

class TestWorkerRouting(unittest.TestCase):
    """Scenario: Async routing sends a background worker or falls back to sync."""

    @patch.object(crm_main, "_process_slack_event", new_callable=AsyncMock)
    def test_event_worker_flag_processes_synchronously(self, mock_process):
        """When _event_worker is set, the event must be processed in the same call."""
        with given("an event_callback payload with the _event_worker flag set"):
            mock_process.return_value = crm_main._ok({"status": "processed"})
            args = {
                "type": "event_callback",
                "event": {"type": "message", "text": "hi"},
                "team_id": "T001",
                "_event_worker": 1,
                "__ow_headers": {},
                "__ow_body": "",
            }

        with when("the Slack event handler processes it (no signature checking in dev mode)"):
            with patch.dict(os.environ, {"SLACK_SIGNING_SECRET": ""}, clear=False):
                result = run(_handle_slack_event(args))

        with then("_process_slack_event is called directly and result is returned"):
            mock_process.assert_called_once()
            self.assertEqual(result.get("statusCode"), 200)

    @patch.object(crm_main, "_process_slack_event", new_callable=AsyncMock)
    def test_no_async_config_falls_back_to_sync(self, mock_process):
        """Without DO_SLACK_ASYNC_URL configured, event processing runs synchronously."""
        with given("an event_callback without async config and without _event_worker"):
            mock_process.return_value = crm_main._ok({"status": "processed"})
            args = {
                "type": "event_callback",
                "event": {"type": "message", "text": "hi"},
                "team_id": "T001",
                "__ow_headers": {},
                "__ow_body": "",
            }

        with when("the handler runs with no DO_SLACK_ASYNC_URL set"):
            with patch.dict(
                os.environ,
                {"SLACK_SIGNING_SECRET": "", "DO_SLACK_ASYNC_URL": "", "DO_SLACK_ASYNC_TOKEN": ""},
                clear=False,
            ):
                result = run(_handle_slack_event(args))

        with then("event is processed synchronously and response is 200 ok"):
            mock_process.assert_called_once()
            self.assertEqual(result.get("statusCode"), 200)
            body = json.loads(result.get("body", "{}"))
            self.assertEqual(body.get("status"), "ok")

    @patch("httpx.Client")
    @patch.object(crm_main, "_process_slack_event", new_callable=AsyncMock)
    def test_async_config_spawns_background_invoke(self, mock_process, mock_httpx_client):
        """With DO_SLACK_ASYNC_URL set, a background HTTP invoke is fired and 200 returned immediately."""
        with given("an event_callback and DO_SLACK_ASYNC_URL is configured"):
            fake_response = MagicMock()
            fake_response.status_code = 200
            fake_client_instance = MagicMock()
            fake_client_instance.__enter__ = MagicMock(return_value=fake_client_instance)
            fake_client_instance.__exit__ = MagicMock(return_value=False)
            fake_client_instance.post = MagicMock(return_value=fake_response)
            mock_httpx_client.return_value = fake_client_instance

            args = {
                "type": "event_callback",
                "event": {"type": "message", "text": "Deal closed"},
                "team_id": "T001",
                "__ow_headers": {},
                "__ow_body": "",
            }

        with when("the handler runs with async config present"):
            with patch.dict(
                os.environ,
                {
                    "SLACK_SIGNING_SECRET": "",
                    "DO_SLACK_ASYNC_URL": "https://faas-example.doserverless.co/api/v1/web/fn-xxx/notion-crm/crm",
                    "DO_SLACK_ASYNC_TOKEN": "Basic dGVzdDp0ZXN0",
                },
                clear=False,
            ):
                result = run(_handle_slack_event(args))

        with then("synchronous _process_slack_event is NOT called and 200 is returned"):
            mock_process.assert_not_called()
            self.assertEqual(result.get("statusCode"), 200)
            body = json.loads(result.get("body", "{}"))
            self.assertEqual(body.get("status"), "ok")

        with then("the background HTTP client was invoked with _event_worker payload"):
            fake_client_instance.post.assert_called_once()
            _, call_kwargs = fake_client_instance.post.call_args
            posted_json = call_kwargs.get("json", {})
            self.assertEqual(posted_json.get("_event_worker"), 1)


# ---------------------------------------------------------------------------
# Scenario 9 – Main router correctly dispatches event payloads
# ---------------------------------------------------------------------------

class TestMainRouterEventDispatch(unittest.TestCase):
    """Scenario: The top-level main() routes Slack Events API payloads correctly."""

    @patch.object(crm_main, "_handle_slack_event", new_callable=AsyncMock)
    def test_main_routes_url_verification_to_event_handler(self, mock_handler):
        """main() must route url_verification payloads through _handle_slack_event."""
        with given("a url_verification payload arrives at main()"):
            mock_handler.return_value = crm_main._ok({"challenge": "abc"})
            args = {"type": "url_verification", "challenge": "abc"}

        with when("main() processes the payload"):
            result = crm_main.main(args)

        with then("_handle_slack_event is called"):
            mock_handler.assert_called_once()

    @patch.object(crm_main, "_handle_slack_event", new_callable=AsyncMock)
    def test_main_routes_event_callback_to_event_handler(self, mock_handler):
        """main() must route event_callback payloads through _handle_slack_event."""
        with given("an event_callback payload arrives at main()"):
            mock_handler.return_value = crm_main._ok({"status": "ok"})
            args = {
                "type": "event_callback",
                "event": {"type": "message"},
                "team_id": "T001",
            }

        with when("main() processes the payload"):
            result = crm_main.main(args)

        with then("_handle_slack_event is called"):
            mock_handler.assert_called_once()


# ---------------------------------------------------------------------------
# Scenario 10 – Slack Interactivity (block_actions from Log it / Dismiss)
# ---------------------------------------------------------------------------

class TestSlackInteraction(unittest.TestCase):
    """Scenario: block_actions payloads (Log it / Dismiss) are handled correctly."""

    def test_unsupported_interaction_type_returns_400(self):
        """Non-block_actions interaction types must return 400."""
        with given("an interaction payload with type other than block_actions"):
            args = {"_interaction_payload": {"type": "view_submission"}}

        with when("_handle_slack_interaction processes it"):
            result = run(_handle_slack_interaction(args))

        with then("400 Unsupported interaction type is returned"):
            self.assertEqual(result.get("statusCode"), 400)

    def test_empty_actions_returns_200(self):
        """block_actions with no actions must return 200 (empty body)."""
        with given("a block_actions payload with empty actions"):
            args = {"_interaction_payload": {"type": "block_actions", "actions": []}}

        with when("_handle_slack_interaction processes it"):
            result = run(_handle_slack_interaction(args))

        with then("200 ok with empty body is returned"):
            self.assertEqual(result.get("statusCode"), 200)

    @patch("httpx.Client")
    def test_event_dismiss_posts_delete_original(self, mock_httpx_client):
        """Dismiss button must POST delete_original to response_url."""
        with given("a block_actions payload with event_dismiss action"):
            fake_instance = MagicMock()
            fake_instance.__enter__ = MagicMock(return_value=fake_instance)
            fake_instance.__exit__ = MagicMock(return_value=False)
            fake_instance.post = MagicMock(return_value=MagicMock(status_code=200))
            mock_httpx_client.return_value = fake_instance

            args = {
                "_interaction_payload": {
                    "type": "block_actions",
                    "actions": [{"action_id": "event_dismiss", "value": "dismiss"}],
                    "response_url": "https://hooks.slack.com/actions/T1/A1/xxx",
                }
            }

        with when("_handle_slack_interaction processes it"):
            result = run(_handle_slack_interaction(args))

        with then("response_url receives delete_original and 200 is returned"):
            self.assertEqual(result.get("statusCode"), 200)
            fake_instance.post.assert_called_once()
            _, call_kwargs = fake_instance.post.call_args
            self.assertEqual(call_kwargs.get("json", {}).get("delete_original"), True)

    @patch("httpx.Client")
    def test_event_clarify_no_posts_delete_original(self, mock_httpx_client):
        """No thanks (event_clarify_no) must POST delete_original to response_url."""
        with given("a block_actions payload with event_clarify_no action"):
            fake_instance = MagicMock()
            fake_instance.__enter__ = MagicMock(return_value=fake_instance)
            fake_instance.__exit__ = MagicMock(return_value=False)
            fake_instance.post = MagicMock(return_value=MagicMock(status_code=200))
            mock_httpx_client.return_value = fake_instance

            args = {
                "_interaction_payload": {
                    "type": "block_actions",
                    "actions": [{"action_id": "event_clarify_no", "value": "dismiss"}],
                    "response_url": "https://hooks.slack.com/actions/T1/A1/yyy",
                }
            }

        with when("_handle_slack_interaction processes it"):
            result = run(_handle_slack_interaction(args))

        with then("response_url receives delete_original and 200 is returned"):
            self.assertEqual(result.get("statusCode"), 200)
            fake_instance.post.assert_called_once()
            _, call_kwargs = fake_instance.post.call_args
            self.assertEqual(call_kwargs.get("json", {}).get("delete_original"), True)

    @patch("httpx.Client")
    def test_event_confirm_log_without_response_url_returns_200(self, mock_httpx_client):
        """Log it without response_url must return 200 (logs warning, no processing)."""
        with given("a block_actions payload with event_confirm_log but no response_url"):
            args = {
                "_interaction_payload": {
                    "type": "block_actions",
                    "actions": [
                        {"action_id": "event_confirm_log", "value": "John from Acme called"}
                    ],
                    "response_url": "",
                    "team": {"id": "T001"},
                    "user": {"id": "U001"},
                    "channel": {"id": "C001"},
                    "container": {"message_ts": "123.456", "channel_id": "C001"},
                }
            }

        with when("_handle_slack_interaction processes it"):
            with patch.dict(os.environ, {"SLACK_BOT_TOKEN": ""}, clear=False):
                result = run(_handle_slack_interaction(args))

        with then("200 is returned (no processing without response_url)"):
            self.assertEqual(result.get("statusCode"), 200)

    @patch.object(crm_main, "_run_interaction_worker", new_callable=AsyncMock)
    @patch("httpx.Client")
    def test_event_confirm_log_posts_replace_original_and_runs_worker(
        self, mock_httpx_client, mock_worker
    ):
        """Log it must replace the message with Processing... and run the slash command."""
        with given("a block_actions payload with event_confirm_log and response_url"):
            post_calls = []

            def capture_post(*args, **kwargs):
                post_calls.append(args + (kwargs,))
                return MagicMock(status_code=200)

            fake_instance = MagicMock()
            fake_instance.__enter__ = MagicMock(return_value=fake_instance)
            fake_instance.__exit__ = MagicMock(return_value=False)
            fake_instance.post = MagicMock(side_effect=capture_post)
            fake_instance.get = MagicMock(
                return_value=MagicMock(
                    status_code=200,
                    json=lambda: {
                        "ok": True,
                        "messages": [{"ts": "123.456", "thread_ts": "123.000"}],
                    },
                )
            )
            mock_httpx_client.return_value = fake_instance
            mock_worker.return_value = crm_main._ok({"status": "processed"})

            args = {
                "_interaction_payload": {
                    "type": "block_actions",
                    "actions": [
                        {"action_id": "event_confirm_log", "value": "John from Acme called"}
                    ],
                    "response_url": "https://hooks.slack.com/actions/T1/A1/xxx",
                    "team": {"id": "T001"},
                    "user": {"id": "U001"},
                    "channel": {"id": "C001"},
                    "container": {"message_ts": "123.456", "channel_id": "C001"},
                }
            }

        with when("_handle_slack_interaction processes it (no async URL)"):
            with patch.dict(
                os.environ,
                {"SLACK_BOT_TOKEN": "xoxb-test", "DO_SLACK_ASYNC_URL": "", "DO_SLACK_ASYNC_TOKEN": ""},
                clear=False,
            ):
                result = run(_handle_slack_interaction(args))

        with then("200 is returned"):
            self.assertEqual(result.get("statusCode"), 200)

        with then("response_url receives replace_original with Processing..."):
            response_url_posts = [
                c for c in post_calls if c and len(c) > 1 and c[1].get("json", {}).get("replace_original")
            ]
            self.assertTrue(response_url_posts, "Expected POST with replace_original")
            payload = response_url_posts[0][1].get("json", {})
            self.assertIn("Processing your request", payload.get("text", ""))

        with then("_run_interaction_worker is invoked with correctly formatted slash invocation"):
            mock_worker.assert_called_once()
            payload = mock_worker.call_args[0][0]
            self._assert_slash_invocation_format(
                payload,
                text="John from Acme called",
                team_id="T001",
                user_id="U001",
                channel="C001",
                message_ts="123.456",
                thread_ts="123.000",
            )

    def _assert_slash_invocation_format(
        self,
        payload,
        *,
        text,
        team_id,
        user_id,
        channel,
        message_ts,
        thread_ts=None,
    ):
        """Assert payload matches slash command invocation format (/nock <text>)."""
        self.assertEqual(payload.get("text"), text, "text must be the message to log")
        self.assertEqual(payload.get("team_id"), team_id)
        self.assertEqual(payload.get("user_id"), user_id)
        self.assertEqual(payload.get("channel_id"), channel)
        self.assertEqual(payload.get("response_url"), "", "interaction uses chat.update, not response_url")
        self.assertEqual(payload.get("_interaction_worker"), 1)
        self.assertEqual(payload.get("slack_context"), {"team_id": team_id, "user_id": user_id})
        upd = payload.get("_interaction_update", {})
        self.assertEqual(upd.get("channel"), channel)
        self.assertEqual(upd.get("message_ts"), message_ts)
        self.assertEqual(upd.get("thread_ts"), thread_ts)

    @patch("httpx.Client")
    def test_event_confirm_log_with_async_url_invokes_background(self, mock_httpx_client):
        """With DO_SLACK_ASYNC_URL set, Log it must POST to invoke URL with worker payload."""
        with given("a block_actions payload with event_confirm_log and async URL configured"):
            post_calls = []

            def capture_post(url, **kwargs):
                post_calls.append((url, kwargs))
                return MagicMock(status_code=200)

            fake_instance = MagicMock()
            fake_instance.__enter__ = MagicMock(return_value=fake_instance)
            fake_instance.__exit__ = MagicMock(return_value=False)
            fake_instance.post = MagicMock(side_effect=capture_post)
            fake_instance.get = MagicMock(
                return_value=MagicMock(
                    status_code=200,
                    json=lambda: {
                        "ok": True,
                        "messages": [{"ts": "123.456"}],
                    },
                )
            )
            mock_httpx_client.return_value = fake_instance

            args = {
                "_interaction_payload": {
                    "type": "block_actions",
                    "actions": [
                        {"action_id": "event_confirm_log", "value": "Deal closed with Acme"}
                    ],
                    "response_url": "https://hooks.slack.com/actions/T1/A1/xxx",
                    "team": {"id": "T001"},
                    "user": {"id": "U001"},
                    "channel": {"id": "C001"},
                    "container": {"message_ts": "123.456", "channel_id": "C001"},
                }
            }

        with when("_handle_slack_interaction processes it with async config"):
            with patch.dict(
                os.environ,
                {
                    "SLACK_BOT_TOKEN": "xoxb-test",
                    "DO_SLACK_ASYNC_URL": "https://faas.example.com/api/v1/web/fn/notion-crm/crm",
                    "DO_SLACK_ASYNC_TOKEN": "Basic dGVzdDp0ZXN0",
                },
                clear=False,
            ):
                result = run(_handle_slack_interaction(args))

        with then("200 is returned"):
            self.assertEqual(result.get("statusCode"), 200)

        with then("invoke URL was called with correctly formatted slash invocation"):
            invoke_posts = [(url, kw) for url, kw in post_calls if "blocking=false" in str(url)]
            self.assertTrue(invoke_posts, "Expected POST to invoke URL with blocking=false")
            _, call_kwargs = invoke_posts[0]
            payload = call_kwargs.get("json", {})
            self._assert_slash_invocation_format(
                payload,
                text="Deal closed with Acme",
                team_id="T001",
                user_id="U001",
                channel="C001",
                message_ts="123.456",
                thread_ts=None,
            )


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main(verbosity=2)
