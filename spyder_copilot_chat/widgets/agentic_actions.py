# -*- coding: utf-8 -*-
"""
Agent 工具执行引擎 — 完整版

处理 AI 模型发出的工具调用（tool_calls），执行实际的操作
（如读写文件、搜索文件、运行命令、MCP 工具等），并将结果返回给模型。

对标 GitHub Copilot Chat 的完整工具系统:
  - 文件操作: read_file, create_file, edit_file, rename_file, delete_file
  - 搜索: search_files, text_search, list_directory, codebase_search
  - 执行: run_command, get_errors
  - 网络: fetch_webpage
  - 记忆: memory_save, memory_recall
  - 项目管理: manage_todo_list
  - 子 Agent: search_subagent, execution_subagent, run_fleet
  - Git 操作: get_changed_files, git_log, git_add, git_commit, git_create_branch, review_changes
  - 编辑: apply_patch, insert_edit
  - Notebook: create_notebook, get_notebook_info, add_notebook_cell, edit_notebook_cell, delete_notebook_cell
  - 内联编辑: get_current_file
  - MCP: 通过 mcp_client 调用外部 MCP 服务器工具
"""

# 新增工具处理器导入
from .tools.subagent import (
    run_search_subagent,
    run_execution_subagent,
    run_fleet,
)
from .tools.git_tools import (
    get_changed_files as git_get_changed_files,
    get_changed_files_diff,
    review_changes as git_review_changes,
    git_add,
    git_commit,
    git_create_branch,
    git_log,
    is_git_repo,
)
from .tools.apply_patch import apply_patch, insert_edit
from .tools.notebook_tools import (
    create_notebook,
    get_notebook_info,
    add_notebook_cell,
    edit_notebook_cell,
    delete_notebook_cell,
)

import fnmatch
import json
import logging
import os
import re
import subprocess
import sys

logger = logging.getLogger(__name__)

# Windows 下禁止子进程弹出控制台窗口
_SUBPROCESS_FLAGS = (
    subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
)


# ── 核心执行函数 ──────────────────────────────────────────────────────

def execute_tool(tool_call, base_dir=None):
    """执行单个工具调用，返回 (tool_call_id, result_text, error_text)

    Parameters
    ----------
    tool_call : dict
        OpenAI 格式的工具调用，包含 id, function {name, arguments}
    base_dir : str or None
        操作的基础目录（项目根目录）

    Returns
    -------
    (tool_call_id, result, error)
        - tool_call_id: 工具调用 ID
        - result: 成功时的输出文本
        - error: 失败时的错误文本
    """
    tool_id = tool_call.get("id", "")
    func_info = tool_call.get("function", {})
    func_name = func_info.get("name", "")

    if not func_name:
        return tool_id, "", "工具名称为空，跳过执行"

    raw_args = func_info.get("arguments", "{}")
    if not raw_args or raw_args.strip() == "":
        raw_args = "{}"
    args = _parse_robust_json(raw_args, func_name)
    if isinstance(args, str):
        # 返回的是错误消息
        return (tool_id, "", args)

    # ── Pre-tool Hooks ──
    try:
        from .hooks import get_hook_engine
        hook_result = get_hook_engine().evaluate_pre_tool(func_name, args)
        if hook_result.blocked:
            return tool_id, "", f"🔌 Hook 阻止: {hook_result.message}"
        if hook_result.modified_args:
            args.update(hook_result.modified_args)
    except Exception as e:
        logger.debug("Hook engine (pre) error: %s", e)

    # 检查是否为 MCP 工具 (mcp_<server>__<tool>)
    if func_name.startswith("mcp_"):
        return _handle_mcp_tool(tool_id, func_name, args, base_dir)

    # 路由到对应的处理函数
    handler = TOOL_HANDLERS.get(func_name)
    if not handler:
        return tool_id, "", f"未知工具: {func_name}"

    try:
        result = handler(args, base_dir=base_dir)

        # ── Post-tool Hooks ──
        try:
            post_result = get_hook_engine().evaluate_post_tool(func_name, args, result)
            if post_result.message:
                result += f"\n\n🔌 Hook: {post_result.message}"
        except Exception as e:
            logger.debug("Hook engine (post) error: %s", e)

        return tool_id, result, ""
    except Exception as e:
        logger.exception("工具 %s 执行失败", func_name)
        return tool_id, "", f"工具 {func_name} 执行失败: {e}"


# ── 工具处理器 ────────────────────────────────────────────────────────

def _handle_read_file(args, base_dir=None):
    """读取文件内容"""
    filepath = args.get("filepath", "")
    if not filepath:
        return "错误: 未指定文件路径"

    path = _resolve_path(filepath, base_dir)
    if not os.path.exists(path):
        return f"文件不存在: {path}"
    if not os.path.isfile(path):
        return f"不是文件: {path}"

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        lines = content.count("\n") + 1
        return f"[{os.path.basename(path)} — {lines} 行]\n```\n{content}\n```"
    except Exception as e:
        return f"读取文件失败: {e}"


