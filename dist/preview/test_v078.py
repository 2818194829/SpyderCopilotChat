# -*- coding: utf-8 -*-
"""v0.7.8 回归：主题化任务面板 + 现代胶囊 + 单色记号。

覆盖：
  1) QSS 里旧胶囊规则已移除、新规则存在
  2) 双色图标（check_circle）真的有两种颜色
  3) PillButton 的点击 / 空格 / 色调 / 省略 / 换肤
  4) TodoPanel 的行数、状态图标、折叠、字号跟随 ui_font
  5) 工具文案里的 emoji 清理（行首才动，正文不动）
  6) 真面板：胶囊与发送按钮等高、任务面板可见
"""
import os
import sys

ROOT = r"C:\Users\Mr_Yang\Documents\deepseek-harness\default-workspace\spyder-copilot-chat"
sys.path.insert(0, ROOT)

from qtpy.QtCore import QEvent, QPoint, Qt
from qtpy.QtGui import QMouseEvent
from qtpy.QtWidgets import QApplication, QLabel

from spyder_copilot_chat.widgets import theme
from spyder_copilot_chat.widgets.chat_widget import _polish_tool_text
from spyder_copilot_chat.widgets.todo_manager import TodoManager
from spyder_copilot_chat.widgets.todo_panel import TodoPanel

APP = QApplication.instance() or QApplication(sys.argv)
FAILS = []


def ck(name, cond, extra=""):
    print(("  ok  " if cond else "  FAIL") + "  " + name
          + ("" if cond else "  -> %r" % (extra,)))
    if not cond:
        FAILS.append(name)


T = theme.Theme(theme.DARK, "indigo", "compact", 18, 19)
theme.set_current_theme(T)

# ── 1) QSS ──
qss = theme.build_qss(T)
ck("旧 modePill 规则已删除", "modePill" not in qss)
ck("旧 agentPill 规则已删除", "agentPill" not in qss)
ck("新胶囊规则存在", "QFrame#pill" in qss and 'tone="act"' in qss)
ck("任务面板规则存在", "QFrame#todoPanel" in qss and "QLabel#todoItem" in qss)
ck("任务面板不再内联深色底", "#1a1a2e" not in qss)
ck("发送按钮胶囊化", "QPushButton#sendButton" in qss)
ck("字号跟着 ui_font（18px 标题）", "font-size: 18px" in qss)

# ── 2) 双色图标 ──
icon = theme.make_icon("check_circle", T["success"], 18, hole=T["surface"])
img = icon.pixmap(18, 18).toImage()
colors = {}
for y in range(img.height()):
    for x in range(img.width()):
        c = img.pixelColor(x, y)
        if c.alpha() > 200:
            colors[c.name()] = colors.get(c.name(), 0) + 1
ck("check_circle 是双色图标", len(colors) >= 2, sorted(colors)[:4])
ck("check_circle 含实心圆色", T["success"].lower() in colors, sorted(colors))
ck("check_circle 含镂空色", T["surface"].lower() in colors, sorted(colors))
for name in ("list", "circle", "progress", "chevron", "arrow_up", "bolt"):
    pix = theme.make_icon(name, T["text"], 16).pixmap(16, 16)
    ck("图标 %s 可绘制" % name, not pix.isNull() and pix.width() == 16)

