# -*- coding: utf-8 -*-
"""v0.7.8 预览：任务面板 + composer 控件行（字号 18/19，与用户实机一致）。

不启动 Spyder、不发网络请求：只把 TodoPanel 与 composer 那几个控件按真实
主题装配起来，离屏 grab 出图。
"""
import os
import sys

ROOT = r"C:\Users\Mr_Yang\Documents\deepseek-harness\default-workspace\spyder-copilot-chat"
sys.path.insert(0, ROOT)
OUT = os.path.join(ROOT, "dist", "preview")

from qtpy.QtCore import Qt
from qtpy.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget,
)

from spyder_copilot_chat.widgets import theme
from spyder_copilot_chat.widgets.todo_panel import TodoPanel

APP = QApplication.instance() or QApplication(sys.argv)

TODOS = [
    {"id": 1, "title": "常量与火球美术", "status": "completed"},
    {"id": 2, "title": "Fireball 实体类", "status": "completed"},
    {"id": 3, "title": "只有变大才能顶碎砖块", "status": "completed"},
    {"id": 4, "title": "射击输入与更新/碰撞", "status": "completed"},
    {"id": 5, "title": "绘制与菜单提示", "status": "completed"},
    {"id": 6, "title": "语法检查", "status": "in-progress"},
]


def build(t, width, todos=TODOS):
    theme.set_current_theme(t)
    root = QWidget()
    theme.named(root, "chatRoot")
    theme.apply_theme(root, t)
    lay = QVBoxLayout(root)
    lay.setContentsMargins(0, 6, 0, 6)
    lay.setSpacing(6)

    panel = TodoPanel()
    panel._on_todos_changed(todos)
    panel.refresh_theme(t)
    lay.addWidget(panel)

    # ── composer 控制行（与 chat_widget 中一致）──
    frame = QFrame()
    theme.named(frame, "composer")
    box = QVBoxLayout(frame)
    box.setContentsMargins(8, 6, 8, 6)
    box.setSpacing(6)

    hint = QLabel("输入消息，或 / 查看命令 · Enter 发送")
    theme.named(hint, "placeholderLabel")
    hint.setStyleSheet(
        "color:%s;font-size:%dpx;background:transparent;" %
        (t["text_faint"], max(11, t.body_font - 2)))
    box.addWidget(hint)

    row = QWidget()
    row.setStyleSheet("background:transparent;")
    ctl = QHBoxLayout(row)
    ctl.setContentsMargins(0, 0, 0, 0)
    ctl.setSpacing(6)

    mode = theme.pill("Act", "bolt", "act")
    agent = theme.pill("默认", "chat", "plain")
    chip = theme.chip("自动")
    ctl.addWidget(mode)
    ctl.addWidget(agent)
    ctl.addWidget(chip)
    ctl.addStretch()
    send = theme.push_button("转向", "primary")
    theme.named(send, "sendButton")
    send.setIcon(theme.make_icon("arrow_up", t["on_accent"], 16))
    ctl.addWidget(send)
    box.addWidget(row)
    lay.addWidget(frame)

    height = max(max(mode.sizeHint().height(), agent.sizeHint().height(),
                     chip.sizeHint().height()), int(t.ui_font) + 12, 24)
    for w in (mode, agent, chip, send):
        w.setFixedHeight(height)

    root.setFixedWidth(width)
    root.adjustSize()
    root.show()
    APP.processEvents()
    root.adjustSize()
    APP.processEvents()
    return root


def shoot(name, palette, width, ui=18, body=19, level="compact", todos=TODOS):
    t = theme.resolve_theme_from_palette(palette, ui, body, level) \
        if hasattr(theme, "resolve_theme_from_palette") else None
    if t is None:
        t = theme.Theme(palette, "indigo", level, ui, body)
    root = build(t, width, todos)
    path = os.path.join(OUT, name)
    root.grab().save(path)
    print("%-30s %dx%d" % (name, root.width(), root.height()))
    root.close()


shoot("v078-dark-todo.png", theme.DARK, 960)
shoot("v078-light-todo.png", theme.LIGHT, 960)
shoot("v078-narrow-todo.png", theme.DARK, 380)
shoot("v078-smallfont-todo.png", theme.DARK, 620, ui=12, body=13)
