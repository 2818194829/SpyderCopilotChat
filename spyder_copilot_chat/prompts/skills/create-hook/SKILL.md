---
name: create-hook
description: "Create a hook (.json) to enforce policy or automate agent lifecycle events."
argument-hint: What should be enforced or automated?
---
Related skill: `agent-customization`.

Guide the user to create a hook in `.github/hooks/` or project config.

## Extract from Conversation
Review the conversation history. If the user has expressed concerns about agent behavior (e.g., "don't run this command", "always check before doing X"), generalize that into a hook.

Hook event types:
- `PreToolUse` — Before a tool is executed
- `SessionStart` — When a new session begins
- `Stop` — When agent should stop
- `PostToolUse` — After a tool completes

## Clarify if Needed
- What event should trigger this hook?
- Should it block, warn, or inject context?
- Does it need a companion script?

## Iterate
1. Draft the hook JSON and save it
2. Identify ambiguous parts and clarify
3. Summarize and suggest testing approach
