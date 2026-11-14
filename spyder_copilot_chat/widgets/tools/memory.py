# -*- coding: utf-8 -*-
"""
AI 工具记忆系统 — 对标 VS Code Copilot Chat 的 copilot_memory

AI 可以通过 memory_save / memory_recall 工具保存和回忆信息。
数据持久化存储到 ~/.spyder_copilot_chat/memory.json
"""

import json
import logging
import os

logger = logging.getLogger(__name__)

MEMORY_DIR = os.path.join(os.path.expanduser("~"), ".spyder_copilot_chat")
MEMORY_FILE = os.path.join(MEMORY_DIR, "memory.json")


def _ensure_dir():
    os.makedirs(MEMORY_DIR, exist_ok=True)


def _read_all():
    """读取所有记忆"""
    try:
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _write_all(data):
    """写入所有记忆"""
    _ensure_dir()
    with open(MEMORY_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def save_memory(namespace, key, value):
    """保存一条记忆"""
    data = _read_all()
    if namespace not in data:
        data[namespace] = {}
    data[namespace][key] = {
        "value": value,
    }
    _write_all(data)
    logger.debug("Memory saved: [%s] %s", namespace, key)


def load_memory(namespace, key):
    """加载一条记忆"""
    data = _read_all()
    ns = data.get(namespace, {})
    entry = ns.get(key)
    if entry is None:
        return None
    return entry.get("value", "")


def list_memories(namespace=None):
    """列出所有记忆，返回 [(namespace, key, value), ...]"""
    data = _read_all()
    result = []
    for ns, keys in data.items():
        if namespace and ns != namespace:
            continue
        for key, entry in keys.items():
            result.append((ns, key, entry.get("value", "")))
    return result


def delete_memory(namespace, key):
    """删除一条记忆"""
    data = _read_all()
    if namespace in data and key in data[namespace]:
        del data[namespace][key]
        if not data[namespace]:
            del data[namespace]
        _write_all(data)
        return True
    return False


def clear_all():
    """清空所有记忆"""
    _write_all({})
