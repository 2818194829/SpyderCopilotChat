# -*- coding: utf-8 -*-
"""出图：编辑器内联 diff 在真实 CodeEditor 语义下的样子。

用默认 Qt 平台（offscreen 下 Qt 拿不到字体，截出来全是色条），配一个
只补了 get_buffer_block_numbers 的 QPlainTextEdit，装饰走真实的
spyder TextDecorationsManager —— 过滤规则与现场完全一致。
"""
import os
import sys

ROOT = (r"C:\Users\Mr_Yang\Documents\deepseek-harness"
        r"\default-workspace\spyder-copilot-chat")
sys.path.insert(0, ROOT)

from qtpy.QtWidgets import QApplication, QPlainTextEdit      # noqa: E402
from qtpy.QtGui import (QFont, QFontDatabase, QTextCursor,   # noqa: E402
                        QColor, QPalette)
from qtpy.QtCore import Qt                                    # noqa: E402

APP = QApplication.instance() or QApplication([])

from spyder.plugins.editor.utils.decoration import (          # noqa: E402
    TextDecorationsManager)
from spyder_copilot_chat.widgets import theme                 # noqa: E402
from spyder_copilot_chat.widgets.editor_diff import InlineReview  # noqa

OUT = os.path.join(ROOT, "docs", "images")
TMP = os.path.join(os.environ.get("TEMP", OUT), "make_v087_target.py")

OLD = '''import pygame
import random

from settings import WIDTH, HEIGHT

FPS = 60


def draw_tank(screen, tank, color):
    x, y = tank.position
    pygame.draw.rect(screen, color, (x, y, 48, 48))
    gun = pygame.transform.rotate(tank.gun, tank.angle)
    screen.blit(gun, (x + 8, y + 8))


def update_bullets(bullets, dt):
    for b in bullets:
        b.x += b.vx * dt
        b.y += b.vy * dt
        if b.x < 0 or b.x > WIDTH:
            bullets.remove(b)


def main():
    pygame.init()
    screen = pygame.display.set_mode((WIDTH, HEIGHT))
    clock = pygame.time.Clock()
    running = True
    while running:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
        screen.fill((24, 26, 32))
        pygame.display.flip()
        clock.tick(FPS)
'''

NEW = '''import pygame
import random

from settings import WIDTH, HEIGHT

FPS = 60


def draw_tank(screen, tank, color):
    x, y = tank.position
    pygame.draw.rect(screen, color, (x, y, 48, 48))
    gun = pygame.transform.rotate(tank.gun, tank.angle)
    screen.blit(gun, (x + 8, y + 8))
    pygame.draw.circle(screen, (255, 255, 255), (x + 24, y + 24), 3)


def update_bullets(bullets, dt, bounds):
    alive = []
    for b in bullets:
        b.x += b.vx * dt
        b.y += b.vy * dt
        if bounds.collidepoint(b.x, b.y):
            alive.append(b)
        else:
            b.explode()
    bullets[:] = alive


def main():
    pygame.init()
    screen = pygame.display.set_mode((WIDTH, HEIGHT))
    clock = pygame.time.Clock()
    running = True
    while running:
        dt = clock.tick(FPS) / 1000.0
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
        screen.fill((24, 26, 32))
        pygame.display.flip()
'''


class FakeEditor(QPlainTextEdit):
    def __init__(self):
        super().__init__()
        self.decorations = TextDecorationsManager(self)

    def get_buffer_block_numbers(self):
        return (0, 10 ** 6)


def pick_font(size):
    for name in ("Consolas", "Cascadia Mono", "DejaVu Sans Mono",
                 "Courier New"):
        if QFontDatabase().families().count(name):
            font = QFont(name)
            font.setPointSize(size)
            return font
    font = QFont()
    font.setStyleHint(QFont.Monospace)
    font.setPointSize(size)
    return font


def render(name, dark):
    theme.set_current_theme(
        theme.resolve_theme("dark" if dark else "light"))
    with open(TMP, "w", encoding="utf-8") as fh:
        fh.write(OLD)

    ed = FakeEditor()
    ed.resize(880, 560)
    t = theme.current_theme()
    pal = ed.palette()
    pal.setColor(QPalette.Base, QColor(t.get("editor_bg") or t["bg"]))
    pal.setColor(QPalette.Text, QColor(t["text"]))
    pal.setColor(QPalette.Highlight, QColor(t["accent"]))
    ed.setPalette(pal)
    ed.setFont(pick_font(10))
    ed.setPlainText(OLD)
    ed.show()
    for _ in range(4):
        APP.processEvents()

    with open(TMP, "w", encoding="utf-8") as fh:
        fh.write(NEW)

    rev = InlineReview(ed, TMP, OLD, saver=lambda *a: True)
    ok = rev.stage()
    for _ in range(8):
        APP.processEvents()

    # 把光标停在第一处修改上，让 ✓ / ✕ 浮出来（演示悬停态）
    try:
        first = (rev.hunks()[0].del_blocks
                 or rev.hunks()[0].add_blocks)[0]
        ed.setTextCursor(QTextCursor(first))
        ed.centerCursor()
        rect = ed.cursorRect(QTextCursor(first))
        rev._hover_hunk = rev.hunks()[0]
        rev._place_hunk_buttons()
        rev._sync_geometry()
        for _ in range(4):
            APP.processEvents()
        note = "hunk at y=%d" % rect.top()
    except Exception as exc:                                  # noqa: BLE001
        note = "no hunk buttons: %s" % exc

    path = os.path.join(OUT, "inline-editor-%s.png"
                        % ("dark" if dark else "light"))
    ed.grab().save(path)
    print("%s ok=%s deco=%d hunks=%d %s -> %s"
          % (name, ok, rev.deco_count, len(rev.hunks()), note, path))


for dark in (True, False):
    render("dark" if dark else "light", dark)

print("done")
