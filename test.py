#!/usr/bin/env python3
"""
Smoke tests for Notion CRM DO Functions.
Pulls function URLs from doctl, reads API_SECRET from .env, then runs requests.

Usage:
  python test.py            # run all tests
  python test.py --agent    # also run the agent test (costs tokens)
"""

import json
import os
import subprocess
import sys
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).parent
ENV_FILE = SCRIPT_DIR / ".env"

FUNCTIONS = ["crm"]

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
BOLD = "\033[1m"
RESET = "\033[0m"

run_agent_test = "--agent" in sys.argv

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def load_env(path: Path) -> Dict[str, str]:
    env: Dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip()
    return env


def get_function_url(name: str) -> Optional[str]:
    try:
        result = subprocess.run(
            ["doctl", "serverless", "functions", "get", f"notion-crm/{name}", "--url"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            print(f"{RED}doctl error for {name}: {result.stderr.strip()}{RESET}")
            return None
        return result.stdout.strip()
    except FileNotFoundError:
        print(f"{RED}ERROR: doctl not found. Install from https://docs.digitalocean.com/reference/doctl/{RESET}")
        sys.exit(1)
    except subprocess.TimeoutExpired:
        print(f"{RED}doctl timed out getting URL for {name}{RESET}")
        return None


def request(
    url: str,
    body: Dict,
    secret: Optional[str] = None,
) -> Tuple[int, Any]:
    data = json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    if secret:
        headers["Authorization"] = f"Bearer {secret}"

    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        # 120s to allow cold start (Python + virtualenv + deps can take 30–60s)
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read().decode()
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                return resp.status, raw
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, raw
    except urllib.error.URLError as exc:
        return 0, str(exc.reason)


def check(label: str, status: int, body: Any, expect_key: Optional[str] = None) -> bool:
    ok = 200 <= status < 300
    if ok and expect_key:
        if isinstance(body, dict):
            ok = expect_key in body
        else:
            ok = False

    icon = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
    print(f"  [{icon}] {label}  (HTTP {status})")
    if not ok:
        snippet = json.dumps(body, default=str)[:200] if isinstance(body, dict) else str(body)[:200]
        print(f"         {YELLOW}{snippet}{RESET}")
    return ok


# ---------------------------------------------------------------------------
# Test suites
# ---------------------------------------------------------------------------


def test_databases(url: str, secret: Optional[str]) -> int:
    print(f"\n{BOLD}databases{RESET}  {url}")
    fails = 0

    status, body = request(url, {"action": "list_databases"}, secret)
    if not check("list_databases", status, body, expect_key="databases"):
        fails += 1

    status, body = request(url, {"action": "get_database", "database_id": "nonexistent-id-000"}, secret)
    # Expect a 4xx from Notion (bad ID), not a 500 or network error
    ok = 400 <= status < 500
    icon = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
    print(f"  [{icon}] get_database (bad id → 4xx)  (HTTP {status})")
    if not ok:
        fails += 1

    status, body = request(url, {"action": "unknown_action"}, secret)
    ok = status == 400
    icon = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
    print(f"  [{icon}] unknown action → 400  (HTTP {status})")
    if not ok:
        fails += 1

    return fails


def test_pages(url: str, secret: Optional[str]) -> int:
    print(f"\n{BOLD}pages{RESET}  {url}")
    fails = 0

    # get_schema is a local lookup — no Notion API call, always safe
    status, body = request(url, {"action": "get_schema", "database_name": "customers"}, secret)
    if not check("get_schema customers", status, body, expect_key="property_email"):
        fails += 1

    status, body = request(url, {"action": "get_schema", "database_name": "outreach"}, secret)
    if not check("get_schema outreach", status, body, expect_key="property_channel"):
        fails += 1

    status, body = request(url, {"action": "get_schema", "database_name": "nonexistent"}, secret)
    ok = status == 404
    icon = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
    print(f"  [{icon}] get_schema unknown db → 404  (HTTP {status})")
    if not ok:
        fails += 1

    status, body = request(url, {"action": "get_page", "page_id": "00000000-0000-0000-0000-000000000000"}, secret)
    ok = 400 <= status < 500
    icon = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
    print(f"  [{icon}] get_page (bad id → 4xx)  (HTTP {status})")
    if not ok:
        fails += 1

    # Missing required params
    status, body = request(url, {"action": "update_page"}, secret)
    ok = status == 400
    icon = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
    print(f"  [{icon}] update_page missing params → 400  (HTTP {status})")
    if not ok:
        fails += 1

    return fails


def test_agent(url: str, secret: Optional[str]) -> int:
    print(f"\n{BOLD}agent{RESET}  {url}")
    fails = 0

    # Missing prompt
    status, body = request(url, {}, secret)
    ok = status == 400
    icon = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
    print(f"  [{icon}] missing prompt → 400  (HTTP {status})")
    if not ok:
        fails += 1

    if run_agent_test:
        print(f"  {YELLOW}Running agent test (costs tokens)...{RESET}")
        status, body = request(
            url,
            {"prompt": "List all accessible databases. Do not create or update anything."},
            secret,
        )
        if not check("agent run (list only)", status, body, expect_key="response"):
            fails += 1
    else:
        print(f"  [    ] agent run skipped (pass --agent to enable)")

    return fails


def test_events(url: str) -> int:
    """Smoke tests for Slack Events API endpoint (no Bearer auth — uses signature auth)."""
    print(f"\n{BOLD}slack events{RESET}  {url}")
    fails = 0

    # url_verification: challenge must be echoed back (no auth required)
    challenge = "test-challenge-abc123"
    status, body = request(url, {"type": "url_verification", "challenge": challenge})
    ok = status == 200 and isinstance(body, dict) and body.get("challenge") == challenge
    icon = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
    print(f"  [{icon}] url_verification → echoes challenge  (HTTP {status})")
    if not ok:
        fails += 1

    # event_callback with no X-Slack-Signature:
    #   - 200 if SLACK_SIGNING_SECRET is unset (dev mode, signature check skipped)
    #   - 401 if SLACK_SIGNING_SECRET is set (unsigned request rejected)
    # Both are correct behaviour — just verify we don't get a 500.
    status, body = request(url, {
        "type": "event_callback",
        "team_id": "T_SMOKE_TEST",
        "event": {
            "type": "message",
            "bot_id": "B_BOT",
            "text": "automated bot message",
            "channel": "C_SMOKE",
            "ts": "1700000000.000001",
        },
    })
    ok = status in (200, 401)
    icon = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
    detail = "200 (secret unset)" if status == 200 else "401 (secret set, sig rejected)"
    print(f"  [{icon}] event_callback no-sig → {detail}  (HTTP {status})")
    if not ok:
        fails += 1

    return fails


def test_slash(url: str, secret: Optional[str]) -> int:
    """Smoke test for Slack slash command routing (multi-action pattern)."""
    print(f"\n{BOLD}slash command{RESET}  {url}")
    fails = 0

    # Missing text with response_url → 400
    status, body = request(
        url,
        {"response_url": "https://hooks.slack.com/placeholder"},
        secret,
    )
    ok = status == 400
    icon = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
    print(f"  [{icon}] missing text → 400  (HTTP {status})")
    if not ok:
        fails += 1

    # Help command → 200 (no API calls, ephemeral response)
    status, body = request(
        url,
        {
            "response_url": "https://hooks.slack.com/placeholder",
            "text": "help",
            "_slash_worker": 1,
        },
        secret,
    )
    ok = status == 200
    icon = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
    print(f"  [{icon}] help → 200  (HTTP {status})")
    if not ok:
        fails += 1

    return fails


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    env = load_env(ENV_FILE)
    secret = env.get("API_SECRET") or None

    print(f"{BOLD}Fetching function URLs from doctl...{RESET}")
    urls = {name: get_function_url(name) for name in FUNCTIONS}

    missing = [name for name, url in urls.items() if not url]
    if missing:
        print(f"{RED}Could not get URLs for: {missing}{RESET}")
        print("Make sure you have deployed: ./deploy.sh")
        sys.exit(1)

    for name, url in urls.items():
        print(f"  {name}: {url}")

    url = urls["crm"]
    total_fails = 0
    total_fails += test_databases(url, secret)
    total_fails += test_pages(url, secret)
    total_fails += test_agent(url, secret)
    total_fails += test_slash(url, secret)
    total_fails += test_events(url)

    print()
    if total_fails == 0:
        print(f"{GREEN}{BOLD}All tests passed.{RESET}")
    else:
        print(f"{RED}{BOLD}{total_fails} test(s) failed.{RESET}")
        sys.exit(1)


if __name__ == "__main__":
    main()
