---
name: create-agent
description: "Create a custom agent (.agent.md) for a specific job."
argument-hint: What job should this agent do and how?
---
Related skill: `agent-customization`. Load and follow **agents.md** for template and principles.

Guide the user to create an `.agent.md`.

## Extract from Conversation
Review the conversation history. If the user has been using the agent in a specialized way, generalize that into a custom agent. Extract:
- The specialized role or persona being assumed
- Tool preferences (which to use, which to avoid)
- The domain or job scope

## Clarify if Needed
If no clear specialization emerges from the conversation, clarify:
- What job should this agent do?
- When should it be picked over the default agent?
- Which tools should it use (or avoid)?

## Iterate
1. Draft the agent file and save it.
2. Identify the most ambiguous parts and ask about those.
3. Summarize, suggest example prompts, and propose related customizations.
