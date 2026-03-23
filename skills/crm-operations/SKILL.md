---
name: crm-operations
description: Guides the CRM agent on Notion-specific conventions, duplicate prevention, and field formatting. Use when creating or updating CRM records, handling company/customer data, or reconciling Notion database entries.
---

# CRM Operations Skill

## Notion Field Formatting

- **Text fields**: Pass plain strings only. Never pass `{"rich_text": [...]}` structures.
- **Relations**: Pass comma-separated page IDs as a string, or a list of IDs.
- **Select/Status**: Use exact option names from the database schema. Common aliases: "Lead" → "Researching", "Prospect" → "Researching".

## Duplicate Prevention

1. Before creating any Company or Customer, call `get_database_pages` with `title_search` for that name.
2. If a match exists (case-insensitive, normalized), use `update_page` with the existing `page_id`.
3. One search per entity type. Never create without searching first.

## Tool Use Is Mandatory for Actions

- NEVER claim to have created, updated, or logged anything without first receiving a successful tool result confirming it.
- If the user asks you to add, create, log, or update something, you MUST call the appropriate tool (`create_page`, `update_page`, etc.) and wait for the result before reporting success.
- Your final response must only describe actions confirmed by actual tool results. Never fabricate an outcome you have not executed.
- For every record created or updated, the tool result includes a `notion_url` field. Always include this as a link in your response so the user can navigate directly to the record.

## Entity Hierarchy

- Create parent records first: Company before Customer, Customer before Interaction/Outreach.
- When linking: use `relation_filter_property` and `relation_filter_page_id` to fetch related records.

## Answering Questions About Entities

When asked "Tell me about X" or "What's our history with Y", fetch full CRM context before responding. Do not answer from memory.
- **Company**: Find company by name → fetch Customers linked via Company relation → fetch Interactions/Outreach linked to those customers.
- **Customer**: Find customer by name → fetch Interactions and Outreach linked via Customer relation.
- Use `get_page` for full details when needed.

## CRM Field Content

- Text fields (Notes, Message, Summary) must contain ONLY factual, relevant CRM data.
- Never write error messages, API warnings, suggestions, "next steps", or instructions into any CRM field.
- If data is unavailable, leave the field EMPTY. Put explanations in your response to the user only.

## Research Citations

When using the research tool to populate records, always add citation URLs from the tool's `citations` array into the record (Notes, Summary, or Sources field).
