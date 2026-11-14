---
name: chronicle
description: Analyze AI chat session history for standup reports, usage tips, and session reindexing. Use when the user asks for a standup, daily summary, usage tips, workflow recommendations, or wants to manage session data.
---

# Chronicle

Analyze the user's AI chat session history. This skill handles standup reports, usage analysis, and session store maintenance.

Sessions are stored locally in `~/.spyder_copilot_chat/sessions/`.

## Workflows

### Standup

When the user asks for a standup, daily summary, or "what did I do":

1. Read session files from `~/.spyder_copilot_chat/sessions/`
2. Group sessions by date and topic
3. Format as a standup report:

```
Standup for <date>:

**✅ Done**
- Task description (files changed, key decisions)
- Session: <session-id>

**🚧 In Progress**
- Current work description
- Session: <session-id>
```

### Tips

Analyze usage patterns and suggest:
- Frequently used commands that could be saved as prompts
- Repetitive workflows that could be skills
- Productivity improvements based on chat patterns
