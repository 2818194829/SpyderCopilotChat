# -*- coding: utf-8 -*-
"""v0.7.8：把真面板（含 composer / 输入框 / 任务面板）离屏跑起来出图。

不启动 Spyder：只依赖 qtpy + 面板自身的 widget 代码。用户配置会被读取
（字号 18/19、compact 密度、indigo 强调色），正好验证"真机"观感。
"""
import os
import sys

ROOT = r"C:\Users\Mr_Yang\Documents\deepseek-harness\default-workspace\spyder-copilot-chat"
sys.path.insert(0, ROOT)
OUT = os.path.join(ROOT, "dist", "preview")

from qtpy.QtWidgets import QApplication

from spyder_copilot_chat.widgets import theme
from spyder_copilot_chat.widgets.todo_manager import TodoManager

APP = QApplication.instance() or QApplication(sys.argv)

TODOS = [
    {"id": 1, "title": "常量与火球美术", "status": "completed"},
    {"id": 2, "title": "Fireball 实体类", "status": "completed"},
    {"id": 3, "title": "只有变大才能顶碎砖块", "status": "completed"},
    {"id": 4, "title": "射击输入与更新/碰撞", "status": "completed"},
    {"id": 5, "title": "绘制与菜单提示", "status": "completed"},
    {"id": 6, "title": "语法检查", "status": "in-progress"},
]


def shoot(name, width=900, height=560, palette=None):
    from spyder_copilot_chat.widgets.chat_widget import CopilotChatPanel
    panel = CopilotChatPanel()
    if palette is not None:
        cfg = dict(panel._config)
        cfg["ui_theme"] = "light" if palette == theme.LIGHT else "dark"
        panel._config = cfg
        panel._apply_theme()
    panel.resize(width, height)
    panel.show()
    APP.processEvents()
    TodoManager().set_todos(TODOS)
    APP.processEvents()
    panel.resize(width, height)
    APP.processEvents()
    path = os.path.join(OUT, name)
    panel.grab().save(path)
    print("%-28s %dx%d" % (name, panel.width(), panel.height()))
    panel.close()
    panel.deleteLater()


shoot("v078-panel-dark.png")
shoot("v078-panel-light.png", palette=theme.LIGHT)
shoot("v078-panel-narrow.png", width=400, height=560)
