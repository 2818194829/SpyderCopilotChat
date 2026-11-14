# -*- coding: utf-8 -*-
"""斜杠命令系统 — 参考 vscode-copilot-chat 的 /fix, /explain, /tests 等命令"""

import json
import os

CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".spyder_copilot_chat")
COMMANDS_FILE = os.path.join(CONFIG_DIR, "commands.json")


# ── 内置命令 ─────────────────────────────────────────────────────────

BUILTIN_COMMANDS = [
    {
        "name": "fix",
        "description": "修复代码中的问题",
        "prompt": "修复以下代码中的所有问题和编译错误。简要解释每个修复。",
    },
    {
        "name": "explain",
        "description": "解释代码的工作原理",
        "prompt": "解释以下代码的工作原理。描述整体结构、关键逻辑以及任何不明显的部分。",
    },
    {
        "name": "tests",
        "description": "生成单元测试",
        "prompt": "为以下代码生成全面的单元测试。覆盖边界情况、典型输入和错误条件。",
    },
    {
        "name": "optimize",
        "description": "优化代码性能",
        "prompt": "优化以下代码的性能。保持可读性，解释所做的优化。",
    },
    {
        "name": "doc",
        "description": "添加文档和注释",
        "prompt": "为以下代码编写文档。添加清晰的文档字符串、参数说明和返回值说明。",
    },
    {
        "name": "simplify",
        "description": "简化代码",
        "prompt": "简化以下代码。使其更简洁、更易读、更易维护，但不改变其行为。",
    },
    {
        "name": "review",
        "description": "代码审查",
        "prompt": "审查以下代码。指出潜在问题、安全漏洞和最佳实践改进。",
    },
    {
        "name": "clear",
        "description": "清除对话历史",
        "prompt": "",
        "action": "clear",
    },
    {
        "name": "history",
        "description": "显示聊天历史列表",
        "prompt": "",
        "action": "history",
    },
    {
        "name": "plan",
        "description": "先规划再执行（Plan 模式）",
        "prompt": "请先制定一个详细的实施计划，然后再逐步执行。对于每一步，说明你要做什么以及为什么。在开始编写代码之前，先列出所有需要修改的文件和修改的要点。",
    },
    {
        "name": "review",
        "description": "代码审查 — 审查当前工作区的更改",
        "prompt": "请审查以下代码更改。检查潜在问题、安全漏洞、性能问题和代码风格。对于每个问题，给出具体的改进建议。",
    },
    {
        "name": "commit",
        "description": "生成 Git 提交信息",
        "prompt": "请为当前的 Git 更改生成一个简洁的提交信息。分析更改的内容，总结主要的修改点。",
    },
    {
        "name": "refactor",
        "description": "重构代码",
        "prompt": "重构以下代码。改善代码结构、可读性和可维护性，同时保持相同的功能行为。",
    },
]

DEFAULT_COMMANDS = [
    {"name": cmd["name"], "prompt": cmd["prompt"]}
    for cmd in BUILTIN_COMMANDS
    if cmd.get("prompt")
]


def _ensure_dir():
    os.makedirs(CONFIG_DIR, exist_ok=True)


def load_commands():
    """加载用户自定义命令"""
    try:
        with open(COMMANDS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return DEFAULT_COMMANDS.copy()


def save_commands(commands):
    """保存命令"""
    _ensure_dir()
    with open(COMMANDS_FILE, "w", encoding="utf-8") as f:
        json.dump(commands, f, indent=2, ensure_ascii=False)


def get_command_prompt(name, commands=None):
    """获取命令对应的 prompt 文本"""
    if commands is None:
        commands = load_commands()
    for cmd in commands:
        if cmd["name"] == name:
            return cmd.get("prompt", "")
    for cmd in BUILTIN_COMMANDS:
        if cmd["name"] == name:
            return cmd.get("prompt", "")
    return ""


def get_builtin_names():
    """获取内置命令名称列表（不含 action 类命令）"""
    return [c["name"] for c in BUILTIN_COMMANDS if c.get("prompt")]
