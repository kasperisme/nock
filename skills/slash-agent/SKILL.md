---
name: slash-agent
description: Guides the agent for full /nock #agent mode. Use when the user adds #agent to their message and expects research, multi-step workflows, or complex CRM operations.
---

# Slash Agent Mode (#agent)

## When This Applies

- User sends `/nock <request> #agent`
- Full tool access: Notion CRUD, research (Perplexity), multi-step reasoning
- Response posted to Slack; can be longer than quick updates

## Behavior

### Questions vs Actions

- **Questions** (e.g. "What's our history with Acme?", "Tell me about John"): Use tools to fetch CRM data. Do NOT create or update records unless explicitly asked. Answer from fetched data only.
- **Actions** (e.g. "Add Acme as company", "Research competitors and log to CRM"): Create/update records. Use research tool when the request implies external lookup.

### Research Workflow

- When the user asks for market research, company info, competitor analysis, or similar: use the `research` tool.
- When creating/updating records from research: include citation URLs in Notes, Summary, or Sources so data is traceable.

### Multi-Step

- Create parent records first (Company → Customer → Interaction/Outreach).
- Search before create for every entity. Never duplicate.
- Summarize all steps in the final response.

### Notion Links

- Always include the Notion URL for every record created or updated. Use the `notion_url` field returned by the tool result.
- Format as a markdown link, e.g. `[Acme Corp](https://notion.so/...)`.

### Response Length

- Longer than quick updates is fine, but keep it actionable. 2–5 sentences typical. No lengthy essays.
