# -*- coding: utf-8 -*-
"""
项目规则文件 — v0.5.6 / P1

对标 AGENTS.md / CLAUDE.md / .clinerules / .cursorrules 的规则体系：
  * 多来源：项目根 + 条件规则目录 + 用户级规则
  * 条件规则：frontmatter 的 globs / applyTo 决定对哪些文件生效
  * 自动注入系统提示词，带来源标签与字符预算，避免挤爆上下文

本模块是纯 Python（不依赖 Qt），可安全地在后台线程调用。
"""

import fnmatch
import logging
import os
import re
from pathlib import Path, PurePosixPath

logger = logging.getLogger(__name__)

CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".spyder_copilot_chat")

# 项目根下常见的「平铺」规则文件（按优先级排序，全部收集但去重）
RULE_FILENAMES = (
    "AGENTS.md",
    "CLAUDE.md",
    "GEMINI.md",
    "copilot-instructions.md",
    ".clinerules",
    ".cursorrules",
    os.path.join(".github", "copilot-instructions.md"),
    os.path.join(".spydercopilot", "RULES.md"),
)

# 条件规则目录：目录下的 *.md 支持 globs / applyTo
RULE_DIRS = (
    os.path.join(".spydercopilot", "rules"),
    os.path.join(".clinerules"),
    os.path.join(".claude", "rules"),
)

USER_RULE_DIR = os.path.join(CONFIG_DIR, "rules")

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_KV_RE = re.compile(r"^([\w-]+)\s*:\s*(.*?)$")

MAX_RULE_CHARS = 6000


def _parse_frontmatter(text):
    """返回 (meta, body)。不依赖 PyYAML，支持标量、[a, b] 列表与布尔。"""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    meta = {}
    for line in m.group(1).strip().splitlines():
        kv = _KV_RE.match(line.strip())
        if not kv:
            continue
        key, val = kv.group(1).strip().lower(), kv.group(2).strip()
        if val[:1] in ('"', "'") and val[-1:] == val[:1] and len(val) > 1:
            val = val[1:-1]
        if val.startswith("[") and val.endswith("]"):
            val = [v.strip().strip('"').strip("'")
                   for v in val[1:-1].split(",") if v.strip()]
        elif val.lower() == "true":
            val = True
        elif val.lower() == "false":
            val = False
        meta[key] = val
    return meta, text[m.end():].strip()


def _as_globs(value):
    if not value:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [v.strip() for v in str(value).split(",") if v.strip()]


def _glob_matches(pattern, rel_path, basename):
    """宽松匹配 glob：支持 **/*.py、src/**、*.py 三种常见写法。"""
    p = pattern.replace("\\", "/").lstrip("./")
    rel = rel_path.replace("\\", "/").lstrip("./")
    try:
        if PurePosixPath(rel).match(p):
            return True
    except (ValueError, TypeError):
        pass
    if fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(basename, p):
        return True
    # **/xxx 也应匹配根目录下的 xxx
    if p.startswith("**/") and fnmatch.fnmatch(basename, p[3:]):
        return True
    return False


def _applies(meta, project_dir, current_file):
    """判断一条条件规则当前是否生效。"""
    globs = _as_globs(meta.get("globs") or meta.get("applyto"))
    if not globs or meta.get("alwaysapply") is True:
        return True
    if not current_file:
        return False
    try:
        rel = os.path.relpath(os.path.abspath(current_file),
                              os.path.abspath(project_dir))
    except (ValueError, TypeError):
        rel = os.path.basename(str(current_file))
    basename = os.path.basename(str(current_file))
    return any(_glob_matches(g, rel, basename) for g in globs)


