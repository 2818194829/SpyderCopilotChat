# -*- coding: utf-8 -*-
"""
自定义 Agent 管理器

对标 GitHub Copilot Chat 的 Custom Agents 系统 (.agent.md)

Agent 文件格式:
  ---
  name: python-reviewer
  description: Expert Python code reviewer
  tools:
    allow: ["read_file", "search_files"]
    deny: ["run_command"]
  model: gpt-4o
  ---
  You are a senior Python code reviewer specializing in...
"""

import logging
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)

_FRONTMATTER_RE = re.compile(r'^---\s*\n(.*?)\n---\s*\n', re.DOTALL)
_YAML_KV_RE = re.compile(r'^(\w[\w-]*)\s*:\s*(.*?)$')


def _parse_frontmatter(text):
    """解析 YAML frontmatter"""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    raw = m.group(1)
    meta = {}
    current_key = None
    for line in raw.strip().split('\n'):
        # 忽略嵌套缩进行（简化处理）
        kv = _YAML_KV_RE.match(line)
        if kv:
            key = kv.group(1).strip()
            val = kv.group(2).strip()
            if val.startswith('"') and val.endswith('"'):
                val = val[1:-1]
            elif val.startswith("'") and val.endswith("'"):
                val = val[1:-1]
            if val.startswith('[') and val.endswith(']'):
                val = [v.strip().strip('"').strip("'")
                       for v in val[1:-1].split(',') if v.strip()]
            if val == 'true':
                val = True
            elif val == 'false':
                val = False
            meta[key] = val
            current_key = key
        elif line.strip().startswith('- ') and current_key:
            # 列表项
            item = line.strip()[2:].strip().strip('"').strip("'")
            if isinstance(meta.get(current_key), list):
                meta[current_key].append(item)
    body = text[m.end():].strip()
    return meta, body


def find_agent_files(project_dir=None, user_dir=None):
    """查找所有 .agent.md 文件

    搜索位置:
      - <project>/.github/agents/*.agent.md
      - <project>/agents/*.agent.md
      - ~/.spyder_copilot_chat/agents/*.agent.md
    """
    if not project_dir:
        project_dir = os.getcwd()
    if not user_dir:
        user_dir = os.path.join(os.path.expanduser("~"),
                                ".spyder_copilot_chat", "agents")

    results = []

    search_paths = [
        os.path.join(project_dir, ".github", "agents"),
        os.path.join(project_dir, "agents"),
        user_dir,
    ]

    for search_dir in search_paths:
        if not os.path.isdir(search_dir):
            continue
        for f in sorted(Path(search_dir).glob("*.agent.md")):
            results.append(str(f))

    return results