def _handle_search_files(args, base_dir=None):
    """按 glob 模式搜索文件"""
    pattern = args.get("pattern", "")
    search_dir = args.get("base_dir") or base_dir or os.getcwd()

    if not pattern:
        return "错误: 未指定搜索模式"

    search_dir = os.path.abspath(search_dir)
    if not os.path.isdir(search_dir):
        return f"目录不存在: {search_dir}"

    matches = []
    # 排除目录
    exclude_dirs = {".git", "__pycache__", ".venv", "venv", "env",
                    "node_modules", ".spyproject", ".idea", "dist", "build"}

    for root, dirs, files in os.walk(search_dir):
        # 排除隐藏目录和常见排除目录
        dirs[:] = [d for d in dirs if not d.startswith(".")
                   and d not in exclude_dirs]
        for f in files:
            rel_path = os.path.relpath(os.path.join(root, f), search_dir)
            if fnmatch.fnmatch(rel_path, pattern):
                matches.append(rel_path)

    # 限制结果数量
    max_results = 200
    if len(matches) > max_results:
        result = "\n".join(matches[:max_results])
        result += f"\n\n... 以及另外 {len(matches) - max_results} 个匹配文件"
    else:
        result = "\n".join(matches) if matches else "(无匹配)"

    return f"找到 {len(matches)} 个匹配文件:\n{result}"


def _handle_text_search(args, base_dir=None):
    """在文件中搜索文本"""
    query = args.get("query", "")
    file_pattern = args.get("pattern", "*")
    search_dir = base_dir or os.getcwd()

    if not query:
        return "错误: 未指定搜索文本"

    search_dir = os.path.abspath(search_dir)
    exclude_dirs = {".git", "__pycache__", ".venv", "venv", "env",
                    "node_modules", ".spyproject", ".idea", "dist", "build"}

    results = []
    max_results = 50

    try:
        regex = re.compile(re.escape(query), re.IGNORECASE)
    except re.error as e:
        return f"正则表达式错误: {e}"

    for root, dirs, files in os.walk(search_dir):
        dirs[:] = [d for d in dirs if not d.startswith(".")
                   and d not in exclude_dirs]
        for f in files:
            if not fnmatch.fnmatch(f, file_pattern):
                continue
            if len(results) >= max_results:
                break
            filepath = os.path.join(root, f)
            try:
                with open(filepath, "r", encoding="utf-8", errors="replace") as fh:
                    for i, line in enumerate(fh, 1):
                        if regex.search(line):
                            rel = os.path.relpath(filepath, search_dir)
                            results.append(f"{rel}:{i}: {line.rstrip()[:200]}")
                            if len(results) >= max_results:
                                break
            except (OSError, UnicodeDecodeError):
                continue

    if not results:
        return f"在 {search_dir} 中未找到匹配 '{query}' 的内容"

    output = "\n".join(results)
    if len(results) >= max_results:
        output += "\n\n(结果已截断，仅显示前 {max_results} 条)"
    return f"搜索 '{query}' 找到 {len(results)} 处匹配:\n{output}"


def _handle_list_directory(args, base_dir=None):
    """列出目录内容"""
    path = args.get("path", "")
    if not path:
        path = base_dir or "."

    target = _resolve_path(path, base_dir)
    if not os.path.exists(target):
        return f"目录不存在: {target}"
    if not os.path.isdir(target):
        return f"不是目录: {target}"

    try:
        entries = sorted(os.listdir(target))
        dirs = []
        files = []
        for e in entries:
            ep = os.path.join(target, e)
            if os.path.isdir(ep):
                dirs.append(f"  📁 {e}/")
            else:
                size = os.path.getsize(ep)
                if size < 1024:
                    size_str = f"{size} B"
                elif size < 1024 * 1024:
                    size_str = f"{size / 1024:.1f} KB"
                else:
                    size_str = f"{size / 1024 / 1024:.1f} MB"
                files.append(f"  📄 {e}  ({size_str})")

        result = f"📂 {target}\n"
        if dirs:
            result += "\n子目录:\n" + "\n".join(dirs) + "\n"
        if files:
            result += "\n文件:\n" + "\n".join(files) + "\n"
        if not dirs and not files:
            result += "(空目录)"
        return result
    except PermissionError:
        return f"权限不足，无法访问: {target}"
    except Exception as e:
        return f"列出目录失败: {e}"


def _handle_create_file(args, base_dir=None):
    """创建或覆盖文件"""
    filepath = args.get("filepath", "")
    content = args.get("content", "")

    if not filepath:
        return "错误: 未指定文件路径"

    path = _resolve_path(filepath, base_dir)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return f"✅ 文件已创建: {path}\n({len(content)} 字符, {content.count(chr(10)) + 1} 行)"
    except Exception as e:
        return f"❌ 创建文件失败: {e}"


def _handle_run_command(args, base_dir=None):
    """在终端运行命令"""
    command = args.get("command", "")
    if not command:
        return "错误: 未指定命令"

    cwd = base_dir or os.getcwd()
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=cwd,
            capture_output=True,
            encoding='utf-8',
            errors='replace',
            timeout=60,
            creationflags=_SUBPROCESS_FLAGS,
        )
        output = ""
        if result.stdout:
            output += result.stdout[:3000]
        if result.stderr:
            if output:
                output += "\n"
            output += f"[stderr]\n{result.stderr[:1000]}"
        if not output:
            output = "(命令执行完毕，无输出)"

        status = f"exit code: {result.returncode}"
        return f"$ {command}\n{status}\n{output}"
    except subprocess.TimeoutExpired:
        return f"$ {command}\n⏱ 命令执行超时 (60s)"
    except Exception as e:
        return f"$ {command}\n❌ 执行失败: {e}"


def _handle_get_errors(args, base_dir=None):
    """获取编辑器错误（返回模拟结果，实际需集成编辑器 API）"""
    filepath = args.get("filepath", "")
    if filepath:
        p = _resolve_path(filepath, base_dir)
        return f"错误检查: {p}\n(当前未集成 Spyder 编辑器 API，请手动检查)"
    return "当前未集成 Spyder 编辑器 API，请在编辑器中查看 Problems 面板。"


