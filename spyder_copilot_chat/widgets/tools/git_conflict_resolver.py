# -*- coding: utf-8 -*-
"""
Git 合并冲突解决工具

对标 GitHub Copilot Chat 的 Conflict Resolution 功能：
  - 检测冲突标记（<<<<<<<, =======, >>>>>>>）
  - 用 AI 辅助解决冲突
  - 应用解决方案
"""

import logging
import os
import re

logger = logging.getLogger(__name__)

# 冲突标记正则
CONFLICT_PATTERN = re.compile(
    r'<<<<<<< .+?\n(.*?)=======\n(.*?)>>>>>>> .+?(?:\n|$)',
    re.DOTALL
)

# 可扫描的文件扩展名
SCANNABLE_EXTENSIONS = {
    '.py', '.js', '.ts', '.jsx', '.tsx', '.html', '.css', '.scss',
    '.json', '.md', '.yaml', '.yml', '.toml', '.txt', '.cpp', '.c',
    '.h', '.hpp', '.java', '.rs', '.go', '.rb', '.php', '.swift',
    '.kt', '.scala', '.vue', '.svelte', '.xml', '.sql',
}


def detect_conflicts(filepath):
    """检测文件中的未解决的合并冲突标记

    Returns: list of conflict blocks, or None 如果没有冲突
    Each block: {ours, theirs, start_line, end_line, full_match}
    """
    if not os.path.isfile(filepath):
        return None

    try:
        with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()
    except Exception as e:
        logger.debug("Cannot read %s: %s", filepath, e)
        return None

    if '<<<<<<<' not in content or '>>>>>>>' not in content:
        return None

    conflicts = []
    for match in CONFLICT_PATTERN.finditer(content):
        ours = match.group(1).strip()
        theirs = match.group(2).strip()

        preceding = content[:match.start()]
        start_line = preceding.count('\n') + 1
        end_line = start_line + match.group(0).count('\n')

        conflicts.append({
            "ours": ours,
            "theirs": theirs,
            "start_line": start_line,
            "end_line": end_line,
            "full_match": match.group(0),
        })

    return conflicts if conflicts else None


def apply_resolution(filepath, start_line, end_line, resolved_content):
    """应用冲突解决方案到文件

    用解决后的内容替换文件中的冲突块。
    Returns: str status message
    """
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            lines = f.readlines()

        # 删除冲突行（1-based 转 0-based）
        del lines[start_line - 1:end_line]

        # 插入解决后的内容
        resolved_lines = resolved_content.split('\n')
        for i, line in enumerate(resolved_lines):
            lines.insert(start_line - 1 + i, line + '\n')

        with open(filepath, 'w', encoding='utf-8') as f:
            f.writelines(lines)

        return f"✅ 冲突已解决: {os.path.basename(filepath)} (行 {start_line}-{end_line})"
    except Exception as e:
        return f"❌ 冲突解决失败: {e}"


def get_conflicted_files(base_dir=None):
    """获取有冲突的文件列表

    先尝试 git status，再手动扫描。
    Returns: list of file paths (absolute)
    """
    if not base_dir:
        base_dir = os.getcwd()

    # 先试 git
    try:
        import subprocess
        result = subprocess.run(
            ["git", "diff", "--name-only", "--diff-filter=U"],
            cwd=base_dir, capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0 and result.stdout.strip():
            files = []
            for f in result.stdout.strip().split('\n'):
                full_path = os.path.join(base_dir, f.strip())
                if os.path.isfile(full_path):
                    files.append(full_path)
            if files:
                return files
    except Exception:
        pass

    # 手动扫描
    return _scan_for_conflicts(base_dir)


def _scan_for_conflicts(base_dir):
    """手动扫描目录中的冲突标记"""
    conflicted = []

    for root, dirs, files in os.walk(base_dir):
        # 跳过常见排除目录
        dirs[:] = [d for d in dirs
                   if not d.startswith('.')
                   and d not in ('node_modules', '__pycache__', 'venv',
                                 '.venv', 'env', 'dist', 'build',
                                 '.git', '.svn')]
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            if ext not in SCANNABLE_EXTENSIONS:
                continue
            filepath = os.path.join(root, f)
            try:
                # 只读前 8KB 检查标记
                with open(filepath, 'r', encoding='utf-8', errors='replace') as fh:
                    head = fh.read(8192)
                if '<<<<<<<' in head:
                    # 完整读取确认
                    fh.seek(0)
                    content = fh.read()
                    if '<<<<<<<' in content and '>>>>>>>' in content:
                        conflicted.append(filepath)
            except Exception:
                continue

    return conflicted


def format_conflict_report(conflicts, filepath):
    """格式化冲突报告"""
    if not conflicts:
        return f"✅ 无冲突: {os.path.basename(filepath)}"

    lines = [f"⚠️ 发现 {len(conflicts)} 个冲突: {os.path.basename(filepath)}\n"]
    for i, c in enumerate(conflicts, 1):
        ours_preview = c['ours'][:100].replace('\n', '\\n')
        theirs_preview = c['theirs'][:100].replace('\n', '\\n')
        lines.append(f"冲突 #{i} (行 {c['start_line']}-{c['end_line']}):")
        lines.append(f"  <<<<<<< 我们的: {ours_preview}")
        lines.append(f"  =========")
        lines.append(f"  >>>>>>> 他们的: {theirs_preview}")
        lines.append("")

    return "\n".join(lines)
