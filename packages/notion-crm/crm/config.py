import json
import logging
from pathlib import Path
from typing import Any, Dict, List

MAX_ITERATIONS = 25

MODEL_AGENT = "claude-haiku-4-5"
MODEL_RELEVANCE_SCORE = "claude-haiku-4-5"
MODEL_EXTRACTION = "claude-haiku-4-5"
PERPLEXITY_MODEL = "sonar"
PERPLEXITY_URL = "https://api.perplexity.ai/chat/completions"

SKILLS_DIRS = [
    Path(__file__).resolve().parent / "skills",
    Path(__file__).resolve().parent.parent.parent.parent / "skills",
]

GENERIC_SYSTEM_PROMPT = """You are a CRM operations agent. The skills above provide detailed guidance for Notion conventions, duplicate prevention, field formatting, and context-specific behavior.

Core principles:
- Prefer taking action with tools over suggesting actions.
- Use tools for create, update, log, reconcile, or research when the request implies it.
- Never invent facts. Only use information from the user request, context, or tool results.
- Convert messy context into short, structured CRM entries. Summarize what was created, updated, and linked.

CRITICAL — tool use is mandatory for actions:
- NEVER claim to have created, updated, or logged anything without first receiving a successful tool result confirming it.
- If the user asks you to add, create, log, or update something, you MUST call the appropriate tool (create_page, update_page, etc.) and wait for the result before reporting success.
- Your final response must only describe actions confirmed by actual tool results. Do not describe an outcome you have not executed."""

REGULAR_AGENT_SYSTEM_ADDITION = """Regular agent context: You are responding to an API or chat request.
- When done, produce a concise action summary of what you did.
- For every record created or updated, include its notion_url from the tool result as a link in your response.
- Be clear and informative in your response."""

SLASH_AGENT_SYSTEM_ADDITION = """Slack slash command context: You are responding via /nock in Slack. Your final response will be posted directly to the channel.
- If the user asks a question (e.g. "What's our history with X?", "Tell me about Y"), use tools to fetch relevant CRM data and answer directly. Do not create or update records unless explicitly asked.
- If the user requests an action (e.g. "Add Acme as company", "Log a call with John"), create or update records.
- Keep your response concise and actionable; users expect a brief summary, not lengthy prose.
- When done, produce a short summary suitable for a Slack message (1-3 sentences).
- For every record created or updated, include its notion_url from the tool result as a link in your response."""

FEEDBACK_SYSTEM_ADDITION = """Feedback mode: You are updating the CRM Settings/Context page with user feedback.
1. Call get_settings_context to read the current context.
2. Produce an updated version that incorporates the user's feedback while preserving existing content.
3. Call update_settings_context with the complete updated context text.
4. Confirm what was changed in your response."""

SYSTEM_PROMPT = GENERIC_SYSTEM_PROMPT + "\n\n---\n\n" + REGULAR_AGENT_SYSTEM_ADDITION

AGENT_PROMPT_CACHE_TTL = 300  # 5 minutes

