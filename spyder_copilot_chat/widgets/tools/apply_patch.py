# -*- coding: utf-8 -*-
"""
补丁应用 (applyPatch) 工具

对标 GitHub Copilot Chat 的 copilot.tools.applyPatch：
精确地应用补丁到工作区中的文本文件，支持添加、修改、删除操作。

补丁格式（统一 diff 格式）:
--- a/file.py
+++ b/file.py
@@ -1,5 +1,7 @@
 原有内容
+新增行
-删除行
 上下文行
"""

import logging
import os
import re

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════
# 补丁应用核心
# ══════════════════════════════════════════════════════════════════════

def apply_patch(patch_text, base_dir=None):
    """应用补丁到文件

    对标 VS Code 的 applyPatch 工具

    Parameters
    ----------
    patch_text : str
        统一 diff 格式的补丁文本
    base_dir : str or None
        基础目录

    Returns
    -------
    str : 应用结果描述
    """
    if not patch_text or not patch_text.strip():
        return "错误: 补丁内容为空"

    # 解析补丁
    patches = _parse_patch(patch_text)
    if not patches:
        return "错误: 无法解析补丁格式"

    results = []
    for file_patch in patches:
        filepath = file_patch["filepath"]
        hunks = file_patch["hunks"]

        # 解析文件路径
        if not filepath or filepath == "/dev/null":
            results.append(f"⚠️ 跳过: 无效的文件路径 '{filepath}'")
            continue

        # 构建绝对路径
        abs_path = filepath
        if not os.path.isabs(abs_path):
            if base_dir:
                abs_path = os.path.join(base_dir, filepath)
            else:
                abs_path = os.path.abspath(filepath)

        # 检查文件是否存在
        file_exists = os.path.isfile(abs_path)

        if not file_exists and not _is_new_file_patch(file_patch):
            results.append(f"⚠️ 文件不存在 (跳过): {filepath}")
            continue

        # 应用补丁
        result = _apply_hunks(abs_path, filepath, hunks, file_exists)
        results.append(result)

    return "\n".join(results)


def _parse_patch(patch_text):
    """解析统一 diff 格式的补丁

    Returns
    -------
    list[dict]: [{"filepath": str, "hunks": [{"old_start", "old_count",
                  "new_start", "new_count", "lines": [(op, text)]}]}]
    """
    patches = []
    current_file = None
    current_hunks = []
    current_hunk = None

    for line in patch_text.split("\n"):
        # 文件头部: --- a/file.py 或 +++ b/file.py
        if line.startswith("--- "):
            if current_file and current_hunks:
                patches.append({"filepath": current_file, "hunks": current_hunks})
                current_hunks = []
            current_file = line[4:].strip()
            # 去掉 a/ 前缀
            if current_file.startswith("a/") or current_file.startswith("b/"):
                current_file = current_file[2:]
            continue

        if line.startswith("+++ "):
            fp = line[4:].strip()
            if fp.startswith("a/") or fp.startswith("b/"):
                fp = fp[2:]
            if fp != "/dev/null":
                current_file = fp
            continue

        # Hunk 头部: @@ -old_start,old_count +new_start,new_count @@
        hunk_match = re.match(
            r'^@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@', line
        )
        if hunk_match:
            # 保存上一个 hunk
            if current_hunk:
                current_hunks.append(current_hunk)

            old_start = int(hunk_match.group(1))
            old_count = int(hunk_match.group(2)) if hunk_match.group(2) else 1
            new_start = int(hunk_match.group(3))
            new_count = int(hunk_match.group(4)) if hunk_match.group(4) else 1

            current_hunk = {
                "old_start": old_start,
                "old_count": old_count,
                "new_start": new_start,
                "new_count": new_count,
                "lines": [],
            }
            continue

        if current_hunk is not None:
            if line.startswith("+"):
                current_hunk["lines"].append(("+", line[1:]))
            elif line.startswith("-"):
                current_hunk["lines"].append(("-", line[1:]))
            elif line.startswith(" "):
                current_hunk["lines"].append((" ", line[1:]))
            elif line.startswith("\\"):  # 无换行符指示
                continue
            else:
                # 空行或未知内容
                current_hunk["lines"].append((" ", ""))

    # 保存最后一个
    if current_hunk:
        current_hunks.append(current_hunk)
    if current_file and current_hunks:
        patches.append({"filepath": current_file, "hunks": current_hunks})

    return patches


