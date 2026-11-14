# -*- coding: utf-8 -*-
"""
自定义指令管理器

对标 GitHub Copilot Chat 的 Custom Instructions 系统:
  - 项目级: .github/copilot-instructions.md 或 AGENTS.md
  - 文件级: .instructions.md (通过 applyTo 匹配特定文件)
  - 用户级: ~/.spyder_copilot_chat/instructions.md

指令文件格式:
  ---
  name: my-instructions
  description: Python coding conventions
  applyTo: ["**/*.py"]
  ---
  Always use type hints for function signatures.
  Use Google-style docstrings.
"""

import fnmatch
import logging
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)

CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".spyder_copilot_chat")

_FRONTMATTER_RE = re.compile(r'^---\s*\n(.*?)\n---\s*\n', re.DOTALL)
_YAML_KV_RE = re.compile(r'^(\w[\w-]*)\s*:\s*(.*?)$')


def _parse_frontmatter(text):
    """解析 YAML frontmatter"""
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
            if val.startswith('"') and val.endswith('"'):
                val = val[1:-1]
            elif val.startswith("'") and val.endswith("'"):
                val = val[1:-1]
            # 列表解析 (粗略)
            if val.startswith('[') and val.endswith(']'):
                val = [v.strip().strip('"').strip("'")
                       for v in val[1:-1].split(',') if v.strip()]
            if val == 'true':
                val = True
            elif val == 'false':
                val = False
            meta[key] = val
    body = text[m.end():].strip()
    return meta, body


def _find_file_upward(start_dir, filename):
    """向上搜索直到找到文件或到达根目录"""
    cur = Path(start_dir).resolve()
    while True:
        candidate = cur / filename
        if candidate.exists():
            return str(candidate)
        parent = cur.parent
        if parent == cur:
            break
        cur = parent
    return None


def load_project_instructions(project_dir=None):
    """加载项目级自定义指令

    搜索顺序:
      1. <project>/.github/copilot-instructions.md
      2. <project>/AGENTS.md
      3. <project>/copilot-instructions.md

    Returns: str (合并后的指令文本) 或 ""
    """
    if not project_dir:
        project_dir = os.getcwd()

    candidates = [
        os.path.join(project_dir, ".github", "copilot-instructions.md"),
        os.path.join(project_dir, "AGENTS.md"),
        os.path.join(project_dir, "copilot-instructions.md"),
    ]

    instructions = []
    for path in candidates:
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()
                meta, body = _parse_frontmatter(content)
                if body.strip():
                    instructions.append(body.strip())
                logger.debug("Loaded project instructions from %s", path)
            except Exception:
                logger.warning("Failed to read %s", path)

    return "\n\n".join(instructions)


def load_user_instructions():
    """加载用户级自定义指令

    搜索: ~/.spyder_copilot_chat/instructions.md

    Returns: str 或 ""
    """
    path = os.path.join(CONFIG_DIR, "instructions.md")
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            meta, body = _parse_frontmatter(content)
            return body.strip() or ""
        except Exception:
            logger.warning("Failed to read %s", path)
    return ""


def load_file_instructions(file_path, project_dir=None):
    """加载适用于特定文件的指令

    搜索当前文件所在目录及父目录中的 .instructions.md 文件，
    通过 applyTo 模式匹配判断是否适用。

    Parameters
    ----------
    file_path : str
        当前编辑的文件路径
    project_dir : str or None
        项目根目录

    Returns: list of str (匹配的指令文本列表)
    """
    if not file_path or not os.path.isfile(file_path):
        return []

    if not project_dir:
        project_dir = os.getcwd()

    file_abs = os.path.abspath(file_path)
    matched = []

    # 从文件所在目录向上搜索 .instructions.md
    cur_dir = os.path.dirname(file_abs)
    project_abs = os.path.abspath(project_dir)

    while cur_dir.startswith(project_abs):
        inst_file = os.path.join(cur_dir, ".instructions.md")
        if os.path.isfile(inst_file):
            try:
                with open(inst_file, "r", encoding="utf-8") as f:
                    content = f.read()
                meta, body = _parse_frontmatter(content)
                if not body.strip():
                    continue

                apply_to = meta.get("applyTo", None)
                if apply_to is None:
                    # 无 applyTo 限制，始终适用
                    matched.append(body.strip())
                elif isinstance(apply_to, list):
                    rel_path = os.path.relpath(file_abs, cur_dir)
                    for pattern in apply_to:
                        if fnmatch.fnmatch(rel_path, pattern) or \
                           fnmatch.fnmatch(os.path.basename(file_abs), pattern):
                            matched.append(body.strip())
                            break
            except Exception:
                logger.warning("Failed to read %s", inst_file)

        parent = os.path.dirname(cur_dir)
        if parent == cur_dir:
            break
        cur_dir = parent

    return matched


def get_effective_instructions(file_path=None, project_dir=None):
    """获取所有生效的自定义指令

    合并顺序:
      1. 项目级指令 (copilot-instructions.md / AGENTS.md)
      2. 用户级指令 (~/.spyder_copilot_chat/instructions.md)
      3. 文件级指令 (.instructions.md 匹配的)

    Returns: str
    """
    parts = []

    proj = load_project_instructions(project_dir)
    if proj:
        parts.append(proj)

    user = load_user_instructions()
    if user:
        parts.append(user)

    if file_path:
        file_insts = load_file_instructions(file_path, project_dir)
        parts.extend(file_insts)

    return "\n\n".join(parts) if parts else ""


def save_user_instructions(content):
    """保存用户级指令"""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    path = os.path.join(CONFIG_DIR, "instructions.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path
