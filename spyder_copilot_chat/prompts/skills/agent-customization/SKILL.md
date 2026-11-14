---
name: agent-customization
description: "Create, update, review, fix, or debug Spyder AI agent customization files (.instructions.md, .prompt.md, .agent.md, SKILL.md, copilot-instructions.md, AGENTS.md). USE FOR: saving coding preferences; troubleshooting why instructions/skills/agents are ignored; configuring applyTo patterns; defining tool restrictions; creating custom agent modes or specialized workflows; packaging domain knowledge; fixing YAML frontmatter syntax."
---

# Agent Customization

## Decision Flow

| Primitive | When to Use |
|-----------|-------------|
| Agent Instructions | Always-on, applies everywhere in the project |
| File Instructions | Explicit via `applyTo` patterns, or on-demand via `description` |
| MCP | Integrates external systems, APIs, or data |
| Skills | Reusable workflows combining tools + domain knowledge |
| Prompts | Reusable prompt templates for common tasks |
| Agents | Specialized personas with tool restrictions |

## File Types

### `.instructions.md` — File Instructions
- Apply via `applyTo` glob patterns in YAML frontmatter
- Or activate on-demand via `description` field
- Example: `applyTo: ["**/*.py"]` for Python-specific rules

### `.agent.md` — Custom Agents
- Define a specialized AI persona with tool restrictions
- YAML frontmatter: `name`, `description`, `tools` (allow/deny lists)

### `SKILL.md` — Agent Skills
- Package multi-step workflows with domain knowledge
- YAML frontmatter: `name`, `description`, `argument-hint`

### `.prompt.md` — Prompt Templates
- Reusable prompt patterns for common tasks
- YAML frontmatter: `name`, `description`, `argument-hint`

### `copilot-instructions.md` / `AGENTS.md` — Project Instructions
- Always-on instructions loaded into every agent session
- Place in project root or `.github/` directory