def load_agent(agent_file):
    """加载单个 agent 定义

    Returns: dict with keys: name, description, tools, model, system_prompt
    """
    try:
        with open(agent_file, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception:
        logger.warning("Failed to read %s", agent_file)
        return None

    meta, body = _parse_frontmatter(content)

    name = meta.get("name", Path(agent_file).stem.replace(".agent", ""))
    description = meta.get("description", "")
    tools = meta.get("tools", {})

    return {
        "name": name,
        "description": description,
        "file": agent_file,
        "model": meta.get("model", ""),
        "tools_allow": tools.get("allow", []) if isinstance(tools, dict) else [],
        "tools_deny": tools.get("deny", []) if isinstance(tools, dict) else [],
        "system_prompt": body,
    }


def list_agents(project_dir=None):
    """列出所有可用的自定义 Agent"""
    agent_files = find_agent_files(project_dir)
    agents = []
    for f in agent_files:
        agent = load_agent(f)
        if agent:
            agents.append(agent)
    return agents


def get_agent(name, project_dir=None):
    """按名称获取 Agent"""
    for agent in list_agents(project_dir):
        if agent["name"] == name:
            return agent
    return None


def get_agents_system_prompt():
    """生成所有 Agent 的摘要"""
    agents = list_agents()
    if not agents:
        return ""

    lines = ["## Available Custom Agents\n"]
    for a in agents:
        lines.append(f"- **{a['name']}**: {a['description']}")
        if a.get("tools_allow"):
            lines.append(f"  Tools: {', '.join(a['tools_allow'])}")
    return "\n".join(lines)


def get_agent_system_prompt(name, project_dir=None):
    """获取指定 Agent 的系统提示词"""
    agent = get_agent(name, project_dir)
    if not agent:
        return None
    return agent["system_prompt"]


# ════════════════════════════════════════════════════════════════
# 内置专业 Agent — 对标 GitHub Copilot Chat Agent 体系
# ════════════════════════════════════════════════════════════════

from ..copilot_client import (
    TOOL_GROUP_READ, TOOL_GROUP_SEARCH, TOOL_GROUP_EDIT,
    TOOL_GROUP_EXECUTE, TOOL_GROUP_WEB, TOOL_GROUP_MEMORY,
    TOOL_GROUP_PLAN, TOOL_GROUP_GIT, TOOL_GROUP_REVIEW,
    TOOL_GROUP_NOTEBOOK, TOOL_GROUP_FLEET, TOOL_GROUP_EDIT2,
    TOOL_GROUP_SESSION,
)

_ALL_TOOLS = (
    TOOL_GROUP_READ + TOOL_GROUP_SEARCH + TOOL_GROUP_EDIT +
    TOOL_GROUP_EXECUTE + TOOL_GROUP_WEB + TOOL_GROUP_MEMORY +
    TOOL_GROUP_PLAN + TOOL_GROUP_GIT + TOOL_GROUP_REVIEW +
    TOOL_GROUP_NOTEBOOK + TOOL_GROUP_FLEET + TOOL_GROUP_EDIT2 +
    TOOL_GROUP_SESSION
)

_CODE_REVIEW_TOOLS = (
    TOOL_GROUP_READ + TOOL_GROUP_SEARCH + TOOL_GROUP_GIT +
    ["review_changes", "read_project_structure"]
)

_TASK_TOOLS = [
    "run_command", "execution_subagent", "read_file", "search_files",
    "text_search", "list_directory", "manage_todo_list",
    "get_changed_files", "git_log", "memory_save", "memory_recall",
]

_BUILTIN_AGENTS = [
    # v0.8.0：max_tool_rounds=0 表示跟随「设置 → Agent → 单段工具轮次」
    # （默认 200 轮 + 自动续跑）。角色的工作范围由 allowed_tools 限定，
    # 不再用 8/10/25 这类小轮数把长任务硬切断。
    {
        "name": "explore",
        "display_name": "探索助手",
        "short_name": "探索",
        "description": "快速代码库探索和搜索（使用快速模型）",
        "icon": "🔍",
        "model_hint": "fast",
        "allowed_tools": TOOL_GROUP_READ + TOOL_GROUP_SEARCH + ["read_project_structure"],
        "max_tool_rounds": 0,
        "system_prompt": (
            "你是探索助手（Explore），专门在代码库中快速查找信息。\n\n"
            "**行为规范：**\n"
            "- 快速定位相关代码，回答要简洁直接\n"
            "- 使用搜索工具查找文件、文本和符号\n"
            "- 对于简单问题直接回答，不调用多余工具\n"
            "- 用 codebase_search 进行语义搜索\n"
            "- 找到答案后立即给出结论，不需要多次确认"
        ),
    },
    {
        "name": "research",
        "display_name": "调研助手",
        "short_name": "调研",
        "description": "深度代码库调研和分析（使用最强模型）",
        "icon": "📚",
        "model_hint": "powerful",
        "allowed_tools": _ALL_TOOLS,
        "max_tool_rounds": 0,
        "system_prompt": (
            "你是调研助手（Research），专门进行深度代码分析。\n\n"
            "**行为规范：**\n"
            "- 方法全面，多文件交叉引用\n"
            "- 提供有证据的全面分析\n"
            "- 阅读多个相关文件来理解完整上下文\n"
            "- 使用 manage_todo_list 规划调研步骤\n"
            "- 给出结构化、带引用的报告"
        ),
    },
    {
        "name": "code_review",
        "display_name": "代码审查",
        "short_name": "审查",
        "description": "审查代码更改，发现问题和改进机会",
        "icon": "👁",
        "model_hint": "powerful",
        "allowed_tools": _CODE_REVIEW_TOOLS,
        "max_tool_rounds": 0,
        "system_prompt": (
            "你是代码审查助手（Code Review），专注于代码审查。\n\n"
            "**审查范围：**\n"
            "- 安全漏洞\n"
            "- 性能问题\n"
            "- 代码风格和可读性\n"
            "- 正确性和边界情况\n"
            "- 最佳实践\n\n"
            "**行为规范：**\n"
            "- 每次审查给出具体、可操作的建议\n"
            "- 按严重程度分类（严重/一般/建议）\n"
            "- 不要吹毛求疵（不要评论缩进这类小事）\n"
            "- 正面也要肯定好的设计决策"
        ),
    },
    {
        "name": "task",
        "display_name": "任务执行",
        "short_name": "任务",
        "description": "快速运行命令、构建和测试",
        "icon": "⚡",
        "model_hint": "fast",
        "allowed_tools": _TASK_TOOLS,
        "max_tool_rounds": 0,
        "system_prompt": (
            "你是任务执行助手（Task），专注于快速执行任务。\n\n"
            "**行为规范：**\n"
            "- 回复简洁，专注于完成任务\n"
            "- 先做、少说 — 不需要解释每一步\n"
            "- 命令失败时给出修复建议\n"
            "- 完成后给出简洁的总结"
        ),
    },
    {
        "name": "plan",
        "display_name": "方案规划",
        "short_name": "规划",
        "description": "先调研后实施 — 分析再编码",
        "icon": "📋",
        "model_hint": "powerful",
        "allowed_tools": _ALL_TOOLS,
        "max_tool_rounds": 0,
        "system_prompt": (
            "你是方案规划助手（Plan），在编码前先制定计划。\n\n"
            "**工作流程：**\n"
            "1. 调研代码库，理解现有代码和架构\n"
            "2. 用 manage_todo_list 创建详细计划\n"
            "3. 获得用户确认后再开始实施\n"
            "4. 实施过程中按计划推进，更新进度\n\n"
            "**行为规范：**\n"
            "- 实施前总是先展示计划\n"
            "- 解释你的推理过程\n"
            "- 考虑多种方案并说明选择理由"
        ),
    },
    {
        "name": "rubber_duck",
        "display_name": "橡皮鸭",
        "short_name": "橡皮鸭",
        "description": "设计评审和逆向思维伙伴",
        "icon": "🦆",
        "model_hint": "powerful",
        "allowed_tools": TOOL_GROUP_READ + TOOL_GROUP_SEARCH + TOOL_GROUP_MEMORY,
        "max_tool_rounds": 0,
        "system_prompt": (
            "你是橡皮鸭（Rubber Duck）—— 设计评审伙伴和逆向思考者。\n\n"
            "**你的角色：**\n"
            "- 对设计决策提出深入问题\n"
            "- 识别潜在的缺陷和边界情况\n"
            "- 提出替代方案\n"
            "- 帮助用户理清思路\n\n"
            "**行为规范：**\n"
            "- 建设性地批评，不攻击\n"
            "- 问题分类为 阻塞性/非阻塞性/建议\n"
            "- 先理解再质疑\n"
            "- 每个问题都附上理由"
        ),
    },
]


def get_builtin_agents():
    """获取所有内置 Agent 定义"""
    return list(_BUILTIN_AGENTS)


def get_builtin_agent(name):
    """按名称获取内置 Agent"""
    for a in _BUILTIN_AGENTS:
        if a["name"] == name:
            return dict(a)
    return None


def get_agent_config(name, project_dir=None):
    """统一查询 Agent 配置（先查内置，再查自定义）

    Returns: dict with keys: name, display_name, description, icon,
             model_hint, allowed_tools, max_tool_rounds, system_prompt
    """
    # 先查内置
    builtin = get_builtin_agent(name)
    if builtin:
        return builtin

    # 再查自定义 .agent.md
    custom = get_agent(name, project_dir)
    if custom:
        return {
            "name": custom["name"],
            "display_name": custom["name"],
            "description": custom.get("description", ""),
            "icon": "📎",
            "model_hint": None,
            "allowed_tools": (
                custom.get("tools_allow") if custom.get("tools_allow")
                else _ALL_TOOLS
            ),
            "max_tool_rounds": 0,
            "system_prompt": custom.get("system_prompt", ""),
        }

    return None


def get_all_agent_configs(project_dir=None):
    """获取所有 Agent 配置（内置 + 自定义）"""
    agents = get_builtin_agents()
    # 添加自定义
    for a in list_agents(project_dir):
        agents.append({
            "name": a["name"],
            "display_name": a["name"],
            "description": a.get("description", ""),
            "icon": "📎",
            "model_hint": None,
            "allowed_tools": (
                a.get("tools_allow") if a.get("tools_allow")
                else _ALL_TOOLS
            ),
            "max_tool_rounds": 0,
            "system_prompt": a.get("system_prompt", ""),
        })
    return agents


def get_agents_system_prompt_v2():
    """生成所有 Agent 的描述摘要（用于系统提示词注入）"""
    lines = ["## Available Agents\n"]
    for a in _BUILTIN_AGENTS:
        tools_str = ", ".join(a["allowed_tools"][:5])
        suffix = "..." if len(a["allowed_tools"]) > 5 else ""
        model_str = f" [{a['model_hint']}]" if a["model_hint"] else ""
        lines.append(
            f"- **{a['icon']} {a['display_name']}**{model_str}: "
            f"{a['description']} Tools: {tools_str}{suffix}"
        )
    return "\n".join(lines)
