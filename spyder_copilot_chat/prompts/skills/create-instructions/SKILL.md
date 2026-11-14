---
name: create-instructions
description: "Create an instructions file (.instructions.md) for a project rule or convention."
argument-hint: What rule or convention to enforce?
---
Related skill: `agent-customization`.

Guide the user to create an instructions file.

## Extract from Conversation
Review the conversation history. If the user has been correcting the agent's output or asking for specific patterns, generalize that into a persistent instruction. Extract:
- Corrections or preferences mentioned during the conversation
- Coding patterns the user enforced or requested
- Project-specific conventions referenced

## Clarify if Needed
- What convention should be enforced?
- Which files does it apply to? (applyTo patterns)
- Always-on or on-demand?

## Iterate
1. Draft the instructions file with proper YAML frontmatter
2. Set `applyTo` patterns (e.g., `["**/*.py"]`, `["**/*.js"]`)
3. Summarize and propose next customizations
