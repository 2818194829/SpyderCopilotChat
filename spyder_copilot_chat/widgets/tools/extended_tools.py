# -*- coding: utf-8 -*-
"""
扩展工具集 — 补全 vs GitHub Copilot Chat 的全部功能

新增:
  - multi_replace_string: 批量字符串替换
  - read_project_structure: 读取项目结构树
  - find_test_files: 查找测试文件
  - search_workspace_symbols: 工作区符号搜索 (Python LSP)
  - view_image: 查看图像文件
  - notebook_run_cell: 运行 Notebook 单元格
  - notebook_get_output: 获取 Notebook 单元格输出
"""

import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

_WINDOWS = sys.platform == "win32"
_SUBPROCESS_FLAGS = subprocess.CREATE_NO_WINDOW if _WINDOWS else 0

EXCLUDE_DIRS = {
    ".git", "__pycache__", ".venv", "venv", "env", "node_modules",
    ".spyproject", ".idea", "dist", "build", ".tox", ".egg-info",
    ".mypy_cache", ".pytest_cache", ".vscode", ".hg", ".svn",
    ".gitignore", ".coverage",
}


# ══════════════════════════════════════════════════════════════════════
# 1. multi_replace_string — 批量字符串替换
# ══════════════════════════════════════════════════════════════════════

def multi_replace_string(args, base_dir=None):
    """在文件中执行多个字符串替换操作

    对标 VS Code 的 copilot.tools.multiReplaceString

    Parameters
    ----------
    args : dict
        filepath: 文件路径 (optional, 若未指定则对 replacements 中的每个指定)
        replacements: [{"filePath": str, "oldString": str, "newString": str}, ...]
    base_dir : str

    Returns
    -------
    str
    """
    replacements = args.get("replacements", [])
    filepath = args.get("filepath", "")

    if not replacements:
        return "错误: 未指定替换列表"

    results = []
    for i, rep in enumerate(replacements):
        fp = rep.get("filepath", filepath)
        old = rep.get("old_string", rep.get("oldString", ""))
        new = rep.get("new_string", rep.get("newString", ""))

        if not fp:
            results.append(f"[{i}] ❌ 未指定文件路径")
            continue
        if not old:
            results.append(f"[{i}] ❌ 未指定 old_string")
            continue

        path = fp
        if not os.path.isabs(path):
            path = os.path.join(base_dir or os.getcwd(), path)

        if not os.path.isfile(path):
            results.append(f"[{i}] ❌ 文件不存在: {fp}")
            continue

        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            results.append(f"[{i}] ❌ 读取失败: {fp} - {e}")
            continue

        count = content.count(old)
        if count == 0:
            results.append(f"[{i}] ⚠️ 未找到匹配: {fp}")
            continue
        if count > 1:
            results.append(f"[{i}] ⚠️ 找到 {count} 处匹配，跳过（需要唯一匹配）: {fp}")
            continue

        try:
            new_content = content.replace(old, new, 1)
            with open(path, "w", encoding="utf-8") as f:
                f.write(new_content)
            lines_changed = old.count("\n") + 1
            results.append(f"[{i}] ✅ {os.path.basename(fp)} — 替换 {lines_changed} 行")
        except Exception as e:
            results.append(f"[{i}] ❌ 写入失败: {fp} - {e}")

    return "多文件替换结果:\n" + "\n".join(results)


# ══════════════════════════════════════════════════════════════════════
# 2. read_project_structure — 读取项目结构树
# ══════════════════════════════════════════════════════════════════════

