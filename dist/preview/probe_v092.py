# -*- coding: utf-8 -*-
"""v0.8.12 探针：真 Spyder 里把截图粘进输入框，看它是否真的变成附件。

跑的是装进 user site 的 wheel，不往 sys.path 插仓库路径。
验的是用户的原话：「输入框无法上传图片」——现在 Ctrl+V 应该出来缩略图。
"""
import json
import os
import sys
import time
import traceback

OUT = os.path.dirname(os.path.abspath(__file__))
RESULT = os.path.join(OUT, "probe_v092.json")
SHOT = os.path.join(OUT, "probe-v092.png")

state = {"done": False}


def fake_shot():
    """造一张「IDE 截图」当剪贴板内容。"""
    from qtpy.QtGui import QImage, QPainter, QColor, QFont
    from qtpy.QtCore import Qt

    img = QImage(420, 240, QImage.Format_ARGB32)
    img.fill(QColor("#1b1f27"))
    p = QPainter(img)
    p.setPen(QColor("#8b93a1"))
    f = QFont("Consolas")
    f.setPixelSize(12)
    p.setFont(f)
    p.drawText(16, 34, "Traceback (most recent call last):")
    p.setPen(QColor("#e06c75"))
    p.drawText(16, 58, "    data._selection()")
    p.setPen(QColor("#d19a66"))
    p.drawText(16, 82, "AttributeError: _selection")
    p.end()
    return img


def do_probe(main):
    if state["done"]:
        return
    from qtpy.QtWidgets import QApplication, QDialog, QVBoxLayout
    from qtpy.QtCore import QTimer

    result = {"ok": False, "steps": []}

    def note(key, value):
        result[key] = value
        result["steps"].append("%s=%r" % (key, value))

    def finish():
        state["done"] = True
        with open(RESULT, "w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=2)
        QApplication.instance().quit()

    app = QApplication.instance()

    def wait(ms):
        end = time.time() + ms / 1000.0
        while time.time() < end:
            app.processEvents()
            time.sleep(0.01)

    try:
        import spyder_copilot_chat as pkg
        from spyder_copilot_chat.widgets import chat_widget as cw
        from spyder_copilot_chat.widgets import attachments as att

        note("plugin_version", pkg.__version__)
        note("plugin_path", os.path.dirname(pkg.__file__))

        # 真窗口 + 真面板（不抢插件自己的 dock，避免把主界面搞乱）
        dlg = QDialog(main)
        dlg.setWindowTitle("图片附件探针")
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(0, 0, 0, 0)
        panel = cw.CopilotChatPanel()
        lay.addWidget(panel)
        dlg.resize(600, 780)
        dlg.show()
        wait(700)
        panel._welcome.setVisible(False)

        note("attach_button_exists", hasattr(panel, "_attach_btn"))
        note("attach_button_visible", panel._attach_btn.isVisible()
             if hasattr(panel, "_attach_btn") else None)

        # ── 1) 剪贴板放一张截图，Ctrl+V ──
        img = fake_shot()
        QApplication.clipboard().setImage(img)
        wait(150)
        note("clipboard_has_image",
             bool(QApplication.clipboard().mimeData().hasImage()))

        panel._input.setFocus()
        panel._input.paste()          # 等价于用户按 Ctrl+V
        wait(400)

        note("attachments_after_paste", len(panel._attachments))
        note("attach_bar_visible", not panel._attach_bar.isHidden())
        note("thumb_widgets", panel._attach_lay.count())
        note("composer_text_after_paste",
             repr(panel._input.toPlainText())[:60])
        if panel._attachments:
            path = panel._attachments[0]["path"]
            note("saved_path_exists", os.path.exists(path))
            note("saved_is_png",
                 att.sniff_ext(open(path, "rb").read()) == ".png")
            note("saved_under_home",
                 path.startswith(os.path.expanduser("~")))

        # ── 2) 再多贴一张 + 拖拽路径（直接调 on_image_files） ──
        panel._input.paste()
        wait(200)
        note("dedupe_same_image", len(panel._attachments))
        second = fake_shot().scaled(200, 120)
        tmp2 = os.path.join(os.environ.get("TEMP", OUT), "probe_v092_b.png")
        second.save(tmp2, "PNG")
        panel._on_image_files([tmp2])
        wait(300)
        note("attachments_after_second", len(panel._attachments))

        # ── 3) 组装成多模态 content ──
        panel._input.setPlainText("这个报错什么意思？")
        content = panel._content_with_images("这个报错什么意思？")
        note("content_is_list", isinstance(content, list))
        if isinstance(content, list):
            note("content_types", [p.get("type") for p in content])
            note("first_is_local_ref",
                 att.path_of(content[1]) is not None if len(content) > 1 else None)

        # ── 4) 发请求前会被编码成 data URL（不真的发网络） ──
        worker = cw.ChatWorker(
            [{"role": "user", "content": content}],
            {"provider": "custom", "api_url": "http://127.0.0.1:1"})
        prepared = worker._prepare_messages()
        body = prepared[-1]["content"]
        note("prepared_kinds", [p.get("type") for p in body])
        note("encoded_to_data_url",
             all(str(p.get("image_url", {}).get("url", "")).startswith("data:")
                 for p in body if p.get("type") == "image_url"))
        note("no_local_ref_left",
             all(att.path_of(p) is None for p in body))

        # 气泡里也摆一次，截图能看到效果
        bubble = panel._add_msg("user", "这个报错什么意思？")
        bubble.set_attachments([a["path"] for a in panel._attachments])
        reply = panel._add_msg("assistant", "")
        reply.append_block(
            "看到了：`data._selection()` 找不到 —— 是块标记的基类继承错了。",
            kind="result")
        wait(700)

        try:
            dlg.grab().save(SHOT)
            note("screenshot", SHOT)
        except Exception as exc:              # noqa: BLE001
            note("screenshot_error", repr(exc))

        result["ok"] = (
            len(panel._attachments) == 2
            and note_ok(result.get("saved_is_png"))
            and result.get("encoded_to_data_url") is True
            and result.get("no_local_ref_left") is True
            and result.get("attach_bar_visible") is True)
        dlg.close()
        QTimer.singleShot(300, finish)
    except Exception:
        result["traceback"] = traceback.format_exc()
        finish()


def note_ok(value):
    return value is True


def main_entry():
    import spyder.app.mainwindow as mw
    from qtpy.QtCore import QTimer

    def log(msg):
        print("[probe] " + msg, flush=True)

    original = mw.MainWindow.showMaximized

    def patched(self, *args, **kwargs):
        out = original(self, *args, **kwargs)
        log("showMaximized")
        QTimer.singleShot(4500, lambda: do_probe(self))
        return out

    mw.MainWindow.showMaximized = patched

    orig_init = mw.MainWindow.__init__

    def patched_init(self, *a, **kw):
        orig_init(self, *a, **kw)
        log("constructed")
        QTimer.singleShot(15000, lambda: do_probe(self))
        QTimer.singleShot(28000, lambda: do_probe(self))

    mw.MainWindow.__init__ = patched_init

    sys.argv = [sys.argv[0], "--new-instance"]
    log("starting spyder")
    from spyder.app import start
    start.main()
    log("spyder exited")


if __name__ == "__main__":
    main_entry()
