---
name: research-citations
description: Ensures research tool results are properly cited when creating or updating CRM records. Use when the agent uses the research tool to populate Notion pages.
---

# Research Citations

## When This Applies

- The agent has used the `research` tool and received content + `citations` array
- Creating or updating a Company, Customer, Interaction, or other record with data from research

## Rule

**Always add citation URLs to the record.** The research tool returns a `citations` array of source URLs. When populating CRM fields (Notes, Summary, Sources, References) from research:

1. Include the relevant URLs in the record so the data is traceable.
2. Prefer a "Sources" or "References" field if the schema has one.
3. Otherwise append to Notes or Summary, e.g. `Sources: [url1] [url2]`.

## Example

Research returns company overview + citations `["https://...", "https://..."]`. When creating the Company page, add to Notes: `[company summary]\n\nSources: https://... https://...`
