# -*- coding: utf-8 -*-
"""出图：同一段对话在 14px / 20px 下的对比，验证字号终于真的能调。

必须用默认 Qt 平台：offscreen 下 Qt 拿不到系统字体，截出来全是色条。
"""
import json
import os
import sys
import tempfile

ROOT = (r"C:\Users\Mr_Yang\Documents\deepseek-harness"
        r"\default-workspace\spyder-copilot-chat")
sys.path.insert(0, ROOT)

from qtpy.QtCore import Qt                                     # noqa: E402
from qtpy.QtGui import QColor, QFont, QImage, QPainter, QPixmap  # noqa: E402
from qtpy.QtWidgets import QApplication                        # noqa: E402

APP = QApplication.instance() or QApplication([])

from spyder_copilot_chat.widgets import settings_dialog as sd  # noqa: E402
from spyder_copilot_chat.widgets.chat_widget import CopilotChatPanel  # noqa

OUT = os.path.join(ROOT, "docs", "images")

CFG_DIR = tempfile.mkdtemp(prefix="v088_img_")
sd.CONFIG_DIR = CFG_DIR
sd.CONFIG_FILE = os.path.join(CFG_DIR, "config.json")
BASE_CFG = {
    "provider": "deepseek", "api_key": "sk-demo",
    "model": "deepseek-flash", "auto_execute": True,
    "ui_theme": "dark", "ui_accent": "spyder", "ui_density": "comfortable",
    "tool_density": "balanced",
}

ANSWER = """坦克的移动改成帧率无关了，`dt` 不再固定成 1/60。

**改动要点**

1. 速度乘上 `dt`，60fps 与 144fps 下移动距离一致；
2. 边界判断改用 `clamp`，避免高速下越过屏幕才回弹；
3. 子弹用列表推导清理，比边遍历边 `remove` 安全。

```python
def update_tank(tank, dt):
    keys = pygame.key.get_pressed()
    direction = int(keys[K_RIGHT]) - int(keys[K_LEFT])
    tank.x = clamp(tank.x + direction * tank.speed * dt, 0, WIDTH)
```
"""

PROCESS = """检查点 `20261114-113530-d82fb`（可用工具栏 ↩ 撤销本步）

▶ run_command `powershell -NoProfile -Command "Remove-Item -LiteralPath
'adventure_island.py','art.py','calculator.py' -Force"`

exit code: 0

✕ run_command 失败：⛔ 安全策略阻止：检测到危险命令（递归删除），已被沙箱拦截
"""


def build(body_px, ui_px):
    with open(sd.CONFIG_FILE, "w", encoding="utf-8") as fh:
        json.dump(dict(BASE_CFG, font_size=body_px, ui_font_size=ui_px), fh)
    panel = CopilotChatPanel()
    panel.resize(760, 880)
    panel._welcome.setVisible(False)
    panel._add_msg("user", "坦克移动速度跟帧率绑在一起了，帮我修一下")
    bubble = panel._add_msg("assistant", ANSWER)
    # 过程面板归 AI 气泡：工具轨迹在正文之上，折叠态只留一行摘要
    bubble.append_block(PROCESS, kind="process")
    panel.show()
    APP.processEvents()
    panel._apply_font_now()
    for _ in range(4):
        APP.processEvents()
    pix = panel.grab()
    panel.hide()
    return pix


def caption(pix, text, height=34):
    """在图上方加一条说明条。"""
    img = QImage(pix.width(), pix.height() + height, QImage.Format_ARGB32)
    img.fill(QColor("#1b1f27"))
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


def main():
    small = build(14, 13)
    large = build(20, 18)

    a = caption(small, "正文 14px（旧默认）")
    b = caption(large, "正文 20px（Aa 菜单 / Ctrl+滚轮）")

    a.save(os.path.join(OUT, "font-size-14-dark.png"))
    b.save(os.path.join(OUT, "font-size-20-dark.png"))

    gap = 18
    comp = QImage(a.width() + b.width() + gap, max(a.height(), b.height()),
                  QImage.Format_ARGB32)
    comp.fill(QColor("#12161c"))
    p = QPainter(comp)
    p.drawPixmap(0, 0, a)
    p.drawPixmap(a.width() + gap, 0, b)
    p.end()
    comp.save(os.path.join(OUT, "font-size-compare-dark.png"))
    print("saved", os.path.join(OUT, "font-size-compare-dark.png"),
          comp.width(), "x", comp.height())


if __name__ == "__main__":
    main()