# ── 3) PillButton ──
pill = theme.pill("Act", "bolt", "act")
pill.setFixedHeight(30)
pill.show()
APP.processEvents()
ck("胶囊有三个部件", pill.layout().count() == 3, pill.layout().count())
ck("胶囊 tone=act", pill.property("tone") == "act")
ck("胶囊文案", pill.text() == "Act")
clicked = []
pill.clicked.connect(lambda: clicked.append(True))
pos = QPoint(pill.width() // 2, pill.height() // 2)
APP.sendEvent(pill, QMouseEvent(QEvent.MouseButtonRelease, pos, pos,
                                Qt.LeftButton, Qt.LeftButton, Qt.NoModifier))
ck("鼠标点击发信号", clicked == [True], clicked)
from qtpy.QtGui import QKeyEvent
from qtpy.QtCore import QEvent
APP.sendEvent(pill, QKeyEvent(QEvent.KeyPress, Qt.Key_Space, Qt.NoModifier))
ck("空格发信号", len(clicked) == 2, clicked)
pill.set_tone("plan")
ck("换 tone 更新属性", pill.property("tone") == "plan")
pill.set_icon("eye")
pill.setText("Plan")
ck("换图标/文案不崩", pill.text() == "Plan")
pill.setFixedWidth(44)
pill.resize(44, 30)
APP.processEvents()
label = [pill.layout().itemAt(i).widget() for i in range(3)][1]
ck("窄宽度下省略", "…" in label.text() or len(label.text()) <= 4, label.text())

# 换字号重建
T2 = theme.Theme(theme.DARK, "indigo", "compact", 12, 13)
theme.set_current_theme(T2)
pill.refresh_theme()
ck("refresh_theme 后图标缩小", pill.layout().itemAt(0).widget().width() <= 14,
   pill.layout().itemAt(0).widget().width())
theme.set_current_theme(T)
pill.refresh_theme()

# ── 4) TodoPanel ──
TODOS = [
    {"id": 1, "title": "常量与火球美术", "status": "completed"},
    {"id": 2, "title": "Fireball 实体类", "status": "completed"},
    {"id": 3, "title": "只有变大才能顶碎砖块", "status": "completed"},
    {"id": 4, "title": "射击输入与更新/碰撞", "status": "completed"},
    {"id": 5, "title": "绘制与菜单提示", "status": "completed"},
    {"id": 6, "title": "语法检查", "status": "in-progress"},
]
from qtpy.QtWidgets import QVBoxLayout, QWidget
host = QWidget()
theme.named(host, "chatRoot")
theme.apply_theme(host, T)
_v = QVBoxLayout(host)
_v.setContentsMargins(0, 0, 0, 0)
panel = TodoPanel()
_v.addWidget(panel)
host.resize(600, 260)
host.show()
panel._on_todos_changed(TODOS)
APP.processEvents()
ck("任务面板可见", panel.isVisible())
ck("行数 6", panel._items_layout.count() == 6,
   panel._items_layout.count())
ck("计数 5/6", panel._progress_label.text() == "5/6",
   panel._progress_label.text())
ck("进度条 83%", 80 <= panel._progress_bar.value() <= 85,
   panel._progress_bar.value())
titles = [panel._items_layout.itemAt(i).widget()
          .findChildren(QLabel)[1].text() for i in range(6)]
ck("条目文案保留", titles[0] == "常量与火球美术", titles)
item = panel._items_layout.itemAt(0).widget().findChildren(QLabel)[1]
ck("完成条目状态属性", item.property("status") == "completed")
ck("条目 objectName 正确", item.objectName() == "todoItem",
   item.objectName())
ck("条目字号跟随 ui_font（17px）", item.font().pixelSize() == 17,
   item.font().pixelSize())
ck("标题含进行中任务", "语法检查" in panel._title_label.full_text(),
   panel._title_label.full_text())

panel._toggle()
APP.processEvents()
ck("折叠后列表隐藏", not panel._items_widget.isVisible())
ck("折叠后进度条隐藏", not panel._progress_bar.isVisible())
panel._toggle()
APP.processEvents()
ck("展开恢复", panel._items_widget.isVisible() and panel._progress_bar.isVisible())

# 换主题 / 换字号后重建不丢行
theme.apply_theme(host, T2)          # 真面板里由 _apply_theme 重装整棵树
panel.refresh_theme(T2)
APP.processEvents()
ck("换肤后仍是 6 行", panel._items_layout.count() == 6,
   panel._items_layout.count())
item = panel._items_layout.itemAt(0).widget().findChildren(QLabel)[1]
ck("12px 字号下条目字号 11px", item.font().pixelSize() == 11,
   item.font().pixelSize())
theme.apply_theme(host, T)
panel.refresh_theme(T)

panel._on_todos_changed([])
APP.processEvents()
ck("清空列表后隐藏", not panel.isVisible())
ck("空列表不崩", panel._items_layout.count() == 0)
panel.close()

# ── 5) emoji 清理 ──
ck("行首 ✅ → ✓", _polish_tool_text("✅ 文件已创建: a.py") == "✓ 文件已创建: a.py",
   _polish_tool_text("✅ 文件已创建: a.py"))
ck("行首 📋 去掉", _polish_tool_text("📋 任务列表已更新 (6/6)") == "任务列表已更新 (6/6)",
   _polish_tool_text("📋 任务列表已更新 (6/6)"))
ck("keep_mark=False 去掉标记",
   _polish_tool_text("✅ 已重命名: a → b", keep_mark=False) == "已重命名: a → b",
   _polish_tool_text("✅ 已重命名: a → b", keep_mark=False))
ck("正文中的 emoji 不动",
   _polish_tool_text("print('✅ ok')") == "print('✅ ok')")
ck("多行只动行首",
   _polish_tool_text("✅ 第一行\n  缩进 ✅ 保留") == "✓ 第一行\n  缩进 ✅ 保留",
   _polish_tool_text("✅ 第一行\n  缩进 ✅ 保留"))
ck("空串安全", _polish_tool_text("") == "" and _polish_tool_text(None) is None)

# ── 6) 真面板 ──
from spyder_copilot_chat.widgets.chat_widget import CopilotChatPanel
chat = CopilotChatPanel()
chat.resize(900, 520)
chat.show()
APP.processEvents()
TodoManager().set_todos(TODOS)
APP.processEvents()
heights = {
    "mode": chat._mode_btn.height(),
    "agent": chat._agent_btn.height(),
    "send": chat._send_btn.height(),
}
ck("胶囊与发送等高一", len(set(heights.values())) == 1, heights)
ck("高度合理 (>24)", list(heights.values())[0] >= 24, heights)
ck("任务面板可见", chat._todo_panel.isVisible())
ck("模式胶囊文案无 ▾", "▾" not in chat._mode_btn.text(), chat._mode_btn.text())
ck("Agent 胶囊文案无 ▾", "▾" not in chat._agent_btn.text(),
   chat._agent_btn.text())
ck("发送按钮有图标", not chat._send_btn.icon().isNull())
chat._set_mode("plan")
APP.processEvents()
ck("切到 Plan 后胶囊 tone 变化", chat._mode_btn.property("tone") == "plan",
   chat._mode_btn.property("tone"))
ck("Plan 文案", chat._mode_btn.text() == "Plan", chat._mode_btn.text())
chat._set_mode("act")
chat.close()
chat.deleteLater()

print("FAILED: %s" % FAILS if FAILS else "ALL PASS")
