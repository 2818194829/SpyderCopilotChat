# -*- coding: utf-8 -*-
"""
检查点（Checkpoint）— v0.5.0 / Cline Edition

对标 Cline：每次 AI 改动文件前先保存受影响文件的快照，出问题时可一键
「撤销本步」。与 Git 相互独立，不动用户的提交历史。

实现刻意保持轻量：只快照「本次工具调用声明会改动的文件」，而不是整个
项目（避免大仓库性能问题），快照以 JSON 存在 ~/.spyder_copilot_chat/
checkpoints/ 下，默认保留最近 30 个。
"""

import json
import logging
import os
import time
import uuid

from .approval_card import (
    affected_files, parse_tool_args, read_text, resolve_path,
)

logger = logging.getLogger(__name__)

CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".spyder_copilot_chat")
MAX_CHECKPOINTS = 30
MAX_SNAPSHOT_BYTES = 2 * 1024 * 1024  # 单个文件超过 2MB 不做内容快照


class CheckpointStore:
    """按工具调用批次保存 / 恢复文件快照"""

    def __init__(self, root=None):
        self.root = root or os.path.join(CONFIG_DIR, "checkpoints")
        self._last_id = None

    # ── 公开 API ──────────────────────────────────────────────────

    def snapshot(self, tool_calls, base_dir=None):
        """在真正执行工具前调用，返回 checkpoint id（无文件可快照时返回 None）"""
        paths = []
        for tc in tool_calls or []:
            fn = tc.get("function", {}) or {}
            args = parse_tool_args(fn.get("arguments", "{}"))
            for p in affected_files(fn.get("name", ""), args, base_dir):
                if p not in paths:
                    paths.append(p)
        if not paths:
            return None

        entries = []
        for path in paths:
            existed = os.path.isfile(path)
            content = None
            skipped = False
            if existed:
                try:
                    if os.path.getsize(path) > MAX_SNAPSHOT_BYTES:
                        skipped = True
                    else:
                        content, _ = read_text(path)
                        if "\x00" in content:  # 二进制文件不快照
                            skipped = True
                            content = None
                except Exception as exc:
                    logger.debug("Checkpoint read failed for %s: %s", path, exc)
                    skipped = True
            entries.append({
                "path": path,
                "existed": existed,
                "skipped": skipped,
                "content": content,
            })

        cp_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        payload = {
            "id": cp_id,
            "created": time.time(),
            "base_dir": base_dir or os.getcwd(),
            "files": entries,
        }
        try:
            os.makedirs(self.root, exist_ok=True)
            with open(os.path.join(self.root, cp_id + ".json"),
                      "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
        except Exception as exc:
            logger.warning("Checkpoint save failed: %s", exc)
            return None

        self._last_id = cp_id
        self._prune()
        return cp_id

    def remember(self, cp_id):
        """v0.5.5：快照在后台线程生成后，在主线程确认最近一次 id。"""
        if cp_id:
            self._last_id = cp_id

    def restore(self, cp_id=None):
        """恢复某个检查点，返回 (成功数, 说明文本列表)"""
        cp_id = cp_id or self._last_id
        if not cp_id:
            return 0, ["没有可撤销的检查点"]

        path = os.path.join(self.root, cp_id + ".json")
        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except Exception as exc:
            return 0, [f"读取检查点失败: {exc}"]

        ok, notes = 0, []
        for entry in payload.get("files", []):
            fp = entry.get("path")
            if not fp:
                continue
            if entry.get("skipped"):
                notes.append(f"⚠ 跳过（大文件/二进制）: {fp}")
                continue
            try:
                if entry.get("existed"):
                    os.makedirs(os.path.dirname(fp), exist_ok=True)
                    with open(fp, "w", encoding="utf-8") as f:
                        f.write(entry.get("content") or "")
                elif os.path.isfile(fp):
                    os.remove(fp)  # 新建的文件 → 撤销即删除
                ok += 1
            except Exception as exc:
                notes.append(f"❌ 恢复失败 {fp}: {exc}")

        if not notes:
            notes.append(f"已恢复 {ok} 个文件到操作前状态")
        return ok, notes

    def last_id(self):
        return self._last_id

    # ── 内部 ──────────────────────────────────────────────────────

    def _prune(self):
        try:
            files = sorted(
                (f for f in os.listdir(self.root) if f.endswith(".json")),
                reverse=True,
            )
            for name in files[MAX_CHECKPOINTS:]:
                try:
                    os.remove(os.path.join(self.root, name))
                except Exception:
                    pass
        except Exception:
            pass
