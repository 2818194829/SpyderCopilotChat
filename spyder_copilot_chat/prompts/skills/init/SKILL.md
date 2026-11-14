---
name: init
description: Generate or update chat customization files for AI coding agents
argument-hint: Optionally specify a focus area or pattern to document for agents
---

The purpose of this command is to create or update chat customization files:
- Agent instructions file (`.github/copilot-instructions.md` or `AGENTS.md`)
- Skills and custom agents to automate common tasks or enforce conventions

The user can optionally call this with an argument specifying a focus area.

## Workflow

1. **Discover existing conventions**
   Search for: `**/{.github/copilot-instructions.md,AGENT.md,AGENTS.md,README.md}`

2. **Explore the codebase**
   Find essential knowledge that helps an AI agent be immediately productive:
   - Build/test commands
   - Architecture decisions and component boundaries
   - Project-specific conventions

3. **Create/update customization files**
   Use the `agent-customization` skill for detailed file format information.

When complete, print a table of added or modified files with explanations.
