# -*- coding: utf-8 -*-
"""
Git 工具集

对标 GitHub Copilot Chat 的:
  - getChangedFiles: 获取工作区中的更改文件（Git diff）
  - code review: 代码审查功能
  - review changes: 审查未提交/已暂存的更改
"""

import logging
import os
import subprocess
import sys

logger = logging.getLogger(__name__)

_WINDOWS = sys.platform == "win32"
_SUBPROCESS_FLAGS = subprocess.CREATE_NO_WINDOW if _WINDOWS else 0


def _run_git(cmd, cwd):
    """运行 git 命令并返回输出"""
    try:
        result = subprocess.run(
            ["git"] + cmd,
            cwd=cwd,
            capture_output=True,
            encoding='utf-8',
            errors='replace',
            timeout=30,
            creationflags=_SUBPROCESS_FLAGS,
        )
        if result.returncode != 0:
            return None, result.stderr.strip()
        return result.stdout, None
    except FileNotFoundError:
        return None, "Git 未安装或不在 PATH 中"
    except subprocess.TimeoutExpired:
        return None, "Git 命令超时"
    except Exception as e:
        return None, str(e)


def is_git_repo(path):
    """检查指定路径是否为 Git 仓库"""
    out, err = _run_git(["rev-parse", "--git-dir"], path)
    return out is not None


def get_changed_files(base_dir=None):
    """获取工作区中已更改文件的列表和 diff

    对标 VS Code 的 copilot.tools.getChangedFiles

    Parameters
    ----------
    base_dir : str or None
        项目根目录

    Returns
    -------
    str : 格式化的更改文件列表 + diff 摘要
    """
    cwd = base_dir or os.getcwd()

    if not is_git_repo(cwd):
        return "当前目录不是 Git 仓库，无法获取更改信息"

    # 获取更改状态
    out, err = _run_git(["status", "--short"], cwd)
    if out is None:
        return f"无法获取 Git 状态: {err}"

    if not out.strip():
        return "✅ 工作区干净，无未提交的更改"

    # 解析更改文件
    staged = []
    unstaged = []
    untracked = []

    for line in out.strip().split("\n"):
        if len(line) < 3:
            continue
        status = line[:2]
        filepath = line[3:]
        if status[0] != " ":
            staged.append((status[0], filepath))
        if status[1] != " ":
            unstaged.append((status[1], filepath))
        if status[0] == "?" and status[1] == "?":
            untracked.append(filepath)

    lines = [f"📂 **Git 更改** — 仓库: {cwd}\n"]

    if staged:
        lines.append(f"### 📌 已暂存 ({len(staged)} 个文件)")
        for s, f in staged:
            label = {"M": "修改", "A": "新增", "D": "删除", "R": "重命名",
                     "C": "复制"}.get(s, s)
            lines.append(f"  - {label}  {f}")
        lines.append("")

    if unstaged:
        lines.append(f"### 📝 未暂存 ({len(unstaged)} 个文件)")
        for s, f in unstaged:
            label = {"M": "修改", "D": "删除", "?" : "未知"}.get(s, s)
            lines.append(f"  - {label}  {f}")
        lines.append("")

    if untracked:
        lines.append(f"### 🆕 未跟踪 ({len(untracked)} 个文件)")
        for f in untracked[:20]:
            lines.append(f"  - {f}")
        if len(untracked) > 20:
            lines.append(f"  ... 以及另外 {len(untracked) - 20} 个文件")
        lines.append("")

    # 获取 diff 摘要（仅未暂存更改）
    diff_out, diff_err = _run_git(["diff", "--stat"], cwd)
    if diff_out:
        lines.append("### Diff 统计 (未暂存)")
        lines.append(f"```\n{diff_out.strip()}\n```")

    # 获取完整的 staged diff
    staged_diff, _ = _run_git(["diff", "--cached", "--stat"], cwd)
    if staged_diff:
        lines.append("### Diff 统计 (已暂存)")
        lines.append(f"```\n{staged_diff.strip()}\n```")

    return "\n".join(lines)


def get_changed_files_diff(base_dir=None, staged=False, max_lines=500):
    """获取更改文件的完整 diff

    Parameters
    ----------
    base_dir : str or None
        项目根目录
    staged : bool
        是否获取已暂存的 diff
    max_lines : int
        最大行数

    Returns
    -------
    str : 格式化的 diff 内容
    """
    cwd = base_dir or os.getcwd()

    if not is_git_repo(cwd):
        return "当前目录不是 Git 仓库"

    cmd = ["diff", "--cached"] if staged else ["diff"]
    diff_out, err = _run_git(cmd, cwd)
    if diff_out is None:
        return f"无法获取 diff: {err}"

    if not diff_out.strip():
        return "无更改"

    # 截断
    lines = diff_out.split("\n")
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines.append(f"\n... (diff 共 {len(lines)} 行，仅显示前 {max_lines} 行)")

    return "\n".join(lines)