def read_project_structure(base_dir=None, max_depth=4, max_files_per_dir=100):
    """读取项目结构树

    对标 VS Code 的 copilot.tools.readProjectStructure

    Parameters
    ----------
    base_dir : str or None
        项目根目录
    max_depth : int
        最大目录深度
    max_files_per_dir : int
        每个目录最多显示的文件数

    Returns
    -------
    str : 格式化的项目树
    """
    root = base_dir or os.getcwd()
    root = os.path.abspath(root)

    if not os.path.isdir(root):
        return f"目录不存在: {root}"

    # 关键文件优先显示
    PRIORITY_FILES = {
        "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt",
        "README.md", "README.rst", "LICENSE", "Makefile", "Dockerfile",
        ".gitignore", ".env.example", "package.json", "tsconfig.json",
        "Cargo.toml", "go.mod", "CMakeLists.txt",
    }

    lines = [f"📂 {os.path.basename(root) or root}/"]

    def _walk(path, prefix="", depth=0):
        if depth > max_depth:
            return

        try:
            entries = sorted(os.listdir(path))
        except PermissionError:
            lines.append(f"{prefix}└── [权限不足]")
            return

        # 分离目录、优先文件、普通文件
        dirs = []
        priority_files = []
        normal_files = []
        hidden = []

        for e in entries:
            ep = os.path.join(path, e)
            if os.path.isdir(ep):
                if e in EXCLUDE_DIRS or e.startswith("."):
                    hidden.append(e)
                else:
                    dirs.append(e)
            else:
                if e in PRIORITY_FILES:
                    priority_files.append(e)
                elif e.startswith("."):
                    hidden.append(e)
                else:
                    normal_files.append(e)

        # 显示关键文件
        for f in priority_files:
            connector = "└──" if not dirs and not normal_files else "├──"
            lines.append(f"{prefix}{connector} ⭐ {f}")

        # 显示普通文件（限制数量）
        shown_files = normal_files[:max_files_per_dir]
        for i, f in enumerate(shown_files):
            is_last_in_group = (i == len(shown_files) - 1 and not dirs)
            connector = "└──" if is_last_in_group else "├──"
            lines.append(f"{prefix}{connector} 📄 {f}")

        if len(normal_files) > max_files_per_dir:
            lines.append(f"{prefix}├── ... 还有 {len(normal_files) - max_files_per_dir} 个文件")

        # 隐藏文件计数
        if hidden:
            lines.append(f"{prefix}├── 📦 {len(hidden)} 个隐藏文件/目录")

        # 递归子目录
        for i, d in enumerate(dirs):
            connector = "└──" if i == len(dirs) - 1 else "├──"
            ext = "├──" if i < len(dirs) - 1 else "   "
            lines.append(f"{prefix}{connector} 📁 {d}/")
            _walk(os.path.join(path, d), prefix + ext, depth + 1)

    _walk(root)
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════
# 3. find_test_files — 查找测试文件
# ══════════════════════════════════════════════════════════════════════

def find_test_files(filepath=None, base_dir=None):
    """查找与指定文件对应的测试文件（或反向查找）

    对标 VS Code 的 copilot.tools.findTestFiles

    Parameters
    ----------
    filepath : str or None
        源文件路径。如果未指定，列出所有测试文件
    base_dir : str or None
        项目根目录

    Returns
    -------
    str : 匹配的测试文件列表
    """
    cwd = base_dir or os.getcwd()

    if filepath and os.path.isfile(filepath):
        # 查找对应测试文件
        stem = Path(filepath).stem
        # 常见测试命名模式
        test_patterns = [
            f"test_{stem}*",
            f"{stem}_test*",
            f"test{stem}*",
            f"{stem}Test*",
            f"Test{stem}*",
        ]
        # 常见测试目录
        test_dirs = ["tests", "test", "testing", "__tests__", "spec", "specs"]

        results = []
        for root, dirs, files in os.walk(cwd):
            dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS and not d.startswith(".")]
            for f in files:
                fl = f.lower()
                for pattern in test_patterns:
                    if fnmatch_fallback(fl, pattern.lower()):
                        rel = os.path.relpath(os.path.join(root, f), cwd)
                        results.append(rel)
                        break

        if results:
            return f"🔗 {Path(filepath).name} 的测试文件:\n" + "\n".join(f"  - {r}" for r in results)
        else:
            return f"未找到 {Path(filepath).name} 对应的测试文件"

    else:
        # 列出所有测试文件
        test_dirs = ["tests", "test", "testing", "__tests__", "spec", "specs"]
        all_tests = []

        for root, dirs, files in os.walk(cwd):
            dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS and not d.startswith(".")]
            for f in files:
                fl = f.lower()
                if (fl.startswith("test_") or fl.endswith("_test") or
                    fl.startswith("test") or "test" in Path(root).parts.lower()):
                    ext = os.path.splitext(f)[1]
                    if ext in ('.py', '.js', '.ts', '.java', '.go', '.rs', '.rb', '.php'):
                        rel = os.path.relpath(os.path.join(root, f), cwd)
                        all_tests.append(rel)

        if all_tests:
            return f"🧪 共 {len(all_tests)} 个测试文件:\n" + "\n".join(
                f"  - {t}" for t in all_tests[:50]
            ) + (f"\n  ... 还有 {len(all_tests) - 50} 个" if len(all_tests) > 50 else "")
        else:
            return "未找到测试文件"


