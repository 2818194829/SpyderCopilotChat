# -*- coding: utf-8 -*-
"""
Prompts & Skills 加载器

从 prompts/ 目录加载提示词模板和技能定义，
对标 GitHub Copilot Chat 的 assets/prompts/ 系统。

支持:
  - 加载 .prompt.md 提示词模板
  - 加载 skills/*/SKILL.md 技能定义
  - 解析 YAML frontmatter 元数据
  - 变量替换 ({{VARIABLE_NAME}})
"""

import logging
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# prompts 目录相对于本模块的位置
_PROMPTS_DIR = Path(__file__).parent

# YAML frontmatter 解析正则
_FRONTMATTER_RE = re.compile(r'^---\s*\n(.*?)\n---\s*\n', re.DOTALL)

# 简单的 YAML key: value 解析（不依赖 pyyaml）
_YAML_KV_RE = re.compile(r'^(\w[\w-]*)\s*:\s*(.*?)$')


def _parse_frontmatter(text):
    """解析 YAML frontmatter，返回 (metadata_dict, body_text)"""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    raw = m.group(1)
    meta = {}
    for line in raw.strip().split('\n'):
        kv = _YAML_KV_RE.match(line)
        if kv:
            key = kv.group(1).strip()
            val = kv.group(2).strip()
            # 去除引号
            if val.startswith('"') and val.endswith('"'):
                val = val[1:-1]
            elif val.startswith("'") and val.endswith("'"):
                val = val[1:-1]
            # 布尔值
            if val.lower() == 'true':
                val = True
            elif val.lower() == 'false':
                val = False
            meta[key] = val
    body = text[m.end():].strip()
    return meta, body


# ══════════════════════════════════════════════════════════════════════
# Prompt 模板加载
# ══════════════════════════════════════════════════════════════════════

def list_prompt_templates():
    """列出所有可用的 prompt 模板"""
    templates = []
    for f in _PROMPTS_DIR.glob("*.prompt.md"):
        content = f.read_text(encoding="utf-8")
        meta, body = _parse_frontmatter(content)
        templates.append({
            "file": f.name,
            "name": meta.get("name", f.stem.replace(".prompt", "")),
            "description": meta.get("description", ""),
            "agent": meta.get("agent", ""),
            "argument_hint": meta.get("argument-hint", ""),
            "body": body,
        })
    return templates


def get_prompt_template(name):
    """按名称获取 prompt 模板"""
    for t in list_prompt_templates():
        if t["name"] == name:
            return t
    return None


def render_prompt(name, variables=None):
    """获取并渲染 prompt 模板，替换 {{VAR}} 变量"""
    t = get_prompt_template(name)
    if not t:
        return None
    body = t["body"]
    if variables:
        for k, v in variables.items():
            body = body.replace("{{" + k + "}}", str(v))
    return body


# ══════════════════════════════════════════════════════════════════════
# Skills 加载
# ══════════════════════════════════════════════════════════════════════

def list_skills():
    """列出所有可用的技能"""
    skills = []
    skills_dir = _PROMPTS_DIR / "skills"
    if not skills_dir.exists():
        return skills

    for skill_dir in sorted(skills_dir.iterdir()):
        if not skill_dir.is_dir():
            continue
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.exists():
            continue

        content = skill_file.read_text(encoding="utf-8")
        meta, body = _parse_frontmatter(content)

        skills.append({
            "name": meta.get("name", skill_dir.name),
            "description": meta.get("description", ""),
            "argument_hint": meta.get("argument-hint", ""),
            "user_invocable": meta.get("user-invocable", True),
            "body": body,
            "path": str(skill_dir),
        })

    return skills


def get_skill(name):
    """按名称获取技能"""
    for s in list_skills():
        if s["name"] == name:
            return s
    return None


def render_skill(name, variables=None):
    """获取并渲染技能内容"""
    s = get_skill(name)
    if not s:
        return None
    body = s["body"]
    if variables:
        for k, v in variables.items():
            body = body.replace("{{" + k + "}}", str(v))
    return body


def get_skills_system_prompt():
    """生成所有技能的摘要，用于注入到系统提示词中"""
    skills = list_skills()
    if not skills:
        return ""

    lines = ["## Available Skills\n"]
    for s in skills:
        desc = s["description"][:120]
        lines.append(f"- **{s['name']}**: {desc}")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════
# 快捷访问
# ══════════════════════════════════════════════════════════════════════

def get_plan_prompt(variables=None):
    """获取 Plan 模式提示词"""
    return render_prompt("plan", variables)


def get_chronicle_standup_prompt():
    """获取 Chronicle Standup 提示词"""
    return render_prompt("chronicle:standup")


def get_chronicle_tips_prompt():
    """获取 Chronicle Tips 提示词"""
    return render_prompt("chronicle:tips")