# ── 新增工具处理器 ──────────────────────────────────────────────────

def _handle_edit_file(args, base_dir=None):
    """编辑已有文件的特定行范围"""
    filepath = args.get("filepath", "")
    start_line = args.get("start_line", 1)
    end_line = args.get("end_line", -1)
    new_content = args.get("new_content", "")

    if not filepath:
        return "错误: 未指定文件路径"
    path = _resolve_path(filepath, base_dir)
    if not os.path.exists(path):
        return f"文件不存在: {path}"

    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception as e:
        return f"读取文件失败: {e}"

    total_lines = len(lines)
    if end_line < 0 or end_line > total_lines:
        end_line = total_lines
    if start_line < 1:
        start_line = 1

    # 替换指定行范围
    new_lines = new_content.split("\n")
    # 保持原行尾换行符
    if lines and lines[-1].endswith("\n"):
        new_lines = [l + "\n" for l in new_lines]
    else:
        new_lines = [l + "\n" for l in new_lines[:-1]] + [new_lines[-1]]

    replaced = lines[:start_line - 1] + new_lines + lines[end_line:]

    try:
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(replaced)
        removed = end_line - start_line + 1
        added = len(new_lines)
        return (f"✅ 文件已编辑: {path}\n"
                f"  删除 {removed} 行, 添加 {added} 行\n"
                f"  当前文件共 {len(replaced)} 行")
    except Exception as e:
        return f"❌ 编辑文件失败: {e}"


def _handle_rename_file(args, base_dir=None):
    """重命名或移动文件"""
    source = args.get("source", "")
    dest = args.get("destination", "")

    if not source or not dest:
        return "错误: 需要指定 source 和 destination"
    src_path = _resolve_path(source, base_dir)
    dst_path = _resolve_path(dest, base_dir)

    if not os.path.exists(src_path):
        return f"源文件不存在: {src_path}"

    try:
        os.makedirs(os.path.dirname(dst_path), exist_ok=True)
        os.rename(src_path, dst_path)
        return f"✅ 已重命名: {src_path} → {dst_path}"
    except Exception as e:
        return f"❌ 重命名失败: {e}"


def _handle_fetch_webpage(args, base_dir=None):
    """获取网页内容"""
    url = args.get("url", "")
    max_chars = args.get("max_chars", 5000)

    if not url:
        return "错误: 未指定 URL"

    try:
        import urllib.request
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                   "AppleWebKit/537.36 (Spyder Copilot Chat)"}
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            content = resp.read().decode("utf-8", errors="replace")

        # 简单提取文本（去除 HTML 标签）
        import re
        text = re.sub(r'<[^>]+>', ' ', content)
        text = re.sub(r'\s+', ' ', text).strip()
        if len(text) > max_chars:
            text = text[:max_chars] + "\n\n[内容已截断]"
        return f"📄 {url}\n\n{text}"
    except Exception as e:
        return f"❌ 获取网页失败: {e}"


def _handle_memory_save(args, base_dir=None):
    """保存信息到长期记忆"""
    key = args.get("key", "")
    value = args.get("value", "")
    namespace = args.get("namespace", "global")

    if not key or not value:
        return "错误: 需要指定 key 和 value"

    try:
        from .tools.memory import save_memory
        save_memory(namespace, key, value)
        return f"✅ 已保存记忆: [{namespace}] {key}"
    except Exception as e:
        return f"❌ 保存记忆失败: {e}"


def _handle_memory_recall(args, base_dir=None):
    """从长期记忆中回忆信息"""
    key = args.get("key", "")
    namespace = args.get("namespace", "")

    try:
        from .tools.memory import load_memory, list_memories
        if not key:
            # 列出所有记忆
            all_mem = list_memories(namespace if namespace else None)
            if not all_mem:
                return "暂无保存的记忆"
            lines = ["📝 已保存的记忆:"]
            for ns, k, v in all_mem:
                lines.append(f"  [{ns}] {k} = {str(v)[:100]}")
            return "\n".join(lines)
        else:
            value = load_memory(namespace or "global", key)
            if value is None:
                return f"未找到记忆: [{namespace or 'global'}] {key}"
            return f"📝 [{namespace or 'global'}] {key}:\n{value}"
    except ImportError:
        # 记忆模块尚未创建
        return "记忆系统尚未初始化"
    except Exception as e:
        return f"❌ 回忆失败: {e}"


def _handle_create_directory(args, base_dir=None):
    """递归创建目录"""
    path = args.get("path", "")
    if not path:
        return "错误: 未指定目录路径"
    target = _resolve_path(path, base_dir)
    try:
        os.makedirs(target, exist_ok=True)
        return f"✅ 目录已创建: {target}"
    except Exception as e:
        return f"❌ 创建目录失败: {e}"


def _handle_delete_file(args, base_dir=None):
    """删除文件"""
    filepath = args.get("filepath", "")
    if not filepath:
        return "错误: 未指定文件路径"
    path = _resolve_path(filepath, base_dir)
    if not os.path.exists(path):
        return f"文件不存在: {path}"
    try:
        os.remove(path)
        return f"✅ 文件已删除: {path}"
    except Exception as e:
        return f"❌ 删除失败: {e}"


