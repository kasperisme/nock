---
name: slack-event-scoring
description: Scores Slack channel messages for CRM relevance. Used by the Events API to decide whether to offer "Log it to the CRM" or ask a clarifying question. Not used by the agent.
---

# Slack Event Scoring

Score a Slack message for CRM relevance. Return JSON only: `{"score": 0.0-1.0, "reason": "brief", "entities": []}`.

## Relevant (higher score)

- Company names, customer names, contact names
- Deal words: proposal, contract, demo, pricing, follow-up, meeting, call, email
- Deal stages: negotiation, closing, onboarding
- CRM-worthy updates: "signed with X", "John from Acme called", "sent proposal to Y"

## Not relevant (lower score)

- General chat, jokes, off-topic
- Internal coordination ("let's sync at 3pm")
- Unrelated links or announcements
- No entities or deal context

## Output

- **score**: 0.0–1.0. Use 0.7+ for clearly relevant, 0.4–0.7 for borderline, &lt;0.4 for off-topic.
- **reason**: One short sentence explaining why (e.g. "Mentions company Acme and call outcome").
- **entities**: Array of extracted names (companies, people) for display in the confirmation UI.
