# -*- coding: utf-8 -*-
"""
聊天参与者系统 — 对标 GitHub Copilot Chat 的 @agent/@workspace/@terminal/@vscode

每个参与者有独立的系统提示词、工具集和配置覆盖。

参与者文件格式 (.participant.md):
  ---
  name: my-participant
  description: Custom participant
  tools:
    allow: ["read_file", "search_files"]
  model: gpt-4o
  ---
  You are a custom participant...
"""

import logging
import os
import re
from pathlib import Path

from qtpy.QtCore import QObject, Signal

from ..copilot_client import (
    TOOL_GROUP_READ, TOOL_GROUP_SEARCH, TOOL_GROUP_EDIT,
    TOOL_GROUP_EXECUTE, TOOL_GROUP_WEB, TOOL_GROUP_MEMORY,
    TOOL_GROUP_PLAN, TOOL_GROUP_GIT, TOOL_GROUP_REVIEW,
    TOOL_GROUP_NOTEBOOK, TOOL_GROUP_FLEET, TOOL_GROUP_EDIT2,
    TOOL_GROUP_SESSION,
)

logger = logging.getLogger(__name__)

# ── 工具集组合 ───────────────────────────────────────────────

ALL_TOOLS = (
    TOOL_GROUP_READ + TOOL_GROUP_SEARCH + TOOL_GROUP_EDIT +
    TOOL_GROUP_EXECUTE + TOOL_GROUP_WEB + TOOL_GROUP_MEMORY +
    TOOL_GROUP_PLAN + TOOL_GROUP_GIT + TOOL_GROUP_REVIEW +
    TOOL_GROUP_NOTEBOOK + TOOL_GROUP_FLEET + TOOL_GROUP_EDIT2 +
    TOOL_GROUP_SESSION
)

WORKSPACE_TOOLS = TOOL_GROUP_READ + TOOL_GROUP_SEARCH + TOOL_GROUP_MEMORY + [
    "read_project_structure", "find_test_files", "search_workspace_symbols",
    "view_image",
]

TERMINAL_TOOLS = TOOL_GROUP_READ + TOOL_GROUP_SEARCH + [
    "run_command", "execution_subagent",
]

SPYDER_TOOLS = TOOL_GROUP_READ + TOOL_GROUP_SEARCH + [
    "get_current_file",
]

TASK_TOOLS = [
    "run_command", "execution_subagent", "read_file", "search_files",
    "text_search", "list_directory", "manage_todo_list",
    "get_changed_files", "git_log", "memory_save", "memory_recall",
]

CODE_REVIEW_TOOLS = (
    TOOL_GROUP_READ + TOOL_GROUP_SEARCH + TOOL_GROUP_GIT +
    ["review_changes", "read_project_structure"]
)

PLAN_TOOLS = ALL_TOOLS

RUBBER_DUCK_TOOLS = TOOL_GROUP_READ + TOOL_GROUP_SEARCH + TOOL_GROUP_MEMORY


class Participant:
    """聊天参与者 — 拥有独立的身份、系统提示词和工具集"""

    def __init__(self, name, display_name, description, system_prompt,
                 allowed_tools=None, model_hint=None, icon="",
                 max_tool_rounds=0):
        # v0.8.0：max_tool_rounds=0 表示「跟随全局预算」（默认 200 轮 +
        # 自动续跑）。角色节制交给 allowed_tools，不再用轮数硬卡。
        self.name = name
        self.display_name = display_name
        self.description = description
        self.system_prompt = system_prompt
        self.allowed_tools = allowed_tools  # None = 全部工具
        self.model_hint = model_hint  # "fast" | "powerful" | None
        self.icon = icon
        self.max_tool_rounds = max_tool_rounds

    def filter_tools(self, all_tools):
        """根据 allowed_tools 过滤工具列表"""
        if self.allowed_tools is None:
            return list(all_tools)
        return [
            t for t in all_tools
            if t.get("function", {}).get("name") in self.allowed_tools
        ]

    def to_dict(self):
        return {
            "name": self.name,
            "display_name": self.display_name,
            "description": self.description,
            "model_hint": self.model_hint,
            "icon": self.icon,
            "max_tool_rounds": self.max_tool_rounds,
        }