def _handle_get_current_file(args, base_dir=None):
    """获取当前编辑器文件信息"""
    include_content = args.get("include_content", True)
    try:
        from qtpy.QtWidgets import QApplication
        for widget in QApplication.topLevelWidgets():
            if hasattr(widget, 'get_plugin'):
                editor = widget.get_plugin('editor')
                if editor and hasattr(editor, 'get_current_editor'):
                    ed = editor.get_current_editor()
                    if ed:
                        filename = getattr(ed, 'filename', '') or '(未命名)'
                        lang = os.path.splitext(filename)[1] if filename else ""
                        info = f"📄 当前文件: {filename}\n语言: {lang or '未知'}"
                        if include_content and hasattr(ed, 'toPlainText'):
                            content = ed.toPlainText()
                            lines = content.count('\n') + 1
                            info += f"\n行数: {lines}"
                            if len(content) <= 5000:
                                info += f"\n\n```{lang[1:] if lang else ''}\n{content}\n```"
                            else:
                                info += f"\n\n(内容过长 {len(content)} 字符)"
                        return info
    except Exception:
        pass
    return "未检测到打开的编辑器文件"


def _handle_codebase_search(args, base_dir=None):
    """在代码库中智能搜索"""
    query = args.get("query", "")
    max_results = args.get("max_results", 20)
    if not query:
        return "错误: 未指定搜索查询"
    search_dir = base_dir or os.getcwd()
    exclude_dirs = {".git", "__pycache__", ".venv", "venv", "env",
                    "node_modules", ".spyproject", ".idea", "dist",
                    "build", ".tox", ".egg-info"}
    results = []
    keywords = [kw.lower() for kw in query.split() if len(kw) > 1]
    try:
        for root, dirs, files in os.walk(search_dir):
            dirs[:] = [d for d in dirs if d not in exclude_dirs and not d.startswith(".")]
            for f in files:
                if not any(f.endswith(ext) for ext in
                           ('.py','.js','.ts','.html','.css','.json','.md','.yaml','.yml','.toml',
                            '.sh','.bat','.cpp','.c','.h','.java','.rs','.go','.rb','.php','.sql','.xml')):
                    continue
                filepath = os.path.join(root, f)
                try:
                    fname_lower = f.lower()
                    name_score = sum(1 for kw in keywords if kw in fname_lower)
                    with open(filepath, "r", encoding="utf-8", errors="replace") as fh:
                        content = fh.read()
                    content_lower = content.lower()
                    score = name_score * 10
                    for kw in keywords:
                        score += content_lower.count(kw)
                    if score > 0:
                        rel = os.path.relpath(filepath, search_dir)
                        match_lines = []
                        for i, line in enumerate(content.split('\n'), 1):
                            if any(kw in line.lower() for kw in keywords):
                                match_lines.append(f"  {i}: {line.strip()[:120]}")
                                if len(match_lines) >= 5:
                                    break
                        results.append({"path": rel, "score": score, "matches": match_lines})
                except Exception:
                    continue
        results.sort(key=lambda x: x["score"], reverse=True)
        results = results[:max_results]
        if not results:
            return f"在代码库中未找到与 '{query}' 相关的文件"
        output = [f"🔍 搜索 '{query}' — {len(results)} 个相关文件:\n"]
        for r in results:
            output.append(f"\n**{r['path']}** (相关度: {r['score']})")
            for ml in r["matches"]:
                output.append(ml)
        return "\n".join(output)
    except Exception as e:
        return f"搜索失败: {e}"


def _handle_manage_todo_list(args, base_dir=None):
    """处理 AI 的任务管理工具调用"""
    from .todo_manager import handle_manage_todo_list
    return handle_manage_todo_list(args, base_dir)


# ── MCP 工具处理 ────────────────────────────────────────────────────

def _handle_mcp_tool(tool_id, func_name, args, base_dir=None):
    """处理 MCP 工具调用 (mcp_<server>__<tool>)"""
    try:
        from .mcp_client import get_mcp_manager
        manager = get_mcp_manager()
        result = manager.call_tool(func_name, args)
        return tool_id, result, ""
    except ImportError:
        return tool_id, "", "MCP 客户端模块不可用"
    except Exception as e:
        logger.exception("MCP 工具 %s 执行失败", func_name)
        return tool_id, "", f"MCP 工具 {func_name} 执行失败: {e}"


def get_mcp_tool_definitions():
    """获取所有 MCP 服务器提供的工具定义（OpenAI 格式）"""
    try:
        from .mcp_client import get_mcp_manager
        manager = get_mcp_manager()
        return manager.get_all_tools()
    except ImportError:
        return []


# ══════════════════════════════════════════════════════════════════════
# 新增工具处理器：子 Agent
# ══════════════════════════════════════════════════════════════════════

def _handle_search_subagent(args, base_dir=None):
    """运行搜索子 Agent"""
    query = args.get("query", "")
    max_results = args.get("max_results", 30)
    if not query:
        return "错误: 未指定搜索查询"
    return run_search_subagent(query, base_dir, max_results)


def _handle_execution_subagent(args, base_dir=None):
    """运行执行子 Agent"""
    command = args.get("command", "")
    timeout = args.get("timeout", 120)
    if not command:
        return "错误: 未指定命令"
    return run_execution_subagent(command, base_dir, timeout)


def _handle_run_fleet(args, base_dir=None):
    """运行 Fleet 模式（并行子 Agent）"""
    tasks = args.get("tasks", [])
    max_concurrent = args.get("max_concurrent", 3)
    if not tasks:
        return "错误: 未指定任务列表"
    return run_fleet(tasks, base_dir, max_concurrent)


# ══════════════════════════════════════════════════════════════════════
# 新增工具处理器：Git 操作
# ══════════════════════════════════════════════════════════════════════

def _handle_get_changed_files(args, base_dir=None):
    """获取工作区已更改的文件"""
    return git_get_changed_files(base_dir)


