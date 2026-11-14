---
name: project-setup-info-local
description: "Comprehensive setup steps to help create complete project structures. USE FOR: creating new projects from scratch; setting up entire project frameworks; initializing MCP servers; creating Python packages or applications. NOT FOR: single files; adding to existing projects; debugging."
---

# How to setup a project

Determine what kind of project the user wants to create, then follow the appropriate setup steps below.

## Python Package

Create structure:
```
project/
  pyproject.toml
  src/project/
    __init__.py
  tests/
    __init__.py
```

Key `pyproject.toml` sections:
- `[build-system]` — Setuptools + wheel
- `[project]` — Name, version, dependencies
- `[project.scripts]` — CLI entry points

## Spyder Plugin

Create structure:
```
plugin/
  pyproject.toml
  plugin_name/
    __init__.py
    plugin.py
    confpage.py
    widgets/
```

Key entry point: `[project.entry-points."spyder.plugins"]`

## Web App (React/Vue/etc.)

Use the framework's CLI: `npm create vite@latest`, `npx create-react-app`, etc.