def fnmatch_fallback(name, pattern):
    """简易 fnmatch（避免 import 问题）"""
    import fnmatch
    return fnmatch.fnmatch(name, pattern)


# ══════════════════════════════════════════════════════════════════════
# 4. search_workspace_symbols — 工作区符号搜索
# ══════════════════════════════════════════════════════════════════════

def search_workspace_symbols(query, base_dir=None, max_results=50):
    """在工作区中搜索符号定义（函数/类/变量）

    对标 VS Code 的 copilot.tools.searchWorkspaceSymbols

    Parameters
    ----------
    query : str
        符号名称（支持部分匹配）
    base_dir : str or None
        项目根目录
    max_results : int
        最大结果数

    Returns
    -------
    str : 匹配的符号列表
    """
    cwd = base_dir or os.getcwd()

    if not query:
        return "错误: 未指定搜索符号名"

    query_lower = query.lower()

    # Python 符号模式
    py_def = re.compile(
        r'^\s*(?:async\s+)?(?:def|class)\s+(\w[\w.]*)', re.MULTILINE
    )
    py_var = re.compile(
        r'^\s*(\w[\w]*)\s*[:=]\s*', re.MULTILINE
    )

    # JS/TS 符号
    js_def = re.compile(
        r'^\s*(?:export\s+)?(?:async\s+)?(?:function\s+|class\s+|interface\s+|'
        r'type\s+|enum\s+|const\s+|let\s+|var\s+)(\w[\w.]*)', re.MULTILINE
    )

    results = []
    for root, dirs, files in os.walk(cwd):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS and not d.startswith(".")]
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            if ext == '.py':
                pattern = py_def
            elif ext in ('.js', '.ts', '.tsx', '.jsx'):
                pattern = js_def
            else:
                continue

            filepath = os.path.join(root, f)
            try:
                with open(filepath, "r", encoding="utf-8", errors="replace") as fh:
                    content = fh.read()
            except (OSError, UnicodeDecodeError):
                continue

            for line_no, line in enumerate(content.split("\n"), 1):
                for m in pattern.finditer(line):
                    sym = m.group(1)
                    if query_lower in sym.lower():
                        rel = os.path.relpath(filepath, cwd)
                        results.append({
                            "name": sym,
                            "file": rel,
                            "line": line_no,
                            "text": line.strip()[:120],
                        })
                        if len(results) >= max_results:
                            break
                if len(results) >= max_results:
                    break
            if len(results) >= max_results:
                break

    if not results:
        return f"🔍 未找到与 '{query}' 匹配的符号"

    # 分类显示
    lines = [f"🔍 符号搜索: '{query}' — {len(results)} 个结果\n"]
    for r in results:
        icon = "📦" if r["text"].startswith("class ") else "🔧"
        lines.append(
            f"{icon} **{r['name']}** — {r['file']}:{r['line']}\n"
            f"   `{r['text']}`"
        )
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════
# 5. view_image — 查看图像文件
# ══════════════════════════════════════════════════════════════════════

def view_image(filepath, base_dir=None):
    """查看图像文件的元数据

    对标 VS Code 的 copilot.tools.viewImage
    （Spyder 中无法直接渲染图片，返回元数据 + base64 编码）

    Parameters
    ----------
    filepath : str
        图像文件路径
    base_dir : str or None
        基础目录

    Returns
    -------
    str : 图像元数据
    """
    path = filepath
    if not os.path.isabs(path):
        path = os.path.join(base_dir or os.getcwd(), path)

    if not os.path.isfile(path):
        return f"文件不存在: {path}"

    ext = os.path.splitext(path)[1].lower()
    if ext not in ('.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp', '.svg', '.ico'):
        return f"不支持的图像格式: {ext}"

    try:
        from PIL import Image
        with Image.open(path) as img:
            info = {
                "文件": os.path.basename(path),
                "格式": img.format or ext.upper(),
                "尺寸": f"{img.width} × {img.height} px",
                "模式": img.mode,
            }
            # 尝试获取更多元数据
            if hasattr(img, 'info'):
                for k in ('dpi', 'exif', 'icc_profile'):
                    if k in img.info:
                        v = img.info[k]
                        if isinstance(v, bytes):
                            v = f"({len(v)} bytes)"
                        info[k] = str(v)[:100]

            lines = ["🖼 **图像信息**"]
            for k, v in info.items():
                lines.append(f"  - {k}: {v}")
            return "\n".join(lines)
    except ImportError:
        # PIL 不可用，返回基本信息
        size = os.path.getsize(path)
        lines = [
            f"🖼 **图像文件**",
            f"  - 文件: {os.path.basename(path)}",
            f"  - 格式: {ext.upper()}",
            f"  - 大小: {size:,} bytes",
        ]
        return "\n".join(lines)
    except Exception as e:
        return f"读取图像失败: {e}"