def _handle_git_log(args, base_dir=None):
    """获取 Git 提交历史"""
    max_count = args.get("max_count", 20)
    return git_log(base_dir, max_count)


def _handle_git_add(args, base_dir=None):
    """暂存文件"""
    files = args.get("files", [])
    if not files:
        return "错误: 未指定文件"
    return git_add(files, base_dir)


def _handle_git_commit(args, base_dir=None):
    """提交暂存的更改"""
    message = args.get("message", "")
    if not message:
        return "错误: 未指定提交信息"
    return git_commit(message, base_dir)


def _handle_git_create_branch(args, base_dir=None):
    """创建并切换到新分支"""
    branch_name = args.get("name", "")
    if not branch_name:
        return "错误: 未指定分支名"
    return git_create_branch(branch_name, base_dir)


# ══════════════════════════════════════════════════════════════════════
# 新增工具处理器：代码审查
# ══════════════════════════════════════════════════════════════════════

def _handle_review_changes(args, base_dir=None):
    """审查代码更改"""
    staged = args.get("staged", False)
    review = git_review_changes(base_dir, staged=staged)
    output = [review["summary"]]
    if review["files"]:
        output.append("\n### 更改的文件")
        for f in review["files"]:
            status_icon = {"M": "📝", "A": "🆕", "D": "🗑", "R": "🔀", "?": "❓"}
            icon = status_icon.get(f["status"], "📄")
            output.append(f"  {icon} {f['path']}")

    if review["diff_content"]:
        diff = review["diff_content"]
        if len(diff) > 10000:
            diff = diff[:10000] + "\n\n...(diff 过长，已截断)..."
        output.append(f"\n### Diff 内容\n```diff\n{diff}\n```")

    return "\n".join(output)


# ══════════════════════════════════════════════════════════════════════
# 新增工具处理器：补丁应用与编辑
# ══════════════════════════════════════════════════════════════════════

def _handle_apply_patch(args, base_dir=None):
    """应用补丁到文件"""
    patch_text = args.get("patch", "")
    if not patch_text:
        return "错误: 未指定补丁内容"
    return apply_patch(patch_text, base_dir)


def _handle_insert_edit(args, base_dir=None):
    """精确的字符串替换编辑"""
    filepath = args.get("filepath", "")
    old_string = args.get("old_string", "")
    new_string = args.get("new_string", "")
    if not filepath:
        return "错误: 未指定文件路径"
    if not old_string:
        return "错误: 未指定要替换的文本"
    return insert_edit(filepath, old_string, new_string, base_dir)


# ══════════════════════════════════════════════════════════════════════
# 新增工具处理器：Notebook 操作
# ══════════════════════════════════════════════════════════════════════

def _handle_create_notebook(args, base_dir=None):
    """创建 Jupyter Notebook"""
    filepath = args.get("filepath", "")
    cells = args.get("cells", None)
    if not filepath:
        return "错误: 未指定文件路径"
    return create_notebook(filepath, cells, base_dir)


def _handle_get_notebook_info(args, base_dir=None):
    """获取 Notebook 摘要"""
    filepath = args.get("filepath", "")
    if not filepath:
        return "错误: 未指定文件路径"
    return get_notebook_info(filepath, base_dir)


def _handle_add_notebook_cell(args, base_dir=None):
    """添加 Notebook 单元格"""
    filepath = args.get("filepath", "")
    cell_type = args.get("cell_type", "code")
    source = args.get("source", "")
    index = args.get("index", None)
    if not filepath:
        return "错误: 未指定文件路径"
    return add_notebook_cell(filepath, cell_type, source, index, base_dir)


def _handle_edit_notebook_cell(args, base_dir=None):
    """编辑 Notebook 单元格"""
    filepath = args.get("filepath", "")
    cell_index = args.get("cell_index", 0)
    new_source = args.get("source", "")
    if not filepath:
        return "错误: 未指定文件路径"
    return edit_notebook_cell(filepath, cell_index, new_source, base_dir)


def _handle_delete_notebook_cell(args, base_dir=None):
    """删除 Notebook 单元格"""
    filepath = args.get("filepath", "")
    cell_index = args.get("cell_index", 0)
    if not filepath:
        return "错误: 未指定文件路径"
    return delete_notebook_cell(filepath, cell_index, base_dir)


# ══════════════════════════════════════════════════════════════════════
# v0.4.1 新增工具处理器
# ══════════════════════════════════════════════════════════════════════

def _handle_multi_replace_string(args, base_dir=None):
    """批量字符串替换"""
    from .tools.extended_tools import multi_replace_string
    return multi_replace_string(args, base_dir)


def _handle_read_project_structure(args, base_dir=None):
    """读取项目结构树"""
    from .tools.extended_tools import read_project_structure
    max_depth = args.get("max_depth", 4)
    return read_project_structure(base_dir, max_depth)


def _handle_find_test_files(args, base_dir=None):
    """查找测试文件"""
    from .tools.extended_tools import find_test_files
    filepath = args.get("filepath", None)
    return find_test_files(filepath, base_dir)


def _handle_search_workspace_symbols(args, base_dir=None):
    """工作区符号搜索"""
    from .tools.extended_tools import search_workspace_symbols
    query = args.get("query", "")
    if not query:
        return "错误: 未指定搜索符号名"
    max_results = args.get("max_results", 50)
    return search_workspace_symbols(query, base_dir, max_results)


def _handle_view_image(args, base_dir=None):
    """查看图像文件"""
    from .tools.extended_tools import view_image
    filepath = args.get("filepath", "")
    if not filepath:
        return "错误: 未指定文件路径"
    return view_image(filepath, base_dir)


