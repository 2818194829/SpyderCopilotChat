---
name: create-prompt
description: "Create a reusable prompt file (.prompt.md) for a common task."
argument-hint: What task should this prompt help with?
---
Related skill: `agent-customization`.

Guide the user to create a `.prompt.md`.

## Extract from Conversation
Review the conversation history. If the user has been working on a repeatable task pattern, generalize that into a reusable prompt. Extract:
- The core task being performed repeatedly
- Any implicit inputs (selected code, file type, context)
- The desired output format or style

## Clarify if Needed
- What task should this prompt help with?
- Should it take arguments or use fixed context?
- Workspace-scoped or personal?

## Iterate
1. Draft the prompt and save it
2. Identify ambiguous parts and clarify
3. Summarize and propose related customizations
