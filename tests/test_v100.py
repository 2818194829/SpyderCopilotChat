# -*- coding: utf-8 -*-
"""v0.8.20 回归：刻度悬停放大 + 清单掠过即选中。

现场：用户截图里用 1 标出右侧刻度、2 标出清单里的某一行，提出两件事：

1. 鼠标靠近刻度 1 应该放大，离开要缩回去；
2. 鼠标悬停在清单第 2 行时，应该能直接在不同行之间选择（WorkBuddy 的
   提问列表就是这个手感），不用先点一下。

v0.8.19 的实现有三处跟不上：

- 刻度 hover 只把 8x4 改成 10x6，在 14px 宽的条上几乎看不出来；
- 清单只连了 ``itemClicked`` / ``itemActivated``，掠过某行没有任何反馈；
- 刻度 ``leaveEvent`` 里立即判断「不在清单上就收起」。指针从刻度横移
  到清单要穿过 2px 缝，这一瞬既不在 rail 也不在清单里，清单会在指针
  底下闪掉——用户根本到不了第 2 行。

改法参考了三类同类实现（Codex 风格消息轨道、DSH 的 chat-rail、以及
WorkBuddy 官方文档对「提问导航」的描述）：以指针所在刻度为中心做梯度
放大、过渡 120ms；清单开 mouse tracking 用 ``itemEntered`` 做悬停选中；
离开后延迟 220ms 再决定收不收（DSH 的 CLOSE_DELAY_MS 是 160ms）。

跑法：``python tests/test_v100.py``（也兼容 pytest）。offscreen 平台。
"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from qtpy.QtCore import QEvent  # noqa: E402
from qtpy.QtWidgets import QApplication  # noqa: E402

_APP = QApplication.instance() or QApplication([])

from spyder_copilot_chat.widgets import chat_widget as CW  # noqa: E402

_results = []


def check(name, condition, extra=""):
    _results.append((name, bool(condition), extra))
    if not condition:
        print("FAIL  {0}  {1}".format(name, extra))


def eq(name, got, want):
    check(name, got == want, "got={0!r} want={1!r}".format(got, want))


def _source(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _rail(n=5, current=0):
    r = CW.MessageRail()
    r.resize(CW.MessageRail.WIDTH, 400)
    r.set_ticks([(i / float(max(1, n - 1)), "{0}. msg".format(i + 1))
                 for i in range(n)], current)
    return r


def _settle(r):
    """把 120ms 的过渡直接推到终态，避免测试依赖真实时钟。"""
    r._hover_anim.setCurrentTime(200)
    _APP.processEvents()


def _fresh(n=5, width=760, height=640):
    p = CW.CopilotChatPanel()
    p.resize(width, height)
    p._config["message_nav_enabled"] = True
    p.show()
    _APP.processEvents()
    for i in range(n):
        p._add_msg("user", "第 {0} 个问题".format(i + 1))
        p._add_msg("assistant", "回答 {0}".format(i + 1))
    for _ in range(6):
        _APP.processEvents()
    p._refresh_rail()
    p._place_rail()
    return p


# ---------------------------------------------------------------------------
# 静态守卫
# ---------------------------------------------------------------------------

def test_static_guards():
    cw = _source("spyder_copilot_chat/widgets/chat_widget.py")
    rail = cw.split("class MessageRail(QFrame):")[1].split(
        "class MessageFlyout")[0]
    fly = cw.split("class MessageFlyout(QFrame):")[1].split(
        "class CopilotChatPanel")[0]
    check("刻度有悬停动画", "QVariantAnimation" in rail
          and "_hover_anim" in rail)
    check("放大尺寸按动画进度插值",
          "_hover_scale" in rail and "int(round(w + (tw - w) * s))" in rail)
    check("最大刻度宽不超出控件",
          "HOVER_W = 14" in rail and "WIDTH = 14" in rail)
    check("清单有 hovered / entered 信号",
          "hovered = Signal(int)" in fly and "entered = Signal()" in fly)
    check("清单开了 mouse tracking",
          "setMouseTracking(True)" in fly
          and "viewport().setMouseTracking(True)" in fly)
    check("清单用 itemEntered 做悬停选中",
          "itemEntered.connect(self._on_item_entered)" in fly)
    panel = cw.split("class CopilotChatPanel(QWidget):")[1]
    check("面板有延迟收起定时器",
          "_flyout_hide_timer" in panel
          and "setInterval(220)" in panel)
    check("刻度与清单间隙收到 2px",
          "rg.left() - size.width() - 2" in panel)
    check("清单 hover 回流到刻度",
          "self._flyout.hovered.connect(self._on_flyout_hovered)" in panel)
    th = _source("spyder_copilot_chat/widgets/theme.py")
    check("列表项有 hover 样式",
          "QListWidget#popupList::item:hover" in th)


# ---------------------------------------------------------------------------
# 刻度：梯度放大、回缩、边界
# ---------------------------------------------------------------------------

def test_rail_hover_scale():
    r = _rail(7, current=0)
    base = r._tick_rects()
    eq("静止时默认刻度 8x4", (base[3].width(), base[3].height()),
       (CW.MessageRail.TICK_W, CW.MessageRail.TICK_H))

    r.set_hover(3)
    _settle(r)
    rects = r._tick_rects()
    eq("悬停项放大到 HOVER 尺寸",
       (rects[3].width(), rects[3].height()),
       (CW.MessageRail.HOVER_W, CW.MessageRail.HOVER_H))
    eq("左侧相邻项次一档",
       (rects[2].width(), rects[2].height()),
       (CW.MessageRail.NEAR_W, CW.MessageRail.NEAR_H))
    eq("右侧相邻项次一档",
       (rects[4].width(), rects[4].height()),
       (CW.MessageRail.NEAR_W, CW.MessageRail.NEAR_H))
    eq("隔一项再降一档",
       (rects[1].width(), rects[1].height()),
       (CW.MessageRail.FAR_W, CW.MessageRail.FAR_H))
    eq("更远的刻度保持默认",
       (rects[6].width(), rects[6].height()),
       (CW.MessageRail.TICK_W, CW.MessageRail.TICK_H))
    check("放大后全部落在控件内",
          all(x.x() >= 0 and x.right() < CW.MessageRail.WIDTH
              for x in rects))

    r.set_hover(-1)
    _settle(r)
    eq("鼠标离开后缩回默认",
       (r._tick_rects()[3].width(), r._tick_rects()[3].height()),
       (CW.MessageRail.TICK_W, CW.MessageRail.TICK_H))

    r.set_hover(0)
    _settle(r)
    cur = r._tick_rects()[0]
    eq("悬停当前项时加宽、不压高度",
       (cur.width(), cur.height()), (CW.MessageRail.HOVER_W,
                                     CW.MessageRail.CUR_H))
    r.deleteLater()


def test_rail_hover_anim_progress():
    r = _rail(5, current=0)
    r.set_hover(3)
    # 刚设置时进度还在 0 附近，不应直接跳到终态
    early = r._tick_rects()[3].width()
    check("放大是渐进的（起点未到终态）",
          early < CW.MessageRail.HOVER_W,
          "early={0}".format(early))
    _settle(r)
    eq("动画结束后到终态", r._tick_rects()[3].width(),
       CW.MessageRail.HOVER_W)
    # 同一索引重复设置不改变状态
    before = r._hover_scale
    r.set_hover(3)
    eq("重复设置同一悬停项不重置", r._hover_scale, before)
    r.deleteLater()


# ---------------------------------------------------------------------------
# 清单：掠过即选中、离开复位、点击仍跳转
# ---------------------------------------------------------------------------

def test_flyout_hover_select():
    f = CW.MessageFlyout()
    f.build([(i, "问题 {0}".format(i + 1)) for i in range(5)], 0)
    got = []
    f.hovered.connect(got.append)

    f._on_item_entered(f._list.item(3))
    check("掠过第 4 行即选中", f._list.item(3).isSelected())
    eq("回传消息序号", got, [3])
    check("其余行未被选中",
          not any(f._list.item(i).isSelected() for i in (0, 1, 2, 4)))

    f._on_item_entered(f._list.item(1))
    check("换一行时选中跟着走",
          f._list.item(1).isSelected()
          and not f._list.item(3).isSelected())

    f.leaveEvent(QEvent(QEvent.Leave))
    eq("离开清单回传 -1", got[-1], -1)
    check("离开后高亮回到视口那条", f._list.item(0).isSelected())

    jumps = []
    f.locate.connect(jumps.append)
    f.setVisible(True)
    f._pick(2)
    eq("点击仍能跳转", jumps, [2])
    check("跳转后清单收起", not f.isVisible())
    f.deleteLater()


def test_flyout_enter_signal():
    f = CW.MessageFlyout()
    f.build([(i, "q{0}".format(i)) for i in range(3)], 0)
    got = []
    f.entered.connect(lambda: got.append("in"))
    f.enterEvent(QEvent(QEvent.Enter))
    eq("进入清单发出 entered", len(got), 1)
    f.deleteLater()


# ---------------------------------------------------------------------------
# 面板：联动、延迟收起、间隙
# ---------------------------------------------------------------------------

def test_panel_link_and_gap():
    p = _fresh(5)
    try:
        p._show_message_flyout()
        rg, fg = p._rail.geometry(), p._flyout.geometry()
        check("清单与刻度几乎贴住（间隙 ≤3px）",
              0 <= rg.left() - fg.right() <= 3,
              "gap={0}".format(rg.left() - fg.right()))

        p._on_flyout_hovered(2)
        eq("掠过清单行 → 对应刻度进入悬停", p._rail._hover, 2)
        p._on_flyout_hovered(-1)
        eq("离开清单 → 刻度悬停清掉", p._rail._hover, -1)
    finally:
        p.close()


def test_panel_delayed_hide():
    from unittest import mock

    p = _fresh(5)
    try:
        p._show_message_flyout()
        p._on_rail_hovered(-1)
        check("移出刻度只是排定收起，不立即收",
              p._flyout.isVisible() and p._flyout_hide_timer.isActive())

        with mock.patch.object(p, "_over_flyout", return_value=False), \
                mock.patch.object(p, "_over_rail", return_value=False):
            p._on_flyout_hide_timeout()
        check("指针不在两块区域时才真正收起", not p._flyout.isVisible())

        p._show_message_flyout()
        p._schedule_flyout_hide()
        with mock.patch.object(p, "_over_flyout", return_value=False), \
                mock.patch.object(p, "_over_rail", return_value=True):
            p._on_flyout_hide_timeout()
        check("指针还在刻度上就不收", p._flyout.isVisible())

        p._schedule_flyout_hide()
        p._cancel_flyout_hide()
        check("重新进入任一块会取消收起",
              not p._flyout_hide_timer.isActive() and p._flyout.isVisible())
    finally:
        p.close()


def test_panel_clear_safe():
    p = _fresh(3)
    try:
        p._show_message_flyout()
        p._clear_ui()
        # 清空后延迟收起定时器可能仍在跑，到点不能摸到半死控件
        p._on_flyout_hide_timeout()
        check("清空会话后收起调用不抛异常", True)
        p._refresh_rail()
        eq("清空后刻度归零", len(p._rail._ticks), 0)
    finally:
        p.close()


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except Exception as exc:  # noqa: BLE001
                check(name + " 未抛异常", False, repr(exc))
    passed = sum(1 for _, ok, _ in _results if ok)
    total = len(_results)
    failures = [(n, e) for n, ok, e in _results if not ok]
    print("\n{0}/{1} passed".format(passed, total))
    if failures:
        print("failures:")
        for name, extra in failures:
            print("  -", name, extra)
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
