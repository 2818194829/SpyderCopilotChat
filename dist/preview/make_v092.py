# -*- coding: utf-8 -*-
"""出图：粘贴图片后 composer 里的缩略图 + 气泡里的缩略图。

必须用默认 Qt 平台（不要 offscreen）：offscreen 拿不到系统字体，
截出来全是色条。
"""
import os
import sys
import tempfile

ROOT = (r"C:\Users\Mr_Yang\Documents\deepseek-harness"
        r"\default-workspace\spyder-copilot-chat")
sys.path.insert(0, ROOT)

from qtpy.QtCore import QRect, Qt, QSize                        # noqa: E402
from qtpy.QtGui import (QColor, QFont, QImage, QPainter,        # noqa: E402
                        QPixmap)
from qtpy.QtWidgets import QApplication                         # noqa: E402

APP = QApplication.instance() or QApplication([])

from spyder_copilot_chat.widgets import settings_dialog as sd   # noqa: E402
from spyder_copilot_chat.widgets.chat_widget import (           # noqa: E402
    CopilotChatPanel,
)

OUT = os.path.join(ROOT, "docs", "images")
CFG_DIR = tempfile.mkdtemp(prefix="v092_img_")
sd.CONFIG_DIR = CFG_DIR
sd.CONFIG_FILE = os.path.join(CFG_DIR, "config.json")


def fake_screenshot():
    """造一张「IDE 报错」截图，当用户粘进来的图用。"""
    img = QImage(520, 300, QImage.Format_ARGB32)
    img.fill(QColor("#1b1f27"))
    p = QPainter(img)
    p.fillRect(0, 0, 520, 26, QColor("#232833"))
    p.setPen(QColor("#6b7280"))
    f = QFont("Consolas")
    f.setPixelSize(12)
    p.setFont(f)
    p.drawText(10, 18, "spyder_copilot_chat/widgets/editor_diff.py")
    p.setPen(QColor("#8b93a1"))
    p.drawText(20, 58, "Traceback (most recent call last):")
    p.drawText(20, 80, '  File "lsp_mixin.py", line 639, in _process_code_analysis')
    p.setPen(QColor("#e06c75"))
    p.drawText(20, 102, "    data._selection()")
    p.setPen(QColor("#d19a66"))
    p.drawText(20, 130, "AttributeError: _selection")
    p.setPen(QColor("#8b93a1"))
    p.drawText(20, 158, "LSPHandleError: Error when processing linting")
    p.fillRect(20, 186, 480, 84, QColor("#241d24"))
    p.setPen(QColor("#98c379"))
    p.drawText(30, 212, "# 改前：每行都挂在 block.userData 上")
    p.setPen(QColor("#abb2bf"))
    p.drawText(30, 238, "class DiffBlockMark(_MarkBase):")
    p.end()
    path = os.path.join(CFG_DIR, "error.png")
    img.save(path, "PNG")
    return path


def fake_chart():
    img = QImage(360, 240, QImage.Format_ARGB32)
    img.fill(QColor("#f7f8fa"))
    p = QPainter(img)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#3b82f6"))
    for i, h in enumerate((60, 110, 84, 150, 128)):
        p.drawRoundedRect(30 + i * 62, 200 - h, 40, h, 4, 4)
    p.setPen(QColor("#4b5563"))
    f = QFont()
    f.setPixelSize(12)
    p.setFont(f)
    p.drawText(20, 22, "每周提交次数")
    p.end()
    path = os.path.join(CFG_DIR, "chart.png")
    img.save(path, "PNG")
    return path


def caption(pix, text, height=34, bg="#1b1f27"):
    img = QImage(pix.width(), pix.height() + height, QImage.Format_ARGB32)
    img.fill(QColor(bg))
    p = QPainter(img)
    p.drawPixmap(0, height, pix)
    f = QFont()
    f.setPixelSize(15)
    f.setBold(True)
    p.setFont(f)
    p.setPen(QColor("#d7dce4"))
    p.drawText(14, height - 11, text)
    p.end()
    return QPixmap.fromImage(img)


def build():
    img1 = fake_screenshot()
    img2 = fake_chart()
    panel = CopilotChatPanel()
    panel.resize(560, 860)
    panel._welcome.setVisible(False)

    # 粘两张图 + 一句提问
    panel._on_image_files([img1, img2])
    panel._input.setPlainText("这个报错什么意思？应该怎么改")
    APP.processEvents()

    # 时间线：用户消息（带图）+ 一段回答
    bubble = panel._add_msg("user", "这个报错什么意思？应该怎么改")
    bubble.set_attachments([img1, img2])
    reply = panel._add_msg("assistant", "")
    reply.end_stream() if hasattr(reply, "end_stream") else None
    reply.append_block(
        "截图里是 **linting** 崩了：`data._selection()` 找不到方法。\n\n"
        "根因是 `DiffBlockMark` 只继承了裸的 `QTextBlockUserData`，"
        "字段抄过去了、方法没抄过来。把基类换成 Spyder 的 "
        "`BlockUserData` 就好，另外 `editor` 不能传 `None` —— "
        "`_selection()` 内部要用它取 document。", kind="result")

    panel.show()
    for _ in range(6):
        APP.processEvents()
    pix = panel.grab()
    panel.hide()
    return pix


def main():
    pix = build()
    whole = caption(pix, "粘贴两张图：composer 出现缩略图，用户消息里也带图")
    whole.save(os.path.join(OUT, "image-attach-panel-dark.png"))

    # composer 特写：底部 ~150px
    crop = pix.copy(QRect(0, max(0, pix.height() - 168), pix.width(), 168))
    crop = crop.scaled(crop.width() * 2, crop.height() * 2,
                       Qt.KeepAspectRatio, Qt.SmoothTransformation)
    close = caption(crop, "特写：✕ 移除 / 点击看大图 / ⌘ 图钉按钮选文件")
    close.save(os.path.join(OUT, "image-attach-composer-dark.png"))
    print("saved",
          os.path.join(OUT, "image-attach-panel-dark.png"),
          whole.width(), "x", whole.height())
    print("saved",
          os.path.join(OUT, "image-attach-composer-dark.png"),
          close.width(), "x", close.height())


if __name__ == "__main__":
    main()
