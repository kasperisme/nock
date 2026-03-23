---
name: feedback-context
description: Guides the agent for /nock #feedback mode. Use when the user wants to update the CRM Settings/Context page with feedback or instructions.
---

# Feedback Context Mode (#feedback)

## When This Applies

- User sends `/nock <feedback> #feedback`
- Only two tools available: `get_settings_context`, `update_settings_context`
- Goal: merge user feedback into the existing context without losing content

## Workflow (Strict Order)

1. **Read first**: Call `get_settings_context` to fetch the current Settings/Context page content.
2. **Merge**: Produce updated text that:
   - Incorporates the user's feedback (new instructions, emphasis, corrections)
   - Preserves existing content that is still valid
   - Keeps structure (sections, bullets) where appropriate
3. **Write once**: Call `update_settings_context` with the *complete* updated context. Do not pass partial text.
4. **Confirm**: In your response, briefly state what was changed.

## Rules

- Never overwrite with only the new feedback. Always merge.
- If the user says "add X" or "include Y", append or integrate into the relevant section.
- If the user says "remove X" or "stop doing Y", edit the existing text accordingly.
- If the user says "focus more on Z", strengthen or add emphasis to that topic in the context.
