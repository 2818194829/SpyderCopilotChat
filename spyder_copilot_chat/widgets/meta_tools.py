# -*- coding: utf-8 -*-
"""
P1 元工具 — v0.5.6

四个只读的「上下文检索」工具，让模型按需取用规则、Skills 与仓库地图，
而不是把所有内容一次性塞进系统提示词：

  load_skill          命中后加载某个 Skill 的完整正文
  list_skills         查看全部可用 Skill 及来源
  list_project_rules  查看当前生效的规则文件清单
  search_repo_map     在仓库索引里按文件名 / 符号检索

这些工具不写任何文件，因此在 Plan 模式白名单内、无需审批。
分发在后台线程完成（task_runtime.execute_tool_batch）。
"""

META_TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "load_skill",
            "description": (
                "加载某个 Skill 的完整方法包（渐进式加载）。"
                "当系统提示词的 Skills 目录中某个技能与当前任务匹配时，"
                "先调用本工具读取正文，再严格按其中步骤执行。"
                "不要凭描述猜测技能内容。"),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string",
                             "description": "Skill 名称，须与目录中的名称一致"}
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_skills",
            "description": "列出所有可用 Skills（内置 / 用户级 / 项目级）及其来源与说明。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_project_rules",
            "description": (
                "列出当前生效的项目 / 用户规则文件及适用 glob。"
                "需要确认项目约定来源、或规则未完整注入时使用。"),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_repo_map",
            "description": (
                "在仓库结构索引中按文件名或顶层符号检索。"
                "用于大仓库中快速定位「某个类/函数在哪个文件」，"
                "命中后再用 read_file 精读。"),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "文件名片段或符号名"},
                    "limit": {"type": "integer", "description": "最多返回条数，默认 30"},
                },
                "required": ["query"],
            },
        },
    },
]

META_TOOL_NAMES = frozenset(
    t["function"]["name"] for t in META_TOOL_DEFINITIONS)


def handle_meta_tool(name, args, base_dir=None, config=None):
    """返回给模型的文本结果。抛出的异常由调用方转成 tool error。"""
    args = args if isinstance(args, dict) else {}
    if name == "load_skill":
        from .skills import load_skill_text
        return load_skill_text(args.get("name", ""), base_dir)
    if name == "list_skills":
        from .skills import catalog_text
        return catalog_text(base_dir)
    if name == "list_project_rules":
        from .rules import list_rules
        return list_rules(base_dir, args.get("file"))
    if name == "search_repo_map":
        from .repo_index import search_repo_map
        return search_repo_map(base_dir, args.get("query", ""),
                               limit=args.get("limit", 30))
    raise ValueError(f"未知元工具: {name}")
