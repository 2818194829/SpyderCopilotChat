# -*- coding: utf-8 -*-
"""聊天历史管理 — 保存/加载/搜索聊天记录"""

import json
import os
import uuid
from datetime import datetime

CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".spyder_copilot_chat")
HISTORY_DIR = os.path.join(CONFIG_DIR, "history")


def _ensure_dir():
    os.makedirs(HISTORY_DIR, exist_ok=True)


def _chat_path(chat_id):
    return os.path.join(HISTORY_DIR, f"{chat_id}.json")


def save_chat(messages, title=None):
    """保存聊天记录，返回 chat_id"""
    _ensure_dir()
    chat_id = str(uuid.uuid4())[:8]
    now = datetime.now().isoformat()
    first_user_msg = next(
        (m["content"][:50] for m in messages if m.get("role") == "user"),
        "空对话"
    )
    data = {
        "id": chat_id,
        "title": title or first_user_msg,
        "created": now,
        "updated": now,
        "messages": messages,
        "message_count": len(messages),
    }
    with open(_chat_path(chat_id), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return chat_id


def load_chat(chat_id):
    """加载单个聊天记录"""
    try:
        with open(_chat_path(chat_id), "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def delete_chat(chat_id):
    """删除聊天记录"""
    try:
        os.remove(_chat_path(chat_id))
        return True
    except OSError:
        return False


def list_chats(limit=50):
    """列出最近的聊天记录"""
    _ensure_dir()
    chats = []
    try:
        for fname in sorted(os.listdir(HISTORY_DIR), reverse=True):
            if not fname.endswith(".json"):
                continue
            try:
                with open(os.path.join(HISTORY_DIR, fname), "r", encoding="utf-8") as f:
                    chat = json.load(f)
                    chats.append({
                        "id": chat.get("id", fname[:-5]),
                        "title": chat.get("title", "无标题"),
                        "created": chat.get("created", ""),
                        "updated": chat.get("updated", ""),
                        "message_count": chat.get("message_count", 0),
                    })
            except Exception:
                continue
            if len(chats) >= limit:
                break
    except FileNotFoundError:
        pass
    return chats


def search_chats(keyword):
    """搜索聊天记录"""
    results = []
    for fname in os.listdir(HISTORY_DIR):
        if not fname.endswith(".json"):
            continue
        try:
            with open(os.path.join(HISTORY_DIR, fname), "r", encoding="utf-8") as f:
                chat = json.load(f)
            if keyword.lower() in chat.get("title", "").lower():
                results.append(chat)
                continue
            for msg in chat.get("messages", []):
                if keyword.lower() in msg.get("content", "").lower():
                    results.append(chat)
                    break
        except Exception:
            continue
    return results