def _handle_notebook_run_cell(args, base_dir=None):
    """运行 Notebook 单元格"""
    from .tools.extended_tools import notebook_run_cell
    filepath = args.get("filepath", "")
    cell_index = args.get("cell_index", 0)
    if not filepath:
        return "错误: 未指定文件路径"
    return notebook_run_cell(filepath, cell_index, base_dir)


def _handle_notebook_get_output(args, base_dir=None):
    """获取 Notebook 单元格输出"""
    from .tools.extended_tools import notebook_get_output
    filepath = args.get("filepath", "")
    cell_index = args.get("cell_index", 0)
    if not filepath:
        return "错误: 未指定文件路径"
    return notebook_get_output(filepath, cell_index, base_dir)


def _handle_run_subagent(args, base_dir=None):
    """启动独立子 Agent 会话"""
    from .tools.session_tools import get_session_manager
    title = args.get("title", args.get("description", "子 Agent 任务"))
    prompt = args.get("prompt", "")
    mgr = get_session_manager()
    session = mgr.create_session(title=title)
    # 将任务描述作为第一条用户消息
    if prompt:
        mgr.add_message(session["id"], {"role": "user", "content": prompt})
    return (f"✅ 子 Agent 会话已创建\n"
            f"  ID: {session['id']}\n"
            f"  标题: {session['title']}\n"
            f"  使用 switch_session('{session['id']}') 切换到此会话")


def _handle_compact_conversation(args, base_dir=None):
    """压缩当前对话"""
    from .tools.session_tools import get_session_manager
    mgr = get_session_manager()
    sid = args.get("session_id", mgr._active_id)
    if not sid:
        return "错误: 无活动会话可压缩"
    return mgr.compact_session(sid)


def _handle_list_sessions(args, base_dir=None):
    """列出所有并行会话"""
    from .tools.session_tools import get_session_manager
    mgr = get_session_manager()
    sessions = mgr.list_sessions()
    if not sessions:
        return "当前没有会话"
    lines = [f"📋 共有 {len(sessions)} 个会话:\n"]
    for s in sessions:
        status_icon = {"idle": "💤", "running": "🔄", "done": "✅", "error": "❌"}
        icon = status_icon.get(s.get("status", "idle"), "❓")
        active_mark = " ▶" if s["id"] == mgr._active_id else ""
        lines.append(
            f"  {icon} [{s['id']}] {s['title']}{active_mark} — "
            f"{s['message_count']} 条消息, {s['tool_rounds']} 轮工具"
        )
    return "\n".join(lines)


def _handle_switch_session(args, base_dir=None):
    """切换活动会话"""
    from .tools.session_tools import get_session_manager
    sid = args.get("session_id", "")
    if not sid:
        return "错误: 未指定 session_id"
    mgr = get_session_manager()
    s = mgr.get_session(sid)
    if not s:
        return f"会话 {sid} 不存在"
    mgr.set_active(sid)
    return f"✅ 已切换到会话: {s['title']} ({sid})\n消息数: {len(s['messages'])}"


# ── Tree-sitter AST 代码理解工具 ──────────────────────────────────

from .tools.tree_sitter_tool_handlers import (
    handle_get_symbol_definitions as ts_get_symbol_definitions,
    handle_get_code_outline as ts_get_code_outline,
    handle_find_related_symbols as ts_find_related_symbols,
    handle_get_call_hierarchy as ts_get_call_hierarchy,
    handle_get_symbol_at_position as ts_get_symbol_at_position,
)

def _handle_get_symbol_definitions(args, base_dir=None):
    return ts_get_symbol_definitions(args, base_dir)

def _handle_get_code_outline(args, base_dir=None):
    return ts_get_code_outline(args, base_dir)

def _handle_find_related_symbols(args, base_dir=None):
    return ts_find_related_symbols(args, base_dir)

def _handle_get_call_hierarchy(args, base_dir=None):
    return ts_get_call_hierarchy(args, base_dir)

def _handle_get_symbol_at_position(args, base_dir=None):
    return ts_get_symbol_at_position(args, base_dir)


# ── Git 冲突解决工具 ────────────────────────────────────────────

from .tools.git_conflict_resolver import (
    detect_conflicts as git_detect_conflicts,
    get_conflicted_files as git_get_conflicted_files,
    format_conflict_report,
)

def _handle_detect_conflicts(args, base_dir=None):
    """检测合并冲突"""
    filepath = args.get("filepath", "")
    if filepath:
        if base_dir and not os.path.isabs(filepath):
            filepath = os.path.join(base_dir, filepath)
        if not os.path.isfile(filepath):
            return f"❌ 文件不存在: {filepath}"
        conflicts = git_detect_conflicts(filepath)
        if not conflicts:
            return f"✅ 无冲突: {os.path.basename(filepath)}"
        return format_conflict_report(conflicts, filepath)
    else:
        files = git_get_conflicted_files(base_dir or os.getcwd())
        if not files:
            return "✅ 未检测到合并冲突"
        lines = [f"⚠️ 发现 {len(files)} 个文件包含冲突:\n"]
        for f in files:
            rel = os.path.relpath(f, base_dir or os.getcwd())
            lines.append(f"- `{rel}`")
            conflicts = git_detect_conflicts(f)
            if conflicts:
                lines.append(f"  ({len(conflicts)} 个冲突)")
        return "\n".join(lines)