NOTION_TOOLS: List[Dict] = [
    {
        "type": "function",
        "function": {
            "name": "get_all_databases",
            "description": (
                "List all Notion databases the integration has access to. "
                "Use this to discover database IDs before querying pages."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_database",
            "description": (
                "Get a specific Notion database by its ID, including its full property schema. "
                "Useful for understanding available fields before creating or updating pages."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "database_id": {
                        "type": "string",
                        "description": "UUID of the database (not the name)",
                    }
                },
                "required": ["database_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_database_pages",
            "description": (
                "Query pages in a Notion database. REQUIRED before create_page: use title_search to check "
                "if a record with that name already exists. If found, use update_page instead of create_page. "
                "Use relation_filter_property + relation_filter_page_id to fetch related records "
                "(e.g. all Customers where Company=page_id). Always search before creating to avoid duplicates."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "database_id": {
                        "type": "string",
                        "description": "UUID of the database",
                    },
                    "title_search": {
                        "type": "string",
                        "description": (
                            "Filter pages whose title/name contains this string (case-insensitive). "
                            "Use when searching for a specific record by name."
                        ),
                    },
                    "relation_filter_property": {
                        "type": "string",
                        "description": (
                            "Name of the relation property to filter by (e.g. Company, Customer). "
                            "Requires relation_filter_page_id. Use to get records linked to a page."
                        ),
                    },
                    "relation_filter_page_id": {
                        "type": "string",
                        "description": (
                            "Page ID to filter by when relation_filter_property is set. "
                            "Returns only pages where that relation contains this page."
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of pages to return (default: 50)",
                    },
                },
                "required": ["database_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_page",
            "description": "Get a specific Notion database page by its ID, including all its properties.",
            "parameters": {
                "type": "object",
                "properties": {
                    "page_id": {"type": "string", "description": "UUID of the page"}
                },
                "required": ["page_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_page",
            "description": (
                "Create a new page in a Notion database. MUST call get_database_pages with title_search "
                "first. If a match exists, use update_page instead — never create duplicates. "
                "Create parent records (e.g. Company) before dependent ones (e.g. Customer)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "database_id": {
                        "type": "string",
                        "description": "UUID of the target database",
                    },
                    "title": {
                        "type": "string",
                        "description": "Title / Name of the new page",
                    },
                    "properties": {
                        "type": "object",
                        "description": (
                            "Additional property values keyed by Notion property name. "
                            "Always pass simple values: plain strings for text, numbers, comma-separated IDs for relations. "
                            "Never pass Notion API structures (e.g. rich_text objects) — only the content."
                        ),
                    },
                },
                "required": ["database_id", "title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_page",
            "description": (
                "Update one or more properties on an existing Notion database page. Use this when "
                "get_database_pages found an existing record — prefer update over create to avoid duplicates. "
                "Property types are auto-detected — pass the Notion property name and the new value."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "page_id": {
                        "type": "string",
                        "description": "UUID of the page to update",
                    },
                    "properties": {
                        "type": "object",
                        "description": (
                            "Map of Notion property name -> simple value. "
                            "For text (Notes, Message, etc.): pass the plain string only. "
                            "For relations: comma-separated page IDs. "
                            'Never pass Notion API structures like {"rich_text": [...]}.'
                        ),
                    },
                },
                "required": ["page_id", "properties"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "research",
            "description": (
                "Conduct market, user, company, or other web research using Perplexity. "
                "Use this when the user asks for research (e.g. company info, market trends, "
                "competitor analysis, industry data, user demographics). Pass a clear, specific "
                "research query. Returns content and a citations array (source URLs). "
                "When creating or updating CRM records from research, include the citation URLs "
                "in the record (e.g. Notes, Summary, Sources) so the data is traceable."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "The research query to look up. Be specific and include context "
                            "(e.g. 'Market size for B2B SaaS in Nordic region 2024', "
                            "'Overview of Acme Corp competitors', 'User pain points for project management software')."
                        ),
                    }
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_user",
            "description": (
                "Ask the user a clarifying question when you need more information to complete "
                "the task. The conversation will pause and resume when they reply in Slack. "
                "Use this instead of ending your response with a question."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "The question to ask the user.",
                    }
                },
                "required": ["question"],
            },
        },
    },
]

FEEDBACK_TOOLS: List[Dict] = [
    {
        "type": "function",
        "function": {
            "name": "get_settings_context",
            "description": (
                "Read the current CRM Settings/Context page content. "
                "Use this first when updating context with feedback. Returns the full text."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_settings_context",
            "description": (
                "Replace the CRM Settings/Context page with new content. "
                "Use after incorporating feedback into the current context. "
                "Pass the complete updated context text to save."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": (
                            "The complete updated context text to save to Settings/Context. "
                            "Must incorporate the user's feedback while preserving useful existing content."
                        ),
                    }
                },
                "required": ["content"],
            },
        },
    },
]

# Confidence threshold above which Nock posts a thread confirmation.
EVENT_CONFIDENCE_THRESHOLD = 0.7
# Below this score the message is silently ignored.
# Between BORDERLINE_LOW and EVENT_CONFIDENCE_THRESHOLD Nock asks a clarifying question.
EVENT_BORDERLINE_LOW = 0.4

SLACK_HELP = """*CRM slash command help*

• `/nock <your note>` — Quick update. I'll extract companies, customers, outreach, etc. from your text and create/update Notion records.
• `/nock <note> #agent` — Full agent mode. I'll research, search, and handle complex multi-step requests.
• `/nock <feedback> #feedback` — Update Settings/Context. I'll read the current context, incorporate your feedback, and save.
• *Several actions at once* — Describe multiple items in one message (e.g. add a company, add a customer, log an interaction). I'll identify each action and process them asynchronously.
• `/nock refresh settings` — Pull all Notion databases and store schema at [root]/Settings/database. Run this first.
• `/nock prompt` — Show the agent system prompt (fetched from Notion).

Examples:
`/nock John from Acme called, interested in pricing`
`/nock Add Acme Corp, add John Smith as customer, log call with John` _(3 actions)_
`/nock Add Acme Corp as company, status: Lead #agent`
`/nock Focus more on B2B SaaS companies in our target segment #feedback`"""

SLACK_EXTRACTION_PROMPT = """Extract CRM entities from this short, free-form text. Return ONLY valid JSON, no markdown.

The text may describe ONE or SEVERAL distinct actions (e.g. "Add Acme Corp, add John Smith as customer, log a call with John").
Identify each action and return them in the "actions" array. Order matters for dependencies (e.g. create company before linking customer).

Databases: Companies, Customers, Outreach, Interactions, Product Feedback, Referrals.

Output JSON schema:
{
  "actions": [
    {
      "target_database": "Customers" | "Companies" | "Interactions" | "Outreach" | "Product Feedback" | "Referrals",
      "title": "Page title (concise)",
      "properties": { "NotionPropertyName": "value", ... },
      "search_in": "Customers" | "Companies" | null,
      "search_query": "string to search for existing record" | null,
      "link_property": "Customer" | "Company" | "Referrer" | "New Lead" | null
    }
  ]
}

Rules:
- Always return an array with at least one action. Single action → actions: [one item].
- AVOID DUPLICATES: Do not output multiple actions for the same entity. One company name = one Companies action. One person name = one Customers action. If text mentions "Acme Corp" twice, output only one Companies action for Acme Corp. Same for customers.
- For interaction notes (e.g. "John Smith called, positive"): target_database=Interactions, search_in=Customers, search_query="John Smith", link_property=Customer. Properties: Summary, Type (Call|Demo|Onboarding|Support|Feedback|Casual chat), Outcome (Positive|Neutral|Blocked|Feature request|Potential referral), Date (YYYY-MM-DD).
- For outreach (e.g. "Sent email to Jane"): target_database=Outreach, search_in=Customers, link_property=Customer. Properties: Message, Channel (Email|LinkedIn|Twitter|Community|Intro|Loom Video), Date Sent.
- For new person: target_database=Customers, no search. Properties: Name, Status, Notes. Use ONLY these Status values: Researching, Outreach Sent, Conversation, Call Booked, Onboarding, Active User, Superfan, Case Study, Referral Source. Never use Prospecting, Lead, Prospect — use Researching instead.
- For new company: target_database=Companies. Properties: Company Name, Status, Industry, etc. Company Status: use ONLY Researching, Target Account, Contacted, Active Customer, Champion Company, Case Study.
- Use today's date for Date fields when not specified.
- Keep properties minimal; only include fields with actual data."""

STATUS_OPTION_ALIASES: Dict[str, str] = {
    "Prospecting": "Researching",
    "Prospect": "Researching",
    "Lead": "Researching",
    "Leads": "Researching",
    "New Lead": "Researching",
    "Target": "Target Account",
    "Contacted": "Outreach Sent",
    "In Progress": "Conversation",
    "Meeting": "Call Booked",
    "Done": "Active User",
    "Champion": "Champion Company",
}

REGISTRY_DB_SCHEMA = {
    "Name": {"title": {}},
    "Notion ID": {"rich_text": {}},
    "Properties": {"rich_text": {}},
}

logger = logging.getLogger(__name__)


def _error(status: int, message: str) -> Dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"error": message}),
    }


