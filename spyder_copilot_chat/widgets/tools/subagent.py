# -*- coding: utf-8 -*-
"""
子 Agent (Subagent) 系统

对标 GitHub Copilot Chat 的 searchSubagent / executionSubagent：
  - 搜索子 Agent：专门用于在工作区中搜索相关代码
  - 执行子 Agent：专门用于运行终端命令并返回结果

子 Agent 在独立上下文中运行，支持上下文窗口管理。
"""

import json
import logging
import os
import re
import subprocess
import sys

logger = logging.getLogger(__name__)

_WINDOWS = sys.platform == "win32"
_SUBPROCESS_FLAGS = subprocess.CREATE_NO_WINDOW if _WINDOWS else 0

# 默认排除目录
EXCLUDE_DIRS = {
    ".git", "__pycache__", ".venv", "venv", "env", "node_modules",
    ".spyproject", ".idea", "dist", "build", ".tox", ".egg-info",
    ".mypy_cache", ".pytest_cache", ".vscode", ".hg", ".svn",
}

# 可搜索的文件扩展名
SEARCHABLE_EXTENSIONS = {
    '.py', '.js', '.ts', '.tsx', '.jsx', '.html', '.css', '.scss',
    '.json', '.md', '.yaml', '.yml', '.toml', '.sh', '.bat', '.ps1',
    '.cpp', '.c', '.h', '.hpp', '.java', '.rs', '.go', '.rb', '.php',
    '.sql', '.xml', '.kt', '.swift', '.r', '.m', '.mm', '.cs', '.fs',
    '.vue', '.svelte', '.astro', '.tex', '.cfg', '.ini', '.conf',
}


# ══════════════════════════════════════════════════════════════════════
# 搜索子 Agent
# ══════════════════════════════════════════════════════════════════════

def run_search_subagent(query, base_dir=None, max_results=30):
    """运行搜索子 Agent — 在工作区中查找相关代码

    对标 VS Code 的 copilot.tools.searchSubagent

    Parameters
    ----------
    query : str
        搜索查询（自然语言或关键词）
    base_dir : str or None
        搜索的根目录
    max_results : int
        最大结果数

    Returns
    -------
    str : 格式化的搜索结果
    """
    search_dir = base_dir or os.getcwd()
    search_dir = os.path.abspath(search_dir)

    if not os.path.isdir(search_dir):
        return f"目录不存在: {search_dir}"

    # 分词：支持自然语言查询
    keywords = _extract_keywords(query)
    if not keywords:
        return "错误: 无法从查询中提取有效的搜索关键词"

    logger.debug("Search subagent: keywords=%s in %s", keywords, search_dir)

    # 分阶段搜索
    # 阶段 1：文件名匹配（最高相关度）
    name_matches = _search_by_filename(keywords, search_dir)
    # 阶段 2：文件内容匹配
    content_matches = _search_by_content(keywords, search_dir, name_matches)
    # 阶段 3：符号定义搜索（函数/类定义）
    symbol_matches = _search_symbols(keywords, search_dir)

    # 合并结果，去重
    seen_paths = set()
    all_results = []

    for match_list in [name_matches, symbol_matches, content_matches]:
        for m in match_list:
            p = m["path"]
            if p not in seen_paths:
                seen_paths.add(p)
                all_results.append(m)

    # 按评分排序
    all_results.sort(key=lambda x: x["score"], reverse=True)
    all_results = all_results[:max_results]

    if not all_results:
        return f"🔍 搜索子 Agent 在 '{search_dir}' 中未找到与 '{query}' 相关的结果"

    lines = [f"🔍 **搜索子 Agent** — 查询: {query}",
             f"   搜索目录: {search_dir}",
             f"   找到 {len(all_results)} 个相关结果:\n"]

    for r in all_results:
        lines.append(f"**{r['path']}** (相关度: {r['score']})")
        for snippet in r.get("snippets", []):
            lines.append(f"  {snippet}")
        lines.append("")

    return "\n".join(lines)