# ══════════════════════════════════════════════════════════════════════
# 6. Notebook 运行/输出工具
# ══════════════════════════════════════════════════════════════════════

def notebook_run_cell(filepath, cell_index, base_dir=None):
    """触发 Notebook 单元格执行

    对标 VS Code 的 copilot.tools.runNotebookCell

    Parameters
    ----------
    filepath : str
        Notebook 文件路径
    cell_index : int
        单元格索引
    base_dir : str

    Returns
    -------
    str
    """
    path = filepath
    if not os.path.isabs(path):
        path = os.path.join(base_dir or os.getcwd(), path)

    if not os.path.isfile(path):
        return f"文件不存在: {path}"

    try:
        with open(path, "r", encoding="utf-8") as f:
            nb = json.load(f)
    except Exception as e:
        return f"读取 Notebook 失败: {e}"

    cells = nb.get("cells", [])
    if cell_index < 0 or cell_index >= len(cells):
        return f"单元格索引 {cell_index} 超出范围 (共 {len(cells)} 个)"

    cell = cells[cell_index]
    if cell.get("cell_type") != "code":
        return f"单元格 [{cell_index}] 不是代码单元格 (类型: {cell.get('cell_type')})"

    source = cell.get("source", [])
    if isinstance(source, list):
        source = "".join(source)

    # 尝试用 subprocess 执行 Python 代码
    try:
        result = subprocess.run(
            [sys.executable, "-c", source],
            cwd=base_dir or os.getcwd(),
            capture_output=True,
            encoding='utf-8',
            errors='replace',
            timeout=30,
            creationflags=_SUBPROCESS_FLAGS,
        )

        # 更新 Notebook 中的 outputs
        outputs = cell.get("outputs", [])
        output_text = ""
        if result.stdout:
            output_text += result.stdout
        if result.stderr:
            output_text += result.stderr

        outputs.append({
            "output_type": "execute_result" if result.returncode == 0 else "error",
            "text": output_text,
            "execution_count": cell.get("execution_count", 0) + 1,
        })
        cell["outputs"] = outputs
        cell["execution_count"] = cell.get("execution_count", 0) + 1

        # 写回
        nb["cells"] = cells
        with open(path, "w", encoding="utf-8") as f:
            json.dump(nb, f, indent=1, ensure_ascii=False)

        status = "✅" if result.returncode == 0 else "⚠️"
        return (f"{status} 单元格 [{cell_index}] 执行完成 (exit code: {result.returncode})\n\n"
                f"```\n{output_text[:3000]}\n```")
    except subprocess.TimeoutExpired:
        return f"⏱ 单元格 [{cell_index}] 执行超时 (30s)"
    except Exception as e:
        return f"❌ 执行失败: {e}"


def notebook_get_output(filepath, cell_index, base_dir=None):
    """获取 Notebook 单元格的最近输出

    对标 VS Code 的 copilot.tools.getNotebookCellOutput

    Parameters
    ----------
    filepath : str
        Notebook 文件路径
    cell_index : int
        单元格索引
    base_dir : str

    Returns
    -------
    str
    """
    path = filepath
    if not os.path.isabs(path):
        path = os.path.join(base_dir or os.getcwd(), path)

    if not os.path.isfile(path):
        return f"文件不存在: {path}"

    try:
        with open(path, "r", encoding="utf-8") as f:
            nb = json.load(f)
    except Exception as e:
        return f"读取 Notebook 失败: {e}"

    cells = nb.get("cells", [])
    if cell_index < 0 or cell_index >= len(cells):
        return f"单元格索引 {cell_index} 超出范围 (共 {len(cells)} 个)"

    cell = cells[cell_index]
    outputs = cell.get("outputs", [])

    if not outputs:
        return f"单元格 [{cell_index}] 无输出 (可能尚未执行)"

    lines = [f"📓 单元格 [{cell_index}] 输出 ({len(outputs)} 条):\n"]
    for i, out in enumerate(outputs):
        otype = out.get("output_type", "unknown")
        text = out.get("text", "")
        if isinstance(text, list):
            text = "".join(text)
        lines.append(f"--- 输出 #{i+1} ({otype}) ---")
        lines.append(text[:5000] if text else "(空)")
        lines.append("")

    return "\n".join(lines)
