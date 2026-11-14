# -*- coding: utf-8 -*-
"""
Notebook 操作工具

对标 GitHub Copilot Chat 的:
  - editNotebook: 编辑笔记本文件
  - runNotebookCell: 运行笔记本单元格
  - getNotebookCellOutput: 获取单元格输出
  - newJupyterNotebook: 创建新 Jupyter Notebook
"""

import json
import logging
import os

logger = logging.getLogger(__name__)


def create_notebook(filepath, cells=None, base_dir=None):
    """创建新的 Jupyter Notebook 文件

    对标 VS Code 的 newJupyterNotebook

    Parameters
    ----------
    filepath : str
        文件路径
    cells : list[dict] or None
        初始单元格列表，每项: {"cell_type": "code"/"markdown", "source": str}
    base_dir : str or None
        基础目录

    Returns
    -------
    str : 执行结果
    """
    path = filepath
    if not os.path.isabs(path):
        path = os.path.join(base_dir or os.getcwd(), path)

    if not path.endswith(".ipynb"):
        path += ".ipynb"

    if os.path.exists(path):
        return f"文件已存在: {path}"

    if cells is None:
        cells = [{"cell_type": "code", "source": "", "metadata": {}}]

    notebook = {
        "nbformat": 4,
        "nbformat_minor": 5,
        "metadata": {
            "kernelspec": {
                "display_name": "Python 3",
                "language": "python",
                "name": "python3"
            },
            "language_info": {
                "name": "python",
                "version": "3.9.0"
            }
        },
        "cells": []
    }

    for cell in cells:
        cell_type = cell.get("cell_type", "code")
        source = cell.get("source", "")
        metadata = cell.get("metadata", {})

        nb_cell = {
            "cell_type": cell_type,
            "metadata": metadata,
            "source": source.split("\n") if isinstance(source, str) else source,
        }

        if cell_type == "code":
            nb_cell["outputs"] = []
            nb_cell["execution_count"] = None

        notebook["cells"].append(nb_cell)

    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(notebook, f, indent=1, ensure_ascii=False)
        return f"✅ Notebook 已创建: {path}\n包含 {len(cells)} 个单元格"
    except Exception as e:
        return f"❌ 创建 Notebook 失败: {e}"


def get_notebook_info(filepath, base_dir=None):
    """获取 Notebook 的摘要信息

    对标 VS Code 的 copilot.getNotebookSummary

    Parameters
    ----------
    filepath : str
        Notebook 文件路径
    base_dir : str or None
        基础目录

    Returns
    -------
    str : Notebook 摘要
    """
    path = filepath
    if not os.path.isabs(path):
        path = os.path.join(base_dir or os.getcwd(), path)

    if not os.path.isfile(path):
        return f"文件不存在: {path}"

    try:
        with open(path, "r", encoding="utf-8") as f:
            nb = json.load(f)
    except json.JSONDecodeError as e:
        return f"Notebook 文件格式错误: {e}"
    except Exception as e:
        return f"读取文件失败: {e}"

    cells = nb.get("cells", [])
    code_cells = sum(1 for c in cells if c.get("cell_type") == "code")
    md_cells = sum(1 for c in cells if c.get("cell_type") == "markdown")
    raw_cells = sum(1 for c in cells if c.get("cell_type") == "raw")

    lines = [
        f"📓 Notebook: {os.path.basename(path)}",
        f"   合计: {len(cells)} 个单元格",
        f"   💻 代码: {code_cells} | 📝 Markdown: {md_cells} | 📄 原始: {raw_cells}",
        "",
    ]

    for i, cell in enumerate(cells):
        ctype = cell.get("cell_type", "unknown")
        source = cell.get("source", [])
        if isinstance(source, list):
            source = "".join(source)
        preview = source[:80].replace("\n", " ").strip()
        icon = {"code": "💻", "markdown": "📝", "raw": "📄"}.get(ctype, "❓")
        lines.append(f"  [{i}] {icon} {ctype}: {preview}...")

    return "\n".join(lines)


def add_notebook_cell(filepath, cell_type="code", source="",
                      index=None, base_dir=None):
    """向 Notebook 添加单元格

    对标 VS Code 的 editNotebook (insert)

    Parameters
    ----------
    filepath : str
        Notebook 文件路径
    cell_type : str
        单元格类型: "code" 或 "markdown"
    source : str
        单元格源代码
    index : int or None
        插入位置（None 表示追加到末尾）
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
        return f"文件不存在: {path}"

    try:
        with open(path, "r", encoding="utf-8") as f:
            nb = json.load(f)
    except Exception as e:
        return f"读取 Notebook 失败: {e}"

    new_cell = {
        "cell_type": cell_type,
        "metadata": {},
        "source": source.split("\n") if isinstance(source, str) else source,
    }
    if cell_type == "code":
        new_cell["outputs"] = []
        new_cell["execution_count"] = None

    cells = nb.get("cells", [])
    if index is not None and 0 <= index <= len(cells):
        cells.insert(index, new_cell)
    else:
        cells.append(new_cell)

    nb["cells"] = cells

    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(nb, f, indent=1, ensure_ascii=False)
        pos = f"位置 {index}" if index is not None else "末尾"
        return f"✅ 已在 {pos} 添加 {cell_type} 单元格\n当前共 {len(cells)} 个单元格"
    except Exception as e:
        return f"❌ 写入 Notebook 失败: {e}"


def edit_notebook_cell(filepath, cell_index, new_source, base_dir=None):
    """编辑 Notebook 中指定索引的单元格

    对标 VS Code 的 editNotebook (edit)

    Parameters
    ----------
    filepath : str
        Notebook 文件路径
    cell_index : int
        单元格索引（0-based）
    new_source : str
        新的源代码
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
        return f"文件不存在: {path}"

    try:
        with open(path, "r", encoding="utf-8") as f:
            nb = json.load(f)
    except Exception as e:
        return f"读取 Notebook 失败: {e}"

    cells = nb.get("cells", [])
    if cell_index < 0 or cell_index >= len(cells):
        return f"错误: 单元格索引 {cell_index} 超出范围 (共 {len(cells)} 个)"

    old_type = cells[cell_index].get("cell_type", "code")
    cells[cell_index]["source"] = (
        new_source.split("\n") if isinstance(new_source, str) else new_source
    )

    nb["cells"] = cells

    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(nb, f, indent=1, ensure_ascii=False)
        return f"✅ 已编辑单元格 [{cell_index}] ({old_type})"
    except Exception as e:
        return f"❌ 写入 Notebook 失败: {e}"


def delete_notebook_cell(filepath, cell_index, base_dir=None):
    """删除 Notebook 中指定索引的单元格

    对标 VS Code 的 editNotebook (delete)

    Parameters
    ----------
    filepath : str
        Notebook 文件路径
    cell_index : int
        单元格索引（0-based）
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
        return f"文件不存在: {path}"

    try:
        with open(path, "r", encoding="utf-8") as f:
            nb = json.load(f)
    except Exception as e:
        return f"读取 Notebook 失败: {e}"

    cells = nb.get("cells", [])
    if cell_index < 0 or cell_index >= len(cells):
        return f"错误: 单元格索引 {cell_index} 超出范围 (共 {len(cells)} 个)"

    deleted = cells.pop(cell_index)
    nb["cells"] = cells

    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(nb, f, indent=1, ensure_ascii=False)
        return f"✅ 已删除单元格 [{cell_index}] ({deleted.get('cell_type', '?')})"
    except Exception as e:
        return f"❌ 写入 Notebook 失败: {e}"
