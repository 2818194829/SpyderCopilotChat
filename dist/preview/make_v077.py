# -*- coding: utf-8 -*-
"""v0.7.7 预览：把 diff 卡片真刀真枪地渲进 QLabel，出图。"""
import os
import sys

ROOT = r"C:\Users\Mr_Yang\Documents\deepseek-harness\default-workspace\spyder-copilot-chat"
sys.path.insert(0, ROOT)
OUT = os.path.join(ROOT, "dist", "preview")

from qtpy.QtCore import Qt
from qtpy.QtWidgets import QApplication, QLabel

from spyder_copilot_chat.widgets import markdown_renderer as md
from spyder_copilot_chat.widgets import theme

TEXT = """改好了，一共动了三处：总价算法带上数量、结果做两位小数、顺手补了空行。

```diff
--- a/calc.py
+++ b/calc.py
@@ -12,7 +12,8 @@ def total(items):
     s = 0
     for i in items:
-        s += i.price
+        s += i.price * i.qty
+        s = round(s, 2)
     return s
```

工具参数里的 apply_patch 补丁走同一套渲染（没有 `@@` 就不显示行号，不猜）：

```
*** Begin Patch
*** Update File: src/app.py
@@ def greet():
-    print("Hi")
+    print("Hello, world!")
*** Add File: notes.txt
+第一行
+第二行
*** End Patch
```

普通代码块保持不变，作为对照：

```python
def total(items):
    return round(sum(i.price * i.qty for i in items), 2)
```
"""

APP = QApplication.instance() or QApplication(sys.argv)


def shoot(name, palette, width):
    t = theme.Theme(palette)
    theme.set_current_theme(t)
    body = md.render_markdown(TEXT)
    label = QLabel()
    label.setTextFormat(Qt.RichText)
    label.setWordWrap(True)
    label.setText(body)
    label.setFixedWidth(width)
    label.setStyleSheet(
        "background:%s;color:%s;padding:14px 16px;" % (palette["bg"], palette["text"]))
    label.adjustSize()
    label.show()
    APP.processEvents()
    label.adjustSize()
    APP.processEvents()
    path = os.path.join(OUT, name)
    label.grab().save(path)
    print("%-28s %dx%d" % (name, label.width(), label.height()))
    label.close()


shoot("v077-dark-diff.png", theme.DARK, 620)
shoot("v077-light-diff.png", theme.LIGHT, 620)
shoot("v077-narrow-diff.png", theme.DARK, 380)
shoot("v077-tiny-diff.png", theme.DARK, 300)