def get_file_diff(filepath, base_dir=None):
    """获取单个文件的 diff"""
    cwd = base_dir or os.getcwd()
    if not is_git_repo(cwd):
        return "当前目录不是 Git 仓库"

    rel = os.path.relpath(filepath, cwd)
    diff_out, err = _run_git(["diff", rel], cwd)
    if diff_out is None:
        return f"无法获取 diff: {err}"
    if not diff_out.strip():
        return f"文件 '{rel}' 无更改"
    return diff_out


# ══════════════════════════════════════════════════════════════════════
# 代码审查
# ══════════════════════════════════════════════════════════════════════

def review_changes(base_dir=None, staged=False):
    """审查代码更改

    对标 VS Code 的 code review 功能：
    收集所有更改文件的 diff，为 AI 提供审查上下文

    Parameters
    ----------
    base_dir : str or None
        项目根目录
    staged : bool
        是否审查已暂存的更改

    Returns
    -------
    dict : {"summary": str, "files": list, "diff_content": str}
    """
    cwd = base_dir or os.getcwd()

    if not is_git_repo(cwd):
        return {
            "summary": "当前目录不是 Git 仓库",
            "files": [],
            "diff_content": "",
        }

    # 获取更改文件列表
    out, _ = _run_git(["status", "--short"], cwd)
    files = []
    if out:
        for line in out.strip().split("\n"):
            if len(line) >= 3:
                status = line[:2].strip()
                fpath = line[3:]
                files.append({"path": fpath, "status": status})

    # 获取 diff
    diff_cmd = ["diff", "--cached"] if staged else ["diff"]
    diff_out, _ = _run_git(diff_cmd, cwd)
    diff_content = diff_out or ""

    # 生成摘要
    if not files:
        summary = "✅ 无代码更改需要审查"
    else:
        modified = sum(1 for f in files if f["status"] in ("M", ""))
        added = sum(1 for f in files if f["status"] == "A")
        deleted = sum(1 for f in files if f["status"] == "D")
        renamed = sum(1 for f in files if f["status"] == "R")
        parts = []
        if modified:
            parts.append(f"{modified} 个修改")
        if added:
            parts.append(f"{added} 个新增")
        if deleted:
            parts.append(f"{deleted} 个删除")
        if renamed:
            parts.append(f"{renamed} 个重命名")
        summary = f"📋 代码审查 — {', '.join(parts)}，共 {len(files)} 个文件"

    return {
        "summary": summary,
        "files": files,
        "diff_content": diff_content,
    }


def review_file(filepath, base_dir=None):
    """审查单个文件的更改"""
    cwd = base_dir or os.getcwd()
    rel = os.path.relpath(filepath, cwd)
    diff_out = get_file_diff(filepath, base_dir)

    # 获取文件当前内容
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except Exception as e:
        content = f"(无法读取: {e})"

    return {
        "file": rel,
        "diff": diff_out,
        "content": content,
    }


# ══════════════════════════════════════════════════════════════════════
# 添加/提交/分支操作
# ══════════════════════════════════════════════════════════════════════

def git_add(files, base_dir=None):
    """暂存文件"""
    cwd = base_dir or os.getcwd()
    if not is_git_repo(cwd):
        return "当前目录不是 Git 仓库"
    out, err = _run_git(["add"] + files, cwd)
    if err:
        return f"暂存失败: {err}"
    return f"✅ 已暂存: {', '.join(files)}"


def git_commit(message, base_dir=None):
    """提交暂存的更改"""
    cwd = base_dir or os.getcwd()
    if not is_git_repo(cwd):
        return "当前目录不是 Git 仓库"
    out, err = _run_git(["commit", "-m", message], cwd)
    if err:
        return f"提交失败: {err}"
    return f"✅ 已提交:\n{out}"


def git_create_branch(branch_name, base_dir=None):
    """创建并切换到新分支"""
    cwd = base_dir or os.getcwd()
    if not is_git_repo(cwd):
        return "当前目录不是 Git 仓库"
    out, err = _run_git(["checkout", "-b", branch_name], cwd)
    if err:
        return f"创建分支失败: {err}"
    return f"✅ 已创建并切换到分支: {branch_name}"


def git_log(base_dir=None, max_count=20):
    """获取提交历史"""
    cwd = base_dir or os.getcwd()
    if not is_git_repo(cwd):
        return "当前目录不是 Git 仓库"
    out, err = _run_git(
        ["log", f"--max-count={max_count}",
         "--pretty=format:%h %ai %an%n  %s%n"],
        cwd
    )
    if err:
        return f"获取日志失败: {err}"
    if not out:
        return "无提交历史"
    lines = [f"📜 **Git 日志** — 最近 {max_count} 条提交\n"]
    lines.append(f"```\n{out}\n```")
    return "\n".join(lines)
