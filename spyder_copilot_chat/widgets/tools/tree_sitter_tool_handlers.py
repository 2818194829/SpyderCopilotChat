# -*- coding: utf-8 -*-
"""
Tree-sitter 工具处理器 — 注册到 TOOL_HANDLERS

可选依赖，如果 tree-sitter 不可用则自动降级。
"""

import json
import logging
import os

from .tree_sitter_tools import (
    TreeSitterManager,
    get_symbol_definitions,
    get_code_outline,
    find_related_symbols,
    get_call_hierarchy,
)

logger = logging.getLogger(__name__)


def handle_get_symbol_definitions(args, base_dir=None):
    """获取文件中的符号定义"""
    filepath = args.get("filepath", "")
    if not filepath:
        return "❌ 错误: 缺少 filepath 参数"
    if base_dir:
        filepath = os.path.join(base_dir, filepath)
    if not os.path.isfile(filepath):
        return f"❌ 文件不存在: {filepath}"

    symbols = get_symbol_definitions(filepath)
    if not symbols:
        return f"📄 未在 {os.path.basename(filepath)} 中找到符号定义"

    lines = [f"📋 **{os.path.basename(filepath)}** 的符号定义:\n"]
    for s in symbols:
        lines.append(f"- {s['type']} **{s['name']}** (行 {s['line']})")

    return "\n".join(lines)


def handle_get_code_outline(args, base_dir=None):
    """获取代码大纲"""
    filepath = args.get("filepath", "")
    max_depth = args.get("max_depth", 3)
    if not filepath:
        return "❌ 错误: 缺少 filepath 参数"
    if base_dir:
        filepath = os.path.join(base_dir, filepath)
    if not os.path.isfile(filepath):
        return f"❌ 文件不存在: {filepath}"

    outline = get_code_outline(filepath, max_depth)
    if not outline:
        return f"📄 无法生成 {os.path.basename(filepath)} 的大纲"

    lines = [f"📋 **{os.path.basename(filepath)}** 的大纲:\n"]

    def _format_outline(entries, indent=0):
        prefix = "  " * indent
        for entry in entries:
            icon = "📦" if entry["type"] == "class" else "🔧"
            lines.append(f"{prefix}- {icon} **{entry['name']}** "
                        f"({entry['type']}, 行 {entry['line']})")
            if entry.get("children") and indent < max_depth:
                _format_outline(entry["children"], indent + 1)

    _format_outline(outline)
    return "\n".join(lines)


def handle_find_related_symbols(args, base_dir=None):
    """搜索相关符号"""
    query = args.get("query", "")
    max_results = args.get("max_results", 20)
    if not query:
        return "❌ 错误: 缺少 query 参数"
    if not base_dir:
        base_dir = os.getcwd()
    if not os.path.isdir(base_dir):
        return f"❌ 目录不存在: {base_dir}"

    results = find_related_symbols(query, base_dir, max_results)

    if not results:
        return f"🔍 未找到与「{query}」相关的符号"

    lines = [f"🔍 找到 {len(results)} 个与「{query}」相关的符号:\n"]
    for r in results:
        icon = "📦" if r["type"] == "class" else "🔧"
        rel_path = os.path.relpath(r["filepath"], base_dir)
        lines.append(f"- {icon} **{r['name']}** — {r['type']} — "
                    f"`{rel_path}` 行 {r['line']}")

    return "\n".join(lines)


def handle_get_call_hierarchy(args, base_dir=None):
    """获取调用层次"""
    filepath = args.get("filepath", "")
    line = args.get("line", 1)
    if not filepath:
        return "❌ 错误: 缺少 filepath 参数"
    if base_dir:
        filepath = os.path.join(base_dir, filepath)
    if not os.path.isfile(filepath):
        return f"❌ 文件不存在: {filepath}"

    result = get_call_hierarchy(filepath, line)
    name = result.get("name", "")
    if not name:
        return "❌ 无法定位函数"

    lines = [f"📊 **{name}** 的调用关系:\n"]
    calls = result.get("calls", [])
    if calls:
        lines.append("**调用:**")
        for c in calls:
            lines.append(f"  → `{c}()`")
    else:
        lines.append("**调用:** (无)")

    called_by = result.get("called_by", [])
    if called_by:
        lines.append("\n**被调用:**")
        for c in called_by:
            lines.append(f"  ← `{c}()`")
    else:
        lines.append("\n**被调用:** (无)")

    return "\n".join(lines)


def handle_get_symbol_at_position(args, base_dir=None):
    """获取指定位置的符号"""
    filepath = args.get("filepath", "")
    line = args.get("line", 1)
    if not filepath:
        return "❌ 错误: 缺少 filepath 参数"
    if base_dir:
        filepath = os.path.join(base_dir, filepath)
    if not os.path.isfile(filepath):
        return f"❌ 文件不存在: {filepath}"

    symbols = get_symbol_definitions(filepath)
    # 找到覆盖该行的符号
    for s in symbols:
        if s["line"] == line:
            return f"📍 `{s['name']}` — {s['type']} (行 {s['line']})"

    return f"📍 行 {line} 未找到精确匹配的符号定义"


# 处理器注册表
TREE_SITTER_HANDLERS = {
    "get_symbol_definitions": handle_get_symbol_definitions,
    "get_code_outline": handle_get_code_outline,
    "find_related_symbols": handle_find_related_symbols,
    "get_call_hierarchy": handle_get_call_hierarchy,
    "get_symbol_at_position": handle_get_symbol_at_position,
}
