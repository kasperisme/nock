---
name: slash-quick-update
description: Guides the agent for quick /nock slash updates without #agent. Use when the user sends a short note (e.g. "John called", "Add Acme Corp") and expects fast, minimal CRM updates.
---

# Slash Quick Update

## When This Applies

- User sends `/nock <note>` without `#agent` or `#feedback`
- Short, free-form text describing one or more CRM actions
- Response will be posted directly to Slack

## Behavior

1. **Batch extraction**: If the note mentions multiple entities (e.g. "Add Acme, add John, log call with John"), extract and process all in one pass. One search per entity, then create or update.
2. **Brevity**: Keep the final response to 1–3 sentences. Users expect a quick summary, not prose.
3. **No research**: Do not use the research tool for quick updates. Only create/update from the note content.
4. **Format**: End with a clear summary: what was created, updated, or linked. Always include the Notion URL for every record created or updated — use the `notion_url` field from the tool result.

## Examples

- Input: "John from Acme called, interested in pricing" → Create/update Acme, John, Interaction; respond: "Logged call with John (Acme). Created Acme Corp and John Smith. [View →](https://notion.so/...)"
- Input: "Add Acme Corp, add John as customer" → Two creates; respond: "Added Acme Corp and John Smith. [Acme](https://notion.so/...) · [John](https://notion.so/...)"
