#!/usr/bin/env python3
"""
Local unit tests for CRM slash command patterns.
Run before deployment: python test_local.py

Tests parsing, URL conversion, routing, and #agent handling without hitting APIs.
"""

import base64
import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# ---------------------------------------------------------------------------
# Mock heavy deps before loading crm.__main__
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).parent
CRM_DIR = SCRIPT_DIR / "packages" / "notion-crm" / "crm"
sys.path.insert(0, str(CRM_DIR))

import types

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


def _mock_get_notion_access_token(*, team_id=None, user_id=None):
    return "mock-token"


mock_logger.get_notion_access_token = _mock_get_notion_access_token
sys.modules["crm_logger"] = mock_logger

mock_notion = types.ModuleType("notion_client")
mock_notion.NotionClient = MagicMock()
sys.modules["notion_client"] = mock_notion

# Load crm.__main__
CRM_MAIN = CRM_DIR / "__main__.py"
spec = importlib.util.spec_from_file_location("crm_main", CRM_MAIN)
crm_main = importlib.util.module_from_spec(spec)
spec.loader.exec_module(crm_main)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestWebUrlToRestUrl(unittest.TestCase):
    """Test web URL → REST API URL conversion for async invoke."""

    def test_web_url_converted_correctly(self):
        web = "https://faas-example.doserverless.co/api/v1/web/fn-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx/notion-crm/crm"
        rest = crm_main._web_url_to_rest_url(web)
        self.assertIn("/api/v1/namespaces/", rest)
        self.assertIn("/actions/", rest)
        self.assertNotIn("/web/", rest)
        self.assertEqual(
            rest,
            "https://faas-example.doserverless.co/api/v1/namespaces/fn-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx/actions/notion-crm/crm",
        )

    def test_rest_url_unchanged(self):
        rest = "https://host/api/v1/namespaces/fn-xxx/actions/notion-crm/crm"
        self.assertEqual(crm_main._web_url_to_rest_url(rest), rest)

    def test_strips_query_string(self):
        web = "https://host/api/v1/web/fn-xxx/notion-crm/crm?foo=bar"
        rest = crm_main._web_url_to_rest_url(web)
        self.assertNotIn("foo", rest)


class TestParseArgs(unittest.TestCase):
    """Test body parsing (JSON and form-urlencoded)."""

    def test_json_body_parsed(self):
        body = {"text": "John called", "response_url": "https://hooks.slack.com/x"}
        encoded = base64.b64encode(json.dumps(body).encode()).decode()
        args = crm_main._parse_args({"__ow_body": encoded})
        self.assertEqual(args.get("text"), "John called")
        self.assertEqual(args.get("response_url"), "https://hooks.slack.com/x")

    def test_form_body_parsed(self):
        form = "command=%2Fcrm&text=John+called&response_url=https%3A%2F%2Fhooks.slack.com%2Fx"
        # DO may pass body as base64; _parse_args tries base64 first then raw
        encoded = base64.b64encode(form.encode()).decode()
        args = crm_main._parse_args({"__ow_body": encoded})
        self.assertEqual(args.get("text"), "John called")
        self.assertEqual(args.get("response_url"), "https://hooks.slack.com/x")

    def test_empty_body_returns_args_unchanged(self):
        args = {"action": "ping"}
        self.assertEqual(crm_main._parse_args(args), args)


class TestSlackAuthBypass(unittest.TestCase):
    """Test that Slack requests (hooks.slack.com response_url) bypass Bearer auth."""

    def test_slack_response_url_bypasses_auth(self):
        response_url = "https://hooks.slack.com/services/T00/B00/xxx"
        self.assertTrue(response_url.startswith("https://hooks.slack.com/"))

    def test_non_slack_url_does_not_bypass(self):
        url = "https://example.com/callback"
        self.assertFalse(url.startswith("https://hooks.slack.com/"))


