# -*- coding: utf-8 -*-
"""
轻量仓库结构索引 — v0.5.6 / P1

不依赖向量库 / LSP，生成「目录树 + 顶层符号」的紧凑仓库地图：
  * 后台构建（submit 到线程池），结果缓存到
    ~/.spyder_copilot_chat/index/<hash>.json
  * 主线程只读内存 / 磁盘缓存，绝不遍历文件系统
  * 注入系统提示词时受字符预算控制；也提供 search_repo_map 工具按需检索

适合大仓库快速定位「哪个文件里有这个函数」，命中后再用 read_file 精读。
"""

import hashlib
import json
import logging
import os
import re
import time

logger = logging.getLogger(__name__)

CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".spyder_copilot_chat")
INDEX_DIR = os.path.join(CONFIG_DIR, "index")

SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    "env", "dist", "build", ".tox", ".mypy_cache", ".pytest_cache",
    ".idea", ".vscode", "site-packages", "target", "vendor", "coverage",
}
SKIP_EXT = {
    ".pyc", ".pyo", ".so", ".dll", ".exe", ".bin", ".png", ".jpg",
    ".jpeg", ".gif", ".ico", ".pdf", ".zip", ".gz", ".tar", ".7z",
    ".whl", ".woff", ".woff2", ".ttf", ".mp4", ".mp3", ".sqlite",
    ".db", ".lock", ".min.js", ".map",
}
SOURCE_EXT = {".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".go", ".rs",
              ".c", ".cpp", ".h", ".hpp", ".cs", ".rb", ".php", ".md",
              ".json", ".yaml", ".yml", ".toml", ".sh", ".bat"}

_SYMBOL_PATTERNS = {
    ".py": re.compile(r"^\s*(?:async\s+)?(?:def|class)\s+([A-Za-z_]\w*)",
                      re.MULTILINE),
    ".js": re.compile(r"^\s*(?:export\s+(?:default\s+)?)?"
                      r"(?:async\s+)?(?:function|class|const|let)\s+"
                      r"([A-Za-z_$]\w*)", re.MULTILINE),
    ".ts": re.compile(r"^\s*(?:export\s+(?:default\s+)?)?"
                      r"(?:async\s+)?(?:function|class|const|let|interface|type)\s+"
                      r"([A-Za-z_$]\w*)", re.MULTILINE),
    ".go": re.compile(r"^func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)",
                      re.MULTILINE),
    ".rs": re.compile(r"^\s*(?:pub\s+)?(?:fn|struct|enum|trait|impl)\s+"
                      r"([A-Za-z_]\w*)", re.MULTILINE),
    ".java": re.compile(r"^\s*(?:public\s+|private\s+|protected\s+|static\s+)*"
                        r"(?:class|interface|enum)\s+([A-Za-z_]\w*)",
                        re.MULTILINE),
    ".cs": re.compile(r"^\s*(?:public\s+|private\s+|static\s+)*"
                      r"(?:class|interface|struct|enum)\s+([A-Za-z_]\w*)",
                      re.MULTILINE),
    ".md": re.compile(r"^#{1,3}\s+(.+)$", re.MULTILINE),
}
for _ext in (".jsx", ".tsx"):
    _SYMBOL_PATTERNS[_ext] = _SYMBOL_PATTERNS[".ts"]
for _ext in (".c", ".cpp", ".h", ".hpp"):
    _SYMBOL_PATTERNS[_ext] = re.compile(
        r"^\s*(?:[A-Za-z_]\w*[\s\*]+)+([A-Za-z_]\w*)\s*\(", re.MULTILINE)

MAX_FILE_BYTES = 400_000
MAX_SYMBOLS_PER_FILE = 30

_MEM_CACHE = {}          # root -> (cache_key, index_dict)


def cache_path(root):
    key = hashlib.sha1(os.path.normcase(os.path.abspath(root))
                       .encode("utf-8")).hexdigest()[:16]
    return os.path.join(INDEX_DIR, key + ".json")


def _ext_of(path):
    ext = os.path.splitext(path)[1].lower()
    return ".min.js" if path.lower().endswith(".min.js") else ext


def _extract_symbols(path, ext):
    pattern = _SYMBOL_PATTERNS.get(ext)
    if not pattern:
        return []
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            text = f.read(MAX_FILE_BYTES)
    except OSError:
        return []
    out = []
    for m in pattern.finditer(text):
        name = m.group(1).strip()
        if name and name not in out:
            out.append(name[:60])
        if len(out) >= MAX_SYMBOLS_PER_FILE:
            break
    return out


