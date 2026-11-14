---
name: install-vscode-extension
description: "How to install a Spyder plugin from source or PyPI"
---

# Installing Spyder Plugins

1. Spyder plugins are Python packages with entry points under `spyder.plugins`.
2. To install a Spyder plugin from PyPI: `pip install <plugin-name>`
3. To install from source: `pip install -e .` in the plugin directory
4. Restart Spyder after installation
5. Enable the plugin in: Tools → Preferences → Plugins