def _extract_keywords(query):
    """从查询中提取关键词"""
    # 移除常见停用词
    stop_words = {
        "the", "a", "an", "is", "are", "was", "were", "be", "been",
        "being", "have", "has", "had", "do", "does", "did", "will",
        "would", "could", "should", "may", "might", "shall", "can",
        "to", "of", "in", "for", "on", "with", "at", "by", "from",
        "as", "into", "through", "during", "before", "after", "above",
        "below", "between", "out", "off", "over", "under", "again",
        "further", "then", "once", "here", "there", "when", "where",
        "why", "how", "all", "each", "every", "both", "few", "more",
        "most", "other", "some", "such", "no", "nor", "not", "only",
        "own", "same", "so", "than", "too", "very", "just", "find",
        "search", "look", "show", "get", "tell", "代码", "查找",
        "搜索", "找到", "寻找",
    }

    # 提取引号中的精确短语
    phrases = re.findall(r'"([^"]+)"', query)
    for p in phrases:
        query = query.replace(f'"{p}"', "")

    # 分词
    words = re.findall(r'[a-zA-Z_][a-zA-Z0-9_]*', query)
    keywords = [w.lower() for w in words if w.lower() not in stop_words and len(w) > 1]

    # 添加引号短语作为关键词
    keywords.extend(p.lower() for p in phrases if len(p) > 1)

    # 去重
    return list(dict.fromkeys(keywords))


def _search_by_filename(keywords, search_dir):
    """按文件名搜索"""
    results = []
    for root, dirs, files in os.walk(search_dir):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS and not d.startswith(".")]
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            if ext not in SEARCHABLE_EXTENSIONS:
                continue
            f_lower = f.lower()
            score = sum(10 for kw in keywords if kw in f_lower)
            if score > 0:
                rel = os.path.relpath(os.path.join(root, f), search_dir)
                results.append({"path": rel, "score": score, "snippets": []})
    return results


def _search_by_content(keywords, search_dir, existing):
    """按文件内容搜索"""
    existing_paths = {e["path"] for e in existing}
    results = []
    for root, dirs, files in os.walk(search_dir):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS and not d.startswith(".")]
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            if ext not in SEARCHABLE_EXTENSIONS:
                continue
            filepath = os.path.join(root, f)
            rel = os.path.relpath(filepath, search_dir)
            try:
                with open(filepath, "r", encoding="utf-8", errors="replace") as fh:
                    content = fh.read()
            except (OSError, UnicodeDecodeError):
                continue

            content_lower = content.lower()
            score = 0
            matched_lines = []
            for kw in keywords:
                count = content_lower.count(kw)
                if count > 0:
                    score += count
                    # 收集匹配行
                    for i, line in enumerate(content.split("\n"), 1):
                        if kw in line.lower() and len(matched_lines) < 5:
                            matched_lines.append(f"  {i}: {line.strip()[:150]}")

            if score > 0:
                if rel in existing_paths:
                    # 更新已有记录的评分
                    for e in results:
                        if e["path"] == rel:
                            e["score"] += score
                            if matched_lines:
                                e["snippets"] = matched_lines[:5]
                            break
                else:
                    results.append({
                        "path": rel,
                        "score": score,
                        "snippets": matched_lines[:5],
                    })
    return results


def _search_symbols(keywords, search_dir):
    """搜索函数/类定义"""
    results = []
    # Python 符号模式
    py_pattern = re.compile(
        r'^(?:def|class|async\s+def)\s+(\w+)', re.MULTILINE
    )
    # JS/TS 符号模式
    js_pattern = re.compile(
        r'^(?:function\s+|(?:export\s+)?(?:class|interface|type|enum|const|let|var)\s+)'
        r'(\w+)', re.MULTILINE
    )

    for root, dirs, files in os.walk(search_dir):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS and not d.startswith(".")]
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            if ext not in ('.py', '.js', '.ts', '.tsx', '.jsx'):
                continue
            filepath = os.path.join(root, f)
            rel = os.path.relpath(filepath, search_dir)
            try:
                with open(filepath, "r", encoding="utf-8", errors="replace") as fh:
                    content = fh.read()
            except (OSError, UnicodeDecodeError):
                continue

            if ext == '.py':
                symbols = py_pattern.findall(content)
            else:
                symbols = js_pattern.findall(content)

            score = 0
            matched_symbols = []
            for sym in symbols:
                sym_lower = sym.lower()
                for kw in keywords:
                    if kw in sym_lower:
                        score += 20
                        matched_symbols.append(sym)
                        break

            if score > 0 and matched_symbols:
                results.append({
                    "path": rel,
                    "score": score,
                    "snippets": [f"  📌 符号: {', '.join(matched_symbols[:8])}"],
                })

    return results