def _ok(data: Any) -> Dict:
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(data, default=str),
    }


def _tools_for_claude(tools: List[Dict]) -> List[Dict]:
    """Convert tools list to Claude Messages API format (name, description, input_schema)."""
    out: List[Dict] = []
    for t in tools:
        if t.get("type") == "function" and "function" in t:
            f = t["function"]
            params = f.get(
                "parameters",
                {"type": "object", "properties": {}, "required": []},
            )
            out.append(
                {
                    "name": f["name"],
                    "description": f.get("description", ""),
                    "input_schema": params,
                }
            )
    return out


def _notion_tools_for_claude() -> List[Dict]:
    """Convert NOTION_TOOLS to Claude format."""
    return _tools_for_claude(NOTION_TOOLS)


def _feedback_tools_for_claude() -> List[Dict]:
    """Convert FEEDBACK_TOOLS to Claude format."""
    return _tools_for_claude(FEEDBACK_TOOLS)


def _load_local_skills() -> str:
    """
    Load locally hosted skills from the repo (SKILL.md files in skills/ directories).
    Returns concatenated skill content to prepend to system prompt, or empty string.
    """
    skills_content: List[str] = []
    seen: set = set()
    for skills_dir in SKILLS_DIRS:
        if not skills_dir.is_dir():
            continue
        for skill_dir in sorted(skills_dir.iterdir()):
            if not skill_dir.is_dir() or skill_dir.name == "slack-event-scoring":
                continue
            skill_file = skill_dir / "SKILL.md"
            if not skill_file.is_file():
                continue
            key = skill_file.resolve()
            if key in seen:
                continue
            seen.add(key)
            try:
                text = skill_file.read_text(encoding="utf-8")
                if text.strip():
                    skills_content.append(
                        f"## Skill: {skill_dir.name}\n\n{text.strip()}"
                    )
            except Exception as e:
                logger.warning("Could not load skill %s: %s", skill_file, e)
    if not skills_content:
        return ""
    return "\n\n---\n\n".join(skills_content) + "\n\n---\n\n"


