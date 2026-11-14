# -*- coding: utf-8 -*-
"""
渐进式 Skills — v0.5.6 / P1

对标 Claude Code / Cline 的 Skills 设计：**只有一行描述常驻上下文**，
模型判断命中后再调用 load_skill 工具加载完整正文，避免几十个方法包
把系统提示词撑爆。

三层来源（后者覆盖同名前者）：
  1. 内置   <pkg>/prompts/skills/*/SKILL.md
  2. 用户级 ~/.spyder_copilot_chat/skills/*/SKILL.md 或 *.md
  3. 项目级 <root>/.spydercopilot/skills/ 或 <root>/.claude/skills/

SKILL.md 格式：
  ---
  name: my-skill
  description: 一句话说明「什么时候该用它」
  ---
  # 正文（命中后才加载）

本模块是纯 Python，可在后台线程安全调用。
"""

import logging
import os
from pathlib import Path

from ..prompts import _parse_frontmatter

logger = logging.getLogger(__name__)

CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".spyder_copilot_chat")
USER_SKILL_DIR = os.path.join(CONFIG_DIR, "skills")
PROJECT_SKILL_DIRS = (
    os.path.join(".spydercopilot", "skills"),
    os.path.join(".claude", "skills"),
)

MAX_SKILL_CHARS = 32000


def _read_skill(path, source):
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.debug("无法读取 Skill %s: %s", path, exc)
        return None
    meta, body = _parse_frontmatter(text)
    if not body.strip():
        return None
    name = str(meta.get("name") or Path(path).parent.name
               or Path(path).stem).strip()
    return {
        "name": name,
        "description": str(meta.get("description") or "").strip(),
        "source": source,
        "path": str(path),
        "body": body.strip()[:MAX_SKILL_CHARS],
    }


def _builtin_skills():
    try:
        from ..prompts import list_skills
        return [
            {"name": s["name"], "description": s.get("description", ""),
             "source": "内置", "path": s.get("path", ""),
             "body": s.get("body", "")}
            for s in list_skills()
        ]
    except Exception as exc:
        logger.debug("加载内置 Skills 失败: %s", exc)
        return []


def _dir_skills(directory, source):
    out = []
    if not os.path.isdir(directory):
        return out
    try:
        # 目录式 <dir>/<name>/SKILL.md
        for sub in sorted(Path(directory).iterdir()):
            if sub.is_dir():
                f = sub / "SKILL.md"
                if f.is_file():
                    item = _read_skill(str(f), source)
                    if item:
                        out.append(item)
        # 平铺式 <dir>/<name>.md
        for f in sorted(Path(directory).glob("*.md")):
            item = _read_skill(str(f), source)
            if item:
                out.append(item)
    except OSError as exc:
        logger.debug("扫描 Skills 目录失败 %s: %s", directory, exc)
    return out


def list_skills(project_dir=None):
    """返回全部可用 Skill（含正文，但只有 catalog 会注入系统提示词）。"""
    root = os.path.abspath(project_dir or os.getcwd())
    merged = {}

    def _add(items):
        for s in items:
            if s.get("name"):
                merged[s["name"]] = s   # 项目级 > 用户级 > 内置

    _add(_builtin_skills())
    _add(_dir_skills(USER_SKILL_DIR, "用户级"))
    for rel in PROJECT_SKILL_DIRS:
        _add(_dir_skills(os.path.join(root, rel), "项目级"))
    return [merged[k] for k in sorted(merged)]


def get_skill(name, project_dir=None):
    if not name:
        return None
    for s in list_skills(project_dir):
        if s["name"] == name or s["name"].lower() == str(name).strip().lower():
            return s
    return None


def skills_catalog_prompt(project_dir=None, enabled=True):
    """只注入一行 / Skill 的目录，正文按需加载。"""
    if not enabled:
        return ""
    skills = list_skills(project_dir)
    if not skills:
        return ""
    lines = [
        "## Skills（渐进式加载）",
        "下面是可用技能的一句话说明。**只有在任务匹配时才调用 "
        "`load_skill` 工具加载完整方法包**，不要凭描述猜测其内部步骤；"
        "不确定时用 `list_skills` 查看全部技能。",
    ]
    for s in skills:
        desc = (s["description"] or "（无说明）")[:160]
        lines.append(f"- **{s['name']}** [{s['source']}]: {desc}")
    return "\n".join(lines)


def catalog_text(project_dir=None):
    """给 list_skills 工具使用的人类可读清单。"""
    skills = list_skills(project_dir)
    if not skills:
        return "当前没有可用 Skill。可在 ~/.spyder_copilot_chat/skills/ "
        "或 <项目>/.spydercopilot/skills/ 下添加 SKILL.md。"
    lines = [f"可用 Skills（{len(skills)} 个）："]
    for s in skills:
        lines.append(
            f"- {s['name']} [{s['source']}] — {s['description'] or '（无说明）'}"
            f"\n  文件: {s['path']}")
    return "\n".join(lines)


def load_skill_text(name, project_dir=None):
    """给 load_skill 工具使用：命中后返回完整正文。"""
    s = get_skill(name, project_dir)
    if not s:
        names = ", ".join(x["name"] for x in list_skills(project_dir)) or "（无）"
        return f"未找到 Skill `{name}`。可用: {names}"
    return (f"# Skill: {s['name']} [{s['source']}]\n"
            f"来源: {s['path']}\n\n{s['body']}")