def _handle_resolve_conflict(args, base_dir=None):
    """解决合并冲突"""
    filepath = args.get("filepath", "")
    conflict_index = args.get("conflict_index", 0)
    context_hint = args.get("context_hint", "")

    if not filepath:
        return "❌ 错误: 未指定 filepath"
    if base_dir and not os.path.isabs(filepath):
        filepath = os.path.join(base_dir, filepath)
    if not os.path.isfile(filepath):
        return f"❌ 文件不存在: {filepath}"

    conflicts = git_detect_conflicts(filepath)
    if not conflicts:
        return f"✅ 无冲突: {os.path.basename(filepath)}"
    if conflict_index >= len(conflicts):
        return (f"❌ 冲突索引 {conflict_index} 超出范围 "
                f"(共 {len(conflicts)} 个)")

    conflict = conflicts[conflict_index]

    # 构造冲突信息供 AI 分析
    resolution_prompt = (
        f"请解决以下 Git 合并冲突，将冲突的两个版本合并为一个正确、可工作的结果。"
        f"尽量保留双方的意图。\n\n"
    )
    if context_hint:
        resolution_prompt += f"上下文: {context_hint}\n\n"
    resolution_prompt += (
        f"<<<<<<< 我们的版本\n"
        f"{conflict['ours']}\n"
        f"=======\n"
        f"{conflict['theirs']}\n"
        f">>>>>>> 他们的版本\n\n"
        f"请只输出解决后的内容，不要添加额外解释。"
    )

    return json.dumps({
        "action": "resolve_conflict",
        "filepath": filepath,
        "start_line": conflict["start_line"],
        "end_line": conflict["end_line"],
        "ours": conflict["ours"],
        "theirs": conflict["theirs"],
        "resolution_prompt": resolution_prompt,
    })


# ── 工具注册表 ──────────────────────────────────────────────────────

TOOL_HANDLERS = {
    # 文件操作
    "read_file": _handle_read_file,
    "create_file": _handle_create_file,
    "edit_file": _handle_edit_file,
    "rename_file": _handle_rename_file,
    "delete_file": _handle_delete_file,
    "create_directory": _handle_create_directory,

    # 搜索
    "search_files": _handle_search_files,
    "text_search": _handle_text_search,
    "list_directory": _handle_list_directory,
    "codebase_search": _handle_codebase_search,

    # 执行
    "run_command": _handle_run_command,
    "get_errors": _handle_get_errors,

    # 网络
    "fetch_webpage": _handle_fetch_webpage,

    # 记忆
    "memory_save": _handle_memory_save,
    "memory_recall": _handle_memory_recall,

    # 项目管理
    "manage_todo_list": _handle_manage_todo_list,
    "get_current_file": _handle_get_current_file,

    # ── 新增：子 Agent 系统 ──
    "search_subagent": _handle_search_subagent,
    "execution_subagent": _handle_execution_subagent,
    "run_fleet": _handle_run_fleet,

    # ── 新增：Git 操作 ──
    "get_changed_files": _handle_get_changed_files,
    "git_log": _handle_git_log,
    "git_add": _handle_git_add,
    "git_commit": _handle_git_commit,
    "git_create_branch": _handle_git_create_branch,

    # ── 新增：代码审查 ──
    "review_changes": _handle_review_changes,

    # ── 新增：编辑工具 ──
    "apply_patch": _handle_apply_patch,
    "insert_edit": _handle_insert_edit,

    # ── 新增：Notebook 操作 ──
    "create_notebook": _handle_create_notebook,
    "get_notebook_info": _handle_get_notebook_info,
    "add_notebook_cell": _handle_add_notebook_cell,
    "edit_notebook_cell": _handle_edit_notebook_cell,
    "delete_notebook_cell": _handle_delete_notebook_cell,

    # ── 新增 v0.4.1：扩展工具 ──
    "multi_replace_string": _handle_multi_replace_string,
    "read_project_structure": _handle_read_project_structure,
    "find_test_files": _handle_find_test_files,
    "search_workspace_symbols": _handle_search_workspace_symbols,
    "view_image": _handle_view_image,
    "notebook_run_cell": _handle_notebook_run_cell,
    "notebook_get_output": _handle_notebook_get_output,

    # ── 新增 v0.4.1：会话工具 ──
    "run_subagent": _handle_run_subagent,
    "compact_conversation": _handle_compact_conversation,
    "list_sessions": _handle_list_sessions,
    "switch_session": _handle_switch_session,

    # ── 新增：Tree-sitter AST 代码理解 ──
    "get_symbol_definitions": _handle_get_symbol_definitions,
    "get_code_outline": _handle_get_code_outline,
    "find_related_symbols": _handle_find_related_symbols,
    "get_call_hierarchy": _handle_get_call_hierarchy,
    "get_symbol_at_position": _handle_get_symbol_at_position,

    # ── 新增：Git 冲突解决 ──
    "detect_conflicts": _handle_detect_conflicts,
    "resolve_conflict": _handle_resolve_conflict,
}


# ── 辅助函数 ─────────────────────────────────────────────────────────