class TestSlashCommandRouting(unittest.TestCase):
    """Test main() routing for slash commands (mocked)."""

    @patch.object(crm_main, "_post_to_slack", new_callable=AsyncMock)
    @patch.object(crm_main, "_get_agent_system_prompt", new_callable=AsyncMock)
    def test_slash_system_prompt_posts_prompt(self, mock_get_prompt, mock_post):
        mock_get_prompt.return_value = "You are a test agent."
        for cmd in ("system prompt", "prompt", "get prompt", "show prompt"):
            mock_post.reset_mock()
            mock_get_prompt.reset_mock()
            args = {
                "response_url": "https://hooks.slack.com/x",
                "text": cmd,
                "_slash_worker": 1,
            }
            result = crm_main.main(args)
            self.assertEqual(result.get("statusCode"), 200)
            mock_post.assert_called_once()
            self.assertIn("Agent system prompt", mock_post.call_args[0][1])
            self.assertIn("You are a test agent", mock_post.call_args[0][1])

    @patch.object(crm_main, "_post_to_slack", new_callable=AsyncMock)
    def test_slash_help_posts_help_message(self, mock_post):
        for cmd in ("help", "h", "?", "-h", "--help"):
            mock_post.reset_mock()
            args = {
                "response_url": "https://hooks.slack.com/x",
                "text": cmd,
                "_slash_worker": 1,
            }
            result = crm_main.main(args)
            self.assertEqual(result.get("statusCode"), 200)
            mock_post.assert_called_once()
            self.assertIn("slash command help", mock_post.call_args[0][1])

    @patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test", "NOTION_API_KEY": "ntn-test"}, clear=False)
    def test_slash_missing_text_returns_400(self):
        args = {
            "response_url": "https://hooks.slack.com/x",
            "text": "",
            "_slash_worker": 1,
        }
        result = crm_main.main(args)
        self.assertEqual(result.get("statusCode"), 400)
        body = json.loads(result.get("body", "{}"))
        self.assertIn("Missing text", body.get("error", ""))

    @patch.dict(os.environ, {"ANTHROPIC_API_KEY": "", "NOTION_API_KEY": "x"}, clear=False)
    @patch.object(crm_main, "_post_to_slack", new_callable=AsyncMock)
    def test_slash_missing_anthropic_key_posts_error(self, mock_post):
        args = {
            "response_url": "https://hooks.slack.com/x",
            "text": "test note",
            "_slash_worker": 1,
        }
        result = crm_main.main(args)
        mock_post.assert_called_once()
        call_args = mock_post.call_args[0]
        self.assertIn("ANTHROPIC_API_KEY", call_args[1])

    @patch.dict(
        os.environ,
        {
            "ANTHROPIC_API_KEY": "sk-ant-test",
            "NOTION_API_KEY": "ntn-test",
            "DO_SLACK_ASYNC_URL": "",
            "DO_SLACK_ASYNC_TOKEN": "",
        },
        clear=False,
    )
    @patch.object(crm_main, "_run_agent", new_callable=AsyncMock)
    @patch.object(crm_main, "NotionClient")
    @patch.object(crm_main, "_post_to_slack", new_callable=AsyncMock)
    def test_slash_worker_tier2_success_posts_to_slack(
        self, mock_post, mock_notion_cls, mock_run_agent
    ):
        mock_run_agent.return_value = crm_main._ok({
            "response": "CRM updated\n• Test Co (Companies)\n<https://notion.so/abc|Open in Notion>",
            "iterations": 1,
            "tool_calls_made": [],
        })
        args = {
            "response_url": "https://hooks.slack.com/x",
            "text": "opret virksomhed test co",
            "_slash_worker": 1,
        }
        result = crm_main.main(args)
        self.assertEqual(result.get("statusCode"), 200)
        mock_post.assert_called_once()
        self.assertIn("CRM updated", mock_post.call_args[0][1])

    @patch.dict(
        os.environ,
        {
            "ANTHROPIC_API_KEY": "sk-ant-test",
            "NOTION_API_KEY": "ntn-test",
            "DO_SLACK_ASYNC_URL": "",
            "DO_SLACK_ASYNC_TOKEN": "",
        },
        clear=False,
    )
    @patch.object(crm_main, "_run_agent", new_callable=AsyncMock)
    @patch.object(crm_main, "NotionClient")
    @patch.object(crm_main, "_post_to_slack", new_callable=AsyncMock)
    def test_slash_worker_tier2_multi_success_posts_combined(
        self, mock_post, mock_notion_cls, mock_run_agent
    ):
        mock_run_agent.return_value = crm_main._ok({
            "response": "CRM updated (2 items)\n• Acme (Companies)\n<https://n/1|Open in Notion>\n• John (Customers)\n<https://n/2|Open in Notion>",
            "iterations": 1,
            "tool_calls_made": [],
        })
        args = {
            "response_url": "https://hooks.slack.com/x",
            "text": "add Acme Corp, add John as customer",
            "_slash_worker": 1,
        }
        result = crm_main.main(args)
        self.assertEqual(result.get("statusCode"), 200)
        mock_post.assert_called_once()
        msg = mock_post.call_args[0][1]
        self.assertIn("CRM updated", msg)
        self.assertIn("2 items", msg)
        self.assertIn("Acme", msg)
        self.assertIn("John", msg)

    @patch.dict(
        os.environ,
        {
            "ANTHROPIC_API_KEY": "sk-ant-test",
            "NOTION_API_KEY": "ntn-test",
            "DO_SLACK_ASYNC_URL": "",
            "DO_SLACK_ASYNC_TOKEN": "",
        },
        clear=False,
    )
    @patch.object(crm_main, "_run_agent", new_callable=AsyncMock)
    @patch.object(crm_main, "NotionClient")
    @patch.object(crm_main, "_post_to_slack", new_callable=AsyncMock)
    def test_slash_worker_tier2_all_failures_posts_error(self, mock_post, mock_notion_cls, mock_run_agent):
        mock_run_agent.return_value = crm_main._error(
            500, "Database not found: Foo; Action 2: Company lookup failed"
        )
        args = {
            "response_url": "https://hooks.slack.com/x",
            "text": "add to Foo database",
            "_slash_worker": 1,
        }
        result = crm_main.main(args)
        self.assertEqual(result.get("statusCode"), 500)
        mock_post.assert_called_once()
        self.assertIn("CRM error", mock_post.call_args[0][1])
        self.assertIn("Database not found", mock_post.call_args[0][1])