def _load_slack_event_scoring_prompt(workspace_context: str = "") -> str:
    """
    Load the slack-event-scoring skill for CRM relevance scoring.

    If workspace_context is provided (databases + Settings/Context for this
    team), it is appended so the scorer calibrates against what THIS workspace
    actually tracks rather than generic CRM keywords.

    Returns the full system prompt string.
    """
    base = ""
    for skills_dir in SKILLS_DIRS:
        if not skills_dir.is_dir():
            continue
        skill_dir = skills_dir / "slack-event-scoring"
        skill_file = skill_dir / "SKILL.md"
        if skill_file.is_file():
            try:
                text = skill_file.read_text(encoding="utf-8")
                if text.strip():
                    base = text.strip()
                    break
            except Exception as e:
                logger.warning("Could not load slack-event-scoring skill: %s", e)
    if not base:
        base = (
            "Score this Slack message's CRM relevance 0.0–1.0. Relevant: company names, deal words "
            "(proposal, contract, demo, pricing, follow-up), contacts, deal stages. "
            'Reply JSON only: {"score": 0.0-1.0, "reason": "brief", "entities": []}'
        )
    if not workspace_context or not workspace_context.strip():
        return base
    return (
        base
        + "\n\n## Workspace context\n\n"
        + workspace_context.strip()
        + "\n\nWhen scoring, treat messages as relevant if they relate to ANY of the "
        "databases or topics listed above, not only traditional sales/deal language."
    )