def _is_new_file_patch(file_patch):
    """检查是否为新建文件的补丁"""
    # 新文件通常 first line 是 + 开头的，没有 - 行
    all_add = all(
        op == "+" or op == " "
        for hunk in file_patch["hunks"]
        for op, _ in hunk["lines"]
    )
    return all_add


def _apply_hunks(abs_path, rel_path, hunks, file_exists):
    """应用 hunk 到文件

    使用三路合并策略：
    1. 尝试精确匹配（上下文行必须完全匹配）
    2. 如果精确匹配失败，尝试模糊匹配（忽略缩进差异）
    """
    if not file_exists:
        # 新文件：直接创建
        try:
            lines = []
            for hunk in hunks:
                for op, text in hunk["lines"]:
                    if op == "+" or op == " ":
                        lines.append(text)
            content = "\n".join(lines)
            if content and not content.endswith("\n"):
                content += "\n"
            os.makedirs(os.path.dirname(abs_path), exist_ok=True)
            with open(abs_path, "w", encoding="utf-8") as f:
                f.write(content)
            return f"✅ 文件已创建: {rel_path} ({len(lines)} 行)"
        except Exception as e:
            return f"❌ 创建文件失败 {rel_path}: {e}"

    # 读取现有文件
    try:
        with open(abs_path, "r", encoding="utf-8") as f:
            file_lines = f.readlines()
        # 去掉行尾换行符
        file_lines = [l.rstrip("\n").rstrip("\r") for l in file_lines]
    except Exception as e:
        return f"❌ 读取文件失败 {rel_path}: {e}"

    try:
        total_added = 0
        total_removed = 0
        offset = 0  # 已应用 hunk 造成的行偏移

        for hunk in hunks:
            old_start = hunk["old_start"] - 1  # 转为 0-based
            lines = hunk["lines"]

            # 计算上下文中删除和保留的行数
            context_before, changes, context_after = _split_hunk_lines(lines)

            # 尝试匹配上下文
            match_pos = _find_hunk_position(
                file_lines, old_start + offset, context_before, changes, context_after
            )

            if match_pos is None:
                # 尝试模糊匹配
                match_pos = _find_hunk_position_fuzzy(
                    file_lines, old_start + offset, context_before, changes, context_after
                )

            if match_pos is None:
                return (f"⚠️ 补丁匹配失败 {rel_path} (hunk 起始行 {old_start + 1}):\n"
                        f"  文件内容已与补丁不匹配，跳过此 hunk")

            # 应用补丁
            new_lines = []
            removed = 0

            # 上下文之前
            new_lines.extend(context_before)

            # 替换行
            for line in changes:
                if line[0] == "-":
                    removed += 1
                elif line[0] == "+":
                    new_lines.append(line[1])

            # 上下文之后
            new_lines.extend(context_after)

            # 替换文件中的对应区域
            old_len = len(context_before) + removed + len(context_after)
            file_lines[match_pos:match_pos + old_len] = new_lines

            total_added += sum(1 for op, _ in changes if op == "+")
            total_removed += removed
            offset += len(new_lines) - old_len

        # 写入文件
        content = "\n".join(file_lines)
        if content and not content.endswith("\n"):
            content += "\n"
        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(content)

        return (f"✅ 补丁已应用到: {rel_path}\n"
                f"  新增 {total_added} 行，删除 {total_removed} 行\n"
                f"  当前文件共 {len(file_lines)} 行")

    except Exception as e:
        logger.warning("应用补丁失败: %s", e)
        return f"❌ 应用补丁失败 {rel_path}: {e}"


def _split_hunk_lines(lines):
    """将 hunk 行分为：上下文之前、变更、上下文之后"""
    context_before = []
    changes = []
    context_after = []
    seen_change = False
    seen_context_after = False

    for op, text in lines:
        if op == " ":
            if not seen_change:
                context_before.append(text)
            else:
                seen_context_after = True
                context_after.append(text)
        else:
            seen_change = True
            if not seen_context_after:
                changes.append((op, text))
            else:
                # 上下文之后出现变更（不合理但容错）
                changes.append((op, text))

    return context_before, changes, context_after