class ParticipantManager(QObject):
    """参与者管理器 — 单例，管理所有参与者的注册和激活"""
    participant_changed = Signal(str)  # participant_name

    _instance = None

    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        super().__init__()
        self._participants = {}
        self._active = "agent"
        self._register_builtin()

    def _register_builtin(self):
        """注册内置参与者"""
        builtins = [
            Participant(
                name="agent",
                display_name="@agent",
                description="全功能 AI 编程助手，可以读写文件、运行命令、搜索代码",
                icon="🤖",
                allowed_tools=None,  # 所有工具
                system_prompt=(
                    "你是全功能 AI 编程助手，可以读写文件、运行命令、搜索代码、"
                    "使用 Git 操作等。使用工具完成用户的需求。"
                ),
            ),
            Participant(
                name="workspace",
                display_name="@workspace",
                description="只读模式，搜索和理解代码库，不能修改文件",
                icon="📂",
                allowed_tools=WORKSPACE_TOOLS,
                model_hint="fast",
                system_prompt=(
                    "你是一个工作区感知的编程助手（只读模式）。\n\n"
                    "**你可以：**\n"
                    "- 搜索文件、文本和代码符号\n"
                    "- 读取文件内容\n"
                    "- 列出目录结构并理解项目组织\n"
                    "- 查找测试文件和项目配置文件\n"
                    "- 用 codebase_search 进行语义搜索\n\n"
                    "**你不能：**\n"
                    "- 创建、编辑或删除文件\n"
                    "- 运行命令\n"
                    "- 使用 Git 操作\n"
                    "- 执行任何修改性操作\n\n"
                    "专注于理解代码和回答问题。"
                ),
            ),
            Participant(
                name="terminal",
                display_name="@terminal",
                description="终端命令助手，解释和运行命令",
                icon="💻",
                allowed_tools=TERMINAL_TOOLS,
                system_prompt=(
                    "你是一个终端命令助手。\n\n"
                    "**你可以：**\n"
                    "- 运行 shell 命令\n"
                    "- 读取文件来理解上下文\n"
                    "- 搜索代码库中的文件\n\n"
                    "**行为规范：**\n"
                    "- 在运行命令前总是解释它做什么\n"
                    "- 对于危险命令（rm -rf, 删除操作等），先警告用户\n"
                    "- 输出过长时进行摘要\n"
                    "- 命令失败时分析错误并提供修复建议"
                ),
            ),
            Participant(
                name="spyder",
                display_name="@spyder",
                description="Spyder IDE 开发助手",
                icon="🔬",
                allowed_tools=SPYDER_TOOLS,
                system_prompt=(
                    "你是 Spyder IDE 专家助手。\n\n"
                    "帮助用户解答关于 Spyder 6 API、插件开发、"
                    "IDE 配置和工作流的问题。\n\n"
                    "**你可以：**\n"
                    "- 读取当前编辑器和文件\n"
                    "- 搜索 Spyder 文档和代码\n"
                    "- 搜索文件系统\n\n"
                    "专注于 Spyder 相关问题。"
                ),
            ),
        ]
        for p in builtins:
            self._participants[p.name] = p

    def register(self, participant):
        """注册一个参与者"""
        self._participants[participant.name] = participant

    def get(self, name):
        """按名称获取参与者"""
        return self._participants.get(name)

    def list_participants(self):
        """列出所有参与者"""
        return list(self._participants.values())

    def set_active(self, name):
        """设置当前激活的参与者"""
        if name in self._participants:
            self._active = name
            self.participant_changed.emit(name)

    def get_active(self):
        """获取当前激活的参与者"""
        return self._participants.get(self._active)

    def get_active_name(self):
        return self._active


# ── 从消息文本中解析 @participant ──────────────────────────

PARTICIPANT_RE = re.compile(r'^@(\w+)\s+')


def parse_participant(text):
    """从消息开头解析 @participant 前缀

    Returns: (participant_name, cleaned_text) 或 (None, text)
    """
    m = PARTICIPANT_RE.match(text)
    if m:
        name = m.group(1)
        rest = text[m.end():]
        return name, rest
    return None, text
