# -*- coding: utf-8 -*-
"""v0.8.4：把真消息气泡离屏跑起来，出一组「展开 / 折叠」对照图。

不启动 Spyder，只依赖 qtpy + 面板自身的 widget 代码，读的是本机真实
主题配置，所以字号、配色和真机一致。
"""
import os
import sys

ROOT = (r"C:\Users\Mr_Yang\Documents\deepseek-harness"
        r"\default-workspace\spyder-copilot-chat")
sys.path.insert(0, ROOT)
# 必须用 windows 平台插件：offscreen 下没有字体库，中文和代码全渲染成色块。
os.environ.pop("QT_QPA_PLATFORM", None)
OUT = os.path.join(ROOT, "docs", "images")

from qtpy.QtWidgets import QApplication        # noqa: E402

from spyder_copilot_chat.widgets import theme  # noqa: E402
from spyder_copilot_chat.widgets.chat_widget import CopilotChatPanel  # noqa: E402

APP = QApplication.instance() or QApplication(sys.argv)

SAY1 = "先确认配置有没有真的打开，再看装饰是不是根本没被推给编辑器。"
TOOL1 = "▸ **read_file** `widgets/editor_diff.py`"
RES1 = ("**✓ read_file** → `_apply_decorations()` 里 add_key 之后没有调用 "
        "`decorations.update()`")
SAY2 = "原因找到了：`_update()` 只保留可见行范围内的装饰，而挂装饰时视图还在第 1 行。"
TOOL2 = "▸ **applyPatch** `widgets/editor_diff.py`"
RES2 = "**✓ applyPatch** → 已写入 12 行，检查点 `cp_8f21`"
FINAL = ("**改好了。** 现在 `stage()` 会先滚到第一个 hunk 再挂装饰，"
         "并且把 `_refresh_decorations()` 接到了滚动与光标事件上，"
         "滚动之后会重新计算可见范围。\n\n"
         "重开一个 200 行以上的文件试一下，红绿行应该能立刻看见了。")


def build(palette=None):
    panel = CopilotChatPanel()
    if palette is not None:
        cfg = dict(panel._config)
        cfg["ui_theme"] = "light" if palette == theme.LIGHT else "dark"
        panel._config = cfg
        panel._apply_theme()
    panel.resize(480, 1000)
    panel.show()
    APP.processEvents()
    bubble = panel._add_msg("assistant", "")
    bubble.append(SAY1)
    bubble.begin_process()
    bubble.add_process(TOOL1)
    bubble.add_process(RES1)
    bubble.append(SAY2)
    bubble.begin_process()
    bubble.add_process(TOOL2)
    bubble.add_process(RES2)
    bubble.append(FINAL)
    APP.processEvents()
    return panel, bubble


def shoot(name, collapsed, palette=None, width=480, height=1320):
    panel, bubble = build(palette)
    if collapsed:
        bubble.finish_process()
    APP.processEvents()
    panel.resize(width, height)
    APP.processEvents()
    # 让气泡重新算一次高度（宽度变了）
    bubble._adjust()
    APP.processEvents()
    path = os.path.join(OUT, name)
    panel.grab().save(path)
    label = "折叠" if collapsed else "展开"
    print("%-30s %-4s %dx%d" % (name, label, panel.width(), panel.height()))
    panel.close()
    panel.deleteLater()


shoot("process-expanded-dark.png", collapsed=False)
shoot("process-collapsed-dark.png", collapsed=True)
shoot("process-expanded-light.png", collapsed=False, palette=theme.LIGHT)
shoot("process-collapsed-light.png", collapsed=True, palette=theme.LIGHT)
print("done")
