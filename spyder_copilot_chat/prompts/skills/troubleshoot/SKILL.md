---
name: troubleshoot
description: Investigate unexpected AI agent behavior by analyzing debug logs. Use when users ask why something happened, why a request was slow, why tools were used or skipped, or why instructions/skills/agents did not load.
---

# Troubleshoot

Investigate and explain unexpected chat agent behavior using log files.

Use this skill for questions like:
- Why did this request take so long?
- Why was a tool or subagent called?
- Why did instruction/skill/agent files not load?
- Why was a tool call blocked or failed?
- Why did the model not follow expectations?

## Data Source

Check these log locations:
- `~/.spyder_copilot_chat/logs/` — Plugin debug logs
- `~/.spyder_copilot_chat/sessions/` — Session chat history
- Spyder's internal log: Help → Spyder log file

## Analysis Steps

1. Read the relevant log file(s)
2. Look for ERROR or WARNING entries
3. Trace the request flow: input → tool calls → response
4. Identify the root cause
5. Suggest fixes or workarounds