def _parse_robust_json(raw_text, tool_name=""):
    """健壮的 JSON 参数解析器

    AI 模型生成的工具参数经常包含未转义的引号、换行符等。
    此函数尝试多种策略解析，最大限度容忍格式错误。

    策略优先级:
      1. 标准 json.loads
      2. 修复常见转义问题后 json.loads
      3. 提取字符串值（针对单参数工具如 run_command）
      4. 返回错误信息
    """
    text = raw_text.strip() if raw_text else "{}"
    if not text:
        return {}

    # ── 策略 1: 标准 JSON 解析 ──
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # ── 策略 2: 修复常见问题 ──
    repaired = _repair_json(text)
    try:
        return json.loads(repaired)
    except json.JSONDecodeError:
        pass

    # ── 策略 3: 针对 run_command / create_file 的提取策略 ──
    if tool_name in ("run_command",):
        extracted = _extract_command_value(text)
        if extracted:
            logger.debug("Extracted command from malformed JSON")
            return {"command": extracted}

    if tool_name in ("create_file",):
        result = _extract_file_fields(text)
        if result:
            logger.debug("Extracted file fields from malformed JSON")
            return result

    # ── 策略 4: 尝试用 ast.literal_eval (Python dict 格式) ──
    try:
        import ast
        return ast.literal_eval(text)
    except (ValueError, SyntaxError):
        pass

    # ── 全部失败 ──
    preview = text[:300].replace('\n', '\\n')
    return (
        f"参数解析失败: JSON 格式错误。\n"
        f"原始参数 (前300字符): {preview}\n"
        f"提示: 在命令中避免使用未转义的引号。"
        f"如需在命令中使用引号，请使用 \\\" 转义。"
    )


def _repair_json(text):
    """尝试修复常见的 AI 生成 JSON 错误"""
    # 1. 处理被截断的 JSON（末尾缺 }）
    if text.count('{') > text.count('}'):
        text = text.rstrip() + '}'

    # 2. 处理末尾多余逗号: "key": "value",} → "key": "value"}
    text = re.sub(r',\s*}', '}', text)
    text = re.sub(r',\s*]', ']', text)

    # 3. 修复 key 缺少引号: {command: "ls"} → {"command": "ls"}
    #    匹配 { 或 , 后面跟的非引号包裹的 key
    text = re.sub(r'([{,])\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*:',
                  r'\1"\2":', text)

    # 4. 修复字符串值中的未转义换行符
    #    找到 "key": "value 跨行 ..." 的模式
    def _fix_multiline_str(m):
        key = m.group(1)
        val = m.group(2)
        # 转义内部的换行和制表符
        val = val.replace('\n', '\\n').replace('\r', '\\r').replace('\t', '\\t')
        return f'"{key}": "{val}"'

    text = re.sub(
        r'"([^"]+)"\s*:\s*"([^"]*?\n[^"]*?)"',
        _fix_multiline_str,
        text,
        flags=re.DOTALL,
    )

    # 5. 修复值中未转义的双引号:
    #    "command": "echo "hello"" → "command": "echo \"hello\""
    #    策略: 找到 "key": " 开头，到下一个未转义的 " 之前，然后
    #    内部的双引号需要转义
    def _fix_inner_quotes(m):
        key = m.group(1)
        val = m.group(2)
        # 转义内部的未转义双引号
        # 简单策略: 找到所有不在开头/结尾的双引号并转义
        if '"' in val:
            # 用占位符标记，然后替换
            val = val.replace('\\"', '\x00ESCAPED\x00')
            val = val.replace('"', '\\"')
            val = val.replace('\x00ESCAPED\x00', '\\"')
        return f'"{key}": "{val}"'

    text = re.sub(
        r'"([^"]+)"\s*:\s*"([^"]*)"',
        _fix_inner_quotes,
        text,
    )

    return text


def _extract_command_value(text):
    """从格式错误的 JSON 中提取 command 字段的值

    处理类似这样的输入:
      {"command": "cd /d D:\\path && python -c \"...复杂代码...\""}
    """
    # 尝试正则提取 "command" 后的值
    # 匹配: "command": "..."  或  "command": '...'
    patterns = [
        r'"command"\s*:\s*"((?:[^"\\]|\\.)*)"',  # 双引号值
        r'"command"\s*:\s*\'((?:[^\'\\]|\\.)*)\'',  # 单引号值
    ]
    for pattern in patterns:
        m = re.search(pattern, text, re.DOTALL)
        if m:
            val = m.group(1)
            # 还原转义
            val = val.replace('\\"', '"').replace('\\n', '\n').replace('\\t', '\t')
            val = val.replace('\\\\', '\\')
            if val.strip():
                return val

    # 更激进的提取: 从第一个 "command": 到文本末尾
    # 适用于被截断的 JSON
    m = re.search(r'"command"\s*:\s*"(.+)', text, re.DOTALL)
    if m:
        val = m.group(1)
        # 去掉末尾可能的 " 和 }
        val = re.sub(r'["}]*$', '', val)
        val = val.replace('\\"', '"').replace('\\n', '\n').replace('\\t', '\t')
        val = val.replace('\\\\', '\\')
        if len(val) > 10:
            return val

    return None


def _extract_file_fields(text):
    """从格式错误的 JSON 中提取 filepath 和 content"""
    result = {}
    # 提取 filepath
    m = re.search(r'"filepath"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
    if m:
        result["filepath"] = m.group(1).replace('\\"', '"')
    # 提取 content（可能跨越多行）
    m = re.search(r'"content"\s*:\s*"(.+)', text, re.DOTALL)
    if m:
        val = m.group(1)
        val = re.sub(r'["}]*$', '', val)
        val = val.replace('\\"', '"').replace('\\n', '\n').replace('\\t', '\t')
        val = val.replace('\\\\', '\\')
        result["content"] = val
    return result if result else None


def _resolve_path(path, base_dir=None):
    """解析路径：如果是相对路径则拼接到 base_dir"""
    if os.path.isabs(path):
        return os.path.normpath(path)
    if base_dir:
        return os.path.normpath(os.path.join(base_dir, path))
    return os.path.normpath(os.path.abspath(path))