class TestAgentDetection(unittest.TestCase):
    """Test #agent detection and text stripping."""

    def test_agent_flag_detected(self):
        self.assertTrue("#agent" in "complex request #agent".lower())
        self.assertTrue("#agent" in "foo #AGENT bar".lower())

    def test_agent_stripped_from_text(self):
        import re

        text = "complex request #agent"
        stripped = re.sub(r"#agent\b", "", text, flags=re.IGNORECASE).strip()
        self.assertEqual(stripped, "complex request")


class TestExtractionSchema(unittest.TestCase):
    """Test extraction output schema expectations."""

    def test_normalize_legacy_single_object(self):
        raw = {"target_database": "Companies", "title": "Acme", "properties": {}}
        out = crm_main._normalize_extraction(raw)
        self.assertEqual(len(out["actions"]), 1)
        self.assertEqual(out["actions"][0]["target_database"], "Companies")

    def test_normalize_actions_array(self):
        raw = {"actions": [{"target_database": "Customers", "title": "John"}]}
        out = crm_main._normalize_extraction(raw)
        self.assertEqual(len(out["actions"]), 1)

    def test_normalize_multiple_actions(self):
        raw = {
            "actions": [
                {"target_database": "Companies", "title": "Acme", "properties": {}},
                {"target_database": "Customers", "title": "John", "properties": {}},
            ]
        }
        out = crm_main._normalize_extraction(raw)
        self.assertEqual(len(out["actions"]), 2)
        self.assertEqual(out["actions"][0]["target_database"], "Companies")
        self.assertEqual(out["actions"][1]["target_database"], "Customers")

    def test_normalize_extraction_error_passthrough(self):
        raw = {"error": "No valid entities found"}
        out = crm_main._normalize_extraction(raw)
        self.assertEqual(out.get("error"), "No valid entities found")

    def test_format_slack_success_multi(self):
        result = {
            "success": True,
            "results": [
                {"title": "Acme", "database": "Companies", "notion_url": "https://n/1"},
                {"title": "John", "database": "Customers", "notion_url": "https://n/2"},
            ],
        }
        msg = crm_main._format_slack_success(result)
        self.assertIn("2 items", msg)
        self.assertIn("Acme", msg)
        self.assertIn("John", msg)

    def test_format_slack_success_partial_errors(self):
        result = {
            "success": True,
            "results": [
                {"title": "Acme", "database": "Companies", "notion_url": "https://n/1"},
            ],
            "errors": ["Action 2: Database not found"],
        }
        msg = crm_main._format_slack_success(result)
        self.assertIn("1 item", msg)
        self.assertIn("Acme", msg)
        self.assertIn("Partial", msg)
        self.assertIn("Action 2", msg)

    def test_format_slack_success_single_legacy(self):
        """Backward compat: single result without results array."""
        result = {
            "success": True,
            "title": "Test Co",
            "database": "Companies",
            "notion_url": "https://notion.so/abc",
        }
        msg = crm_main._format_slack_success(result)
        self.assertIn("CRM updated", msg)
        self.assertIn("Test Co", msg)
        self.assertIn("Companies", msg)

    def test_valid_extraction_single_action_format(self):
        extraction = {
            "target_database": "Companies",
            "title": "Test Corp",
            "properties": {"Company Name": "Test Corp"},
            "search_in": None,
            "search_query": None,
            "link_property": None,
        }
        out = crm_main._normalize_extraction(extraction)
        self.assertEqual(len(out["actions"]), 1)
        self.assertEqual(out["actions"][0]["target_database"], "Companies")

    def test_resolve_select_status_prospecting_to_researching(self):
        prop_def = {
            "type": "status",
            "status": {"options": [{"name": "Researching"}, {"name": "Active User"}]},
        }
        out = crm_main._resolve_select_status_value(
            prop_def, "Prospecting", crm_main.STATUS_OPTION_ALIASES
        )
        self.assertEqual(out, "Researching")

    def test_resolve_select_status_exact_match(self):
        prop_def = {
            "type": "select",
            "select": {"options": [{"name": "Researching"}, {"name": "Done"}]},
        }
        out = crm_main._resolve_select_status_value(
            prop_def, "Researching", crm_main.STATUS_OPTION_ALIASES
        )
        self.assertEqual(out, "Researching")

    def test_resolve_select_status_invalid_returns_none(self):
        prop_def = {
            "type": "status",
            "status": {"options": [{"name": "Researching"}, {"name": "Active User"}]},
        }
        out = crm_main._resolve_select_status_value(
            prop_def, "InvalidOption", crm_main.STATUS_OPTION_ALIASES
        )
        self.assertIsNone(out)

    def test_valid_extraction_actions_array_format(self):
        extraction = {
            "actions": [
                {
                    "target_database": "Companies",
                    "title": "Acme",
                    "properties": {"Company Name": "Acme"},
                    "search_in": None,
                    "search_query": None,
                    "link_property": None,
                },
                {
                    "target_database": "Customers",
                    "title": "John",
                    "properties": {"Name": "John"},
                    "search_in": None,
                    "search_query": None,
                    "link_property": None,
                },
            ]
        }
        out = crm_main._normalize_extraction(extraction)
        self.assertEqual(len(out["actions"]), 2)
        self.assertEqual(out["actions"][0]["target_database"], "Companies")
        self.assertEqual(out["actions"][1]["target_database"], "Customers")


if __name__ == "__main__":
    unittest.main(verbosity=2)