def build_index(root, max_files=1500, max_depth=5):
    """遍历生成索引。纯计算 + 文件 IO，必须在后台线程执行。"""
    root = os.path.abspath(root or os.getcwd())
    files, dirs_seen = [], set()
    started = time.time()
    if not os.path.isdir(root):
        index = {"root": root, "files": [], "tree": "", "truncated": False,
                 "built_at": time.time()}
        _save(root, index)
        return index

    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        depth = 0 if rel_dir == "." else rel_dir.count(os.sep) + 1
        dirnames[:] = [d for d in dirnames
                       if d not in SKIP_DIRS and not d.startswith(".")]
        if depth >= max_depth:
            dirnames[:] = []
        if depth <= 3:
            dirs_seen.add("" if rel_dir == "." else
                          rel_dir.replace(os.sep, "/") + "/")
        for fn in sorted(filenames):
            if fn.startswith("."):
                continue
            ext = _ext_of(fn)
            if ext in SKIP_EXT or ext not in SOURCE_EXT:
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            files.append({"path": rel, "symbols": _extract_symbols(full, ext)})
            if len(files) >= max_files:
                break
        if len(files) >= max_files:
            break

    tree_lines = []
    for d in sorted(dirs_seen)[:180]:
        if d:
            tree_lines.append("  " * (d.count("/") - 1) + d.rstrip("/") + "/")
    index = {
        "root": root,
        "built_at": time.time(),
        "files": files,
        "tree": "\n".join(tree_lines),
        "truncated": len(files) >= max_files,
        "elapsed": round(time.time() - started, 3),
    }
    _save(root, index)
    return index


def _save(root, index):
    try:
        os.makedirs(INDEX_DIR, exist_ok=True)
        tmp = cache_path(root) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(index, f, ensure_ascii=False)
        os.replace(tmp, cache_path(root))
        _MEM_CACHE[os.path.normcase(root)] = (index.get("built_at"), index)
    except OSError as exc:
        logger.debug("仓库索引缓存写入失败: %s", exc)


def _load(root):
    key = os.path.normcase(os.path.abspath(root))
    cached = _MEM_CACHE.get(key)
    if cached:
        return cached[1]
    path = cache_path(root)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            index = json.load(f)
        _MEM_CACHE[key] = (index.get("built_at"), index)
        return index
    except (OSError, ValueError) as exc:
        logger.debug("仓库索引缓存读取失败: %s", exc)
        return None


def ensure_index_async(root, on_done, owner):
    """后台构建 / 刷新索引，结果通过 submit 的回调回到主线程。"""
    from .task_runtime import submit
    root = os.path.abspath(root or os.getcwd())
    return submit(lambda: build_index(root), on_done, owner)


def invalidate(root):
    root = os.path.abspath(root or os.getcwd())
    _MEM_CACHE.pop(os.path.normcase(root), None)
    try:
        os.remove(cache_path(root))
    except OSError:
        pass


def format_repo_map(root, budget=6000, enabled=True):
    """生成注入系统提示词的紧凑仓库地图（只读缓存，不触发遍历）。"""
    if not enabled:
        return ""
    index = _load(root)
    if not index or not index.get("files"):
        return ""
    try:
        budget = max(500, int(budget))
    except (TypeError, ValueError):
        budget = 6000
    lines = [
        "## 仓库结构索引（自动生成的仓库地图）",
        "需要定位文件时优先用 `search_repo_map` 检索文件名或符号，"
        "再用 `read_file` 精读，不要盲目遍历目录。",
        "",
        "### 目录树",
        "```",
        (index.get("tree") or "(空)")[:1500],
        "```",
        "",
        "### 文件与顶层符号",
    ]
    used = sum(len(x) + 1 for x in lines)
    for item in index["files"]:
        syms = ", ".join(item.get("symbols") or [])
        line = f"- {item['path']}" + (f": {syms}" if syms else "")
        used += len(line) + 1
        if used > budget:
            lines.append("…（超出预算，其余文件请用 search_repo_map 检索）")
            break
        lines.append(line)
    text = "\n".join(lines)
    if index.get("truncated"):
        text += "\n（索引已按文件数上限截断）"
    return text[:budget]


def search_repo_map(root, query, limit=30):
    """按文件名 / 符号检索索引；返回供模型使用的文本。"""
    index = _load(root)
    if not index:
        return ("仓库索引尚未生成。请点工具栏「🗺 索引」重建，"
                "或稍后再试（首次启动会在后台自动构建）。")
    q = (query or "").strip().lower()
    if not q:
        return "请提供检索关键词（文件名或符号名）。"
    hits = []
    for item in index.get("files", []):
        path = item.get("path", "")
        syms = item.get("symbols") or []
        matched = [s for s in syms if q in s.lower()]
        score = 2 if q in path.lower() else (1 if matched else 0)
        if score:
            hits.append((score, path, matched[:8]))
    hits.sort(key=lambda x: (-x[0], x[1]))
    hits = hits[:max(1, int(limit or 30))]
    if not hits:
        return f"索引中没有匹配 `{query}` 的文件或符号。"
    lines = [f"仓库索引匹配 `{query}`（{len(hits)} 项）："]
    for _score, path, syms in hits:
        lines.append(f"- {path}" + (f"  → {', '.join(syms)}" if syms else ""))
    return "\n".join(lines)


def index_summary(root):
    index = _load(root)
    if not index:
        return "尚未生成"
    return (f"{len(index.get('files', []))} 个文件 · "
            f"构建于 {time.strftime('%H:%M:%S', time.localtime(index.get('built_at', 0)))}"
            + ("（已截断）" if index.get("truncated") else ""))
