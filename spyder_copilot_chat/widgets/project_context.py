# -*- coding: utf-8 -*-
"""项目上下文收集 — 参考 vscode-copilot-chat 的 @workspace / 代码搜索"""

import fnmatch
import hashlib
import os

DEFAULT_EXCL_DIRS = {
    ".git", ".hg", ".svn", "__pycache__", ".mypy_cache",
    ".venv", "venv", "env", "node_modules", "dist", "build",
    ".idea", ".vscode", ".tox", ".egg-info",
}

DEFAULT_EXCL_PATTERNS = (
    "*.pyc", "*.pyo", "*.pyd", "*.egg",
    "*.png", "*.jpg", "*.jpeg", "*.gif", "*.svg", "*.ico",
    "*.zip", "*.tar", "*.gz", "*.rar",
    "*.exe", "*.dll", "*.so", "*.bin",
    "*.db", "*.sqlite",
    ".DS_Store", "Thumbs.db",
    "*.lock", "*.min.js", "*.min.css",
    "*.tiktoken", "*.wasm",
)


def _lang(path):
    ext = os.path.splitext(path)[1].lower()
    return {
        ".py": "python", ".js": "javascript", ".ts": "typescript",
        ".html": "html", ".css": "css", ".json": "json",
        ".md": "markdown", ".yaml": "yaml", ".yml": "yaml",
        ".toml": "toml", ".sh": "bash", ".bat": "batch",
        ".cpp": "cpp", ".c": "c", ".h": "c",
        ".java": "java", ".rs": "rust", ".go": "go",
        ".rb": "ruby", ".php": "php", ".cs": "csharp",
        ".r": "r", ".sql": "sql", ".xml": "xml",
        ".kt": "kotlin", ".swift": "swift",
    }.get(ext, "")


def collect_project_files(root_dir, max_files=50, max_size_kb=100):
    """收集项目文件列表"""
    results = []
    try:
        for dirpath, dirnames, filenames in os.walk(root_dir):
            dirnames[:] = [d for d in dirnames if d not in DEFAULT_EXCL_DIRS
                          and not d.startswith(".")]
            for f in filenames:
                if any(fnmatch.fnmatch(f, p) for p in DEFAULT_EXCL_PATTERNS):
                    continue
                fp = os.path.join(dirpath, f)
                try:
                    if os.path.getsize(fp) > max_size_kb * 1024:
                        continue
                    rel = os.path.relpath(fp, root_dir)
                    lang = _lang(f)
                    results.append({
                        "path": rel,
                        "abs_path": fp,
                        "lang": lang,
                        "size": os.path.getsize(fp),
                    })
                except OSError:
                    continue
                if len(results) >= max_files:
                    break
            if len(results) >= max_files:
                break
    except Exception:
        pass
    return results


def read_file_content(filepath, max_chars=8000):
    """读取文件内容"""
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            content = f.read(max_chars)
        if os.path.getsize(filepath) > max_chars:
            content += "\n\n[文件截断，仅显示前{}字符]".format(max_chars)
        return content
    except Exception:
        return ""


def format_file_for_context(file_info):
    """格式化文件为上下文块"""
    content = read_file_content(file_info["abs_path"])
    lang = file_info.get("lang", "")
    fence = f"```{lang}" if lang else "```"
    return f"### File: {file_info['path']}\n{fence}\n{content}\n```\n---\n"