# ══════════════════════════════════════════════════════════════════════
# 执行子 Agent
# ══════════════════════════════════════════════════════════════════════

def run_execution_subagent(command, base_dir=None, timeout=120):
    """运行执行子 Agent — 执行终端命令并返回结果

    对标 VS Code 的 copilot.tools.executionSubagent

    Parameters
    ----------
    command : str
        要执行的命令
    base_dir : str or None
        工作目录
    timeout : int
        超时时间（秒）

    Returns
    -------
    str : 格式化的命令执行结果
    """
    cwd = base_dir or os.getcwd()

    if not command or not command.strip():
        return "错误: 未指定命令"

    logger.debug("Execution subagent: running '%s' in %s (timeout=%ds)",
                 command, cwd, timeout)

    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=cwd,
            capture_output=True,
            encoding='utf-8',
            errors='replace',
            timeout=timeout,
            creationflags=_SUBPROCESS_FLAGS,
        )

        output_parts = [f"$ {command}", f"进程退出码: {result.returncode}"]

        if result.stdout:
            stdout = result.stdout
            # 智能截断：保留头部和尾部
            if len(stdout) > 10000:
                stdout = (stdout[:5000] + "\n\n... (中间省略 %d 字符) ...\n\n" % (len(stdout) - 10000)
                          + stdout[-5000:])
            output_parts.append(f"\n[标准输出]\n{stdout}")

        if result.stderr:
            stderr = result.stderr
            if len(stderr) > 5000:
                stderr = stderr[:5000] + "\n\n...(截断)..."
            output_parts.append(f"\n[标准错误]\n{stderr}")

        if not result.stdout and not result.stderr:
            output_parts.append("(命令执行完毕，无输出)")

        return "\n".join(output_parts)

    except subprocess.TimeoutExpired:
        return (f"$ {command}\n"
                f"⏱ 命令执行超时 ({timeout}s)\n"
                f"  建议：简化命令或增加超时时间")
    except FileNotFoundError:
        return (f"$ {command}\n"
                f"❌ 命令未找到: {command.split()[0] if command.split() else command}\n"
                f"  建议：检查命令是否已安装或指定完整路径")
    except PermissionError:
        return (f"$ {command}\n"
                f"❌ 权限不足，无法执行")
    except Exception as e:
        return (f"$ {command}\n"
                f"❌ 执行失败: {e}")


# ══════════════════════════════════════════════════════════════════════
# Fleet 模式（并行子 Agent）
# ══════════════════════════════════════════════════════════════════════

def run_fleet(tasks, base_dir=None, max_concurrent=3):
    """Fleet 模式 — 并行执行多个子 Agent 任务

    对标 VS Code 的 fleet 群体模式

    Parameters
    ----------
    tasks : list[dict]
        任务列表，每项包含:
        - type: "search" 或 "execution"
        - query/command: 搜索查询或要执行的命令
        - description: 任务描述
    base_dir : str or None
        基础目录
    max_concurrent : int
        最大并行数

    Returns
    -------
    str : 格式化的并行任务结果
    """
    import threading
    import queue

    if not tasks:
        return "错误: 未指定任务"

    result_queue = queue.Queue()
    threads = []
    semaphore = threading.Semaphore(max_concurrent)

    def _run_task(task):
        with semaphore:
            try:
                ttype = task.get("type", "search")
                desc = task.get("description", ttype)
                if ttype == "search":
                    query = task.get("query", "")
                    res = run_search_subagent(query, base_dir)
                elif ttype == "execution":
                    cmd = task.get("command", "")
                    res = run_execution_subagent(cmd, base_dir)
                else:
                    res = f"未知任务类型: {ttype}"
                result_queue.put({"description": desc, "result": res})
            except Exception as e:
                result_queue.put({"description": task.get("description", "?"),
                                  "result": f"❌ 失败: {e}"})

    for task in tasks:
        t = threading.Thread(target=_run_task, args=(task,))
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    results = []
    while not result_queue.empty():
        results.append(result_queue.get())

    lines = ["🚀 **Fleet 模式** — 并行执行完成",
             f"   任务数: {len(tasks)}，并行度: {max_concurrent}\n"]

    for r in results:
        lines.append(f"--- {r['description']} ---")
        lines.append(r["result"])
        lines.append("")

    return "\n".join(lines)