def _find_hunk_position(file_lines, start_pos, context_before, changes, context_after):
    """查找 hunk 在文件中的精确匹配位置"""
    hunk_context = context_before + [t for op, t in changes if op == "-"] + context_after
    if not hunk_context:
        return start_pos

    # 在 start_pos 附近搜索
    search_start = max(0, start_pos - 5)
    search_end = min(len(file_lines), start_pos + 5)

    for pos in range(search_start, search_end):
        if _match_at_position(file_lines, pos, context_before, changes, context_after):
            return pos

    return None


def _find_hunk_position_fuzzy(file_lines, start_pos, context_before, changes, context_after):
    """模糊匹配 — 忽略缩进差异"""
    hunk_context = context_before + [t for op, t in changes if op == "-"] + context_after
    if not hunk_context:
        return start_pos

    search_start = max(0, start_pos - 20)
    search_end = min(len(file_lines), start_pos + 20)

    best_pos = None
    best_score = 0

    for pos in range(search_start, search_end):
        score = _fuzzy_match_at_position(
            file_lines, pos, context_before, changes, context_after
        )
        if score > best_score:
            best_score = score
            best_pos = pos

    if best_score > 0.5:  # 50% 以上的匹配率
        return best_pos
    return None


def _match_at_position(file_lines, pos, context_before, changes, context_after):
    """检查在 pos 位置是否精确匹配 hunk"""
    total_len = len(context_before) + sum(1 for op, t in changes if op == "-") + len(context_after)
    if pos + total_len > len(file_lines):
        return False

    idx = 0
    # 上下文之前
    for text in context_before:
        if file_lines[pos + idx] != text:
            return False
        idx += 1
    # 删除的行
    for op, text in changes:
        if op == "-":
            if file_lines[pos + idx] != text:
                return False
            idx += 1
    # 上下文之后
    for text in context_after:
        if file_lines[pos + idx] != text:
            return False
        idx += 1

    return True


def _fuzzy_match_at_position(file_lines, pos, context_before, changes, context_after):
    """模糊匹配 — 返回匹配分数 (0~1)"""
    total_len = len(context_before) + sum(1 for op, t in changes if op == "-") + len(context_after)
    if pos + total_len > len(file_lines):
        return 0

    matched = 0
    total = 0
    idx = 0

    def _lines_match(a, b):
        return a.strip() == b.strip()

    for text in context_before:
        total += 1
        if idx + pos < len(file_lines) and _lines_match(file_lines[pos + idx], text):
            matched += 1
        idx += 1

    for op, text in changes:
        if op == "-":
            total += 1
            if idx + pos < len(file_lines) and _lines_match(file_lines[pos + idx], text):
                matched += 1
            idx += 1

    for text in context_after:
        total += 1
        if idx + pos < len(file_lines) and _lines_match(file_lines[pos + idx], text):
            matched += 1
        idx += 1

    return matched / total if total > 0 else 0


# ══════════════════════════════════════════════════════════════════════
# 简化编辑工具（替代 applyPatch 的轻量级方案）
# ══════════════════════════════════════════════════════════════════════

def insert_edit(filepath, old_string, new_string, base_dir=None):
    """精确的字符串替换编辑

    对标 VS Code 的 insertEdit / replaceString 工具

    Parameters
    ----------
    filepath : str
        文件路径
    old_string : str
        要被替换的精确字符串（必须唯一）
    new_string : str
        替换后的新字符串
    base_dir : str or None
        基础目录

    Returns
    -------
    str : 执行结果
    """
    path = filepath
    if not os.path.isabs(path):
        path = os.path.join(base_dir or os.getcwd(), path)

    if not os.path.isfile(path):
        return f"错误: 文件不存在 {path}"

    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        return f"错误: 读取文件失败 {e}"

    # 检查 old_string 出现次数
    count = content.count(old_string)
    if count == 0:
        return (f"错误: 在文件中未找到匹配的文本\n"
                f"文件: {path}\n"
                f"提示: old_string 必须与文件中的内容完全一致")
    elif count > 1:
        return (f"错误: 找到 {count} 处匹配，无法确定替换位置\n"
                f"请提供更多上下文以确保唯一性")

    try:
        new_content = content.replace(old_string, new_string)
        with open(path, "w", encoding="utf-8") as f:
            f.write(new_content)

        old_lines = old_string.count("\n") + 1
        new_lines = new_string.count("\n") + 1
        return (f"✅ 编辑成功: {os.path.basename(path)}\n"
                f"  替换 {old_lines} 行 → {new_lines} 行")
    except Exception as e:
        return f"错误: 写入文件失败 {e}"