def _read_rule_file(path, scope, project_dir, current_file, seen):
    norm = os.path.normcase(os.path.abspath(path))
    if norm in seen or not os.path.isfile(path):
        return None
    seen.add(norm)
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.debug("无法读取规则文件 %s: %s", path, exc)
        return None
    meta, body = _parse_frontmatter(text)
    if not body.strip():
        return None
    # 平铺规则文件也允许写 globs，方便按文件条件生效
    if not _applies(meta, project_dir, current_file):
        return None
    return {
        "source": scope,
        "path": path,
        "name": os.path.basename(path),
        "description": str(meta.get("description") or ""),
        "globs": _as_globs(meta.get("globs") or meta.get("applyto")),
        "content": body.strip()[:MAX_RULE_CHARS],
    }


def collect_rules(project_dir=None, current_file=None, include_user=True):
    """收集当前生效的全部规则。

    Returns
    -------
    list[dict] — 每项含 source / path / name / description / globs / content
    """
    root = os.path.abspath(project_dir or os.getcwd())
    seen = set()
    rules = []

    # 1) 项目根 + 最多向上 2 层的平铺规则文件
    dirs = [root]
    cur = root
    for _ in range(2):
        parent = os.path.dirname(cur)
        if not parent or parent == cur:
            break
        cur = parent
        dirs.append(cur)
    for d in dirs:
        for rel in RULE_FILENAMES:
            p = os.path.join(d, rel)
            if os.path.isfile(p):
                item = _read_rule_file(
                    p, "项目规则" if d == root else "上级规则",
                    root, current_file, seen)
                if item:
                    rules.append(item)

    # 2) 条件规则目录（globs / applyTo）
    for rel in RULE_DIRS:
        d = os.path.join(root, rel)
        if not os.path.isdir(d):
            continue
        try:
            files = sorted(Path(d).glob("*.md"))
        except OSError:
            continue
        for f in files:
            item = _read_rule_file(str(f), "条件规则", root,
                                   current_file, seen)
            if item:
                rules.append(item)

    # 3) 用户级规则（始终生效）
    if include_user and os.path.isdir(USER_RULE_DIR):
        for f in sorted(Path(USER_RULE_DIR).glob("*.md")):
            item = _read_rule_file(str(f), "用户规则", root,
                                   current_file, seen)
            if item:
                rules.append(item)

    return rules


def list_rules(project_dir=None, current_file=None):
    """给模型看的规则清单（不含正文），供 list_project_rules 工具使用。"""
    rules = collect_rules(project_dir, current_file)
    if not rules:
        return "当前项目没有检测到规则文件。"
    lines = [f"检测到 {len(rules)} 条生效规则："]
    for r in rules:
        scope = r["source"]
        extra = f" · 适用 {', '.join(r['globs'])}" if r["globs"] else ""
        desc = f" — {r['description']}" if r["description"] else ""
        lines.append(f"- [{scope}] {r['name']} ({r['path']}){extra}{desc}")
    return "\n".join(lines)


def format_rules_prompt(project_dir=None, current_file=None, budget=12000,
                        enabled=True):
    """生成注入系统提示词的规则块；带字符预算，超出时截断并给出提示。"""
    if not enabled:
        return ""
    rules = collect_rules(project_dir, current_file)
    if not rules:
        return ""
    try:
        budget = max(1000, int(budget))
    except (TypeError, ValueError):
        budget = 12000

    lines = [
        "## 项目规则（自动加载，优先级高于通用习惯）",
        "以下规则来自项目 / 用户的规则文件，必须遵守；"
        "若与用户当次指令冲突，以用户当次指令为准。",
    ]
    used = 0
    included = 0
    for r in rules:
        head = f"\n### [{r['source']}] {r['name']}"
        if r["globs"]:
            head += f"（适用: {', '.join(r['globs'])}）"
        body = "\n" + r["content"]
        if used + len(head) + len(body) > budget:
            remain = max(0, budget - used - len(head))
            if remain > 400:
                lines.append(head + body[:remain] + "\n…（规则内容因预算截断）")
                included += 1
            break
        lines.append(head + body)
        used += len(head) + len(body)
        included += 1
    if included < len(rules):
        lines.append(
            f"\n（还有 {len(rules) - included} 条规则未注入，"
            "可用 list_project_rules 工具查看来源清单）")
    return "\n".join(lines)
