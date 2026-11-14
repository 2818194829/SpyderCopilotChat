# -*- coding: utf-8 -*-
"""v0.8.19 回归：消息清单浮层与右侧刻度条垂直居中对齐。

现场：用户截图里用红框分别圈出「悬停展开的两行消息清单」和「右侧两个
刻度点」，再用箭头连起来——两块东西一个飘在面板顶部、一个在面板中部，
视觉上完全脱节，要求垂直居中对齐。

真因不是浮层算错了尺寸，而是**两块用了不同的垂直基准**：

- ``MessageRail._tick_rects()`` 把整组刻度居中排布（``start =
  max(2, (h - total) // 2)``），所以 2 条消息时两个刻度紧挨在导航条
  垂直中心附近；
- ``_show_message_flyout()`` 却写死 ``y = max(vg.top() + 4, rg.top() - 4)``，
  即贴导航条**顶端**。导航条几乎和视口一样高（v0.8.17 实拍里 806px），
  于是浮层中心比刻度中心高了 363px，正好复现用户截图。

修复：新增纯算术的 ``_flyout_y()``，以导航条垂直中心为准，再夹进滚动区
可见范围（清单比视口还高时优先保住上沿）。抽成静态方法而不是写死在
定位函数里，是为了让这些边界能被直接钉住。

跑法：``python tests/test_v099.py``（也兼容 pytest）。offscreen 平台，
不需要真实字体与显示服务。
"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

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


def _fresh(n=2, width=760, height=640):
    """干净的面板：show 过才有真实 viewport 几何，定位才算得准。"""
    p = CW.CopilotChatPanel()
    p.resize(width, height)
    p._config["message_nav_enabled"] = True
    p.show()
    _APP.processEvents()
    for i in range(n):
        p._add_msg("user", "第 {0} 个问题".format(i + 1))
        p._add_msg("assistant", "回答 {0}\n\n第二段补充说明，撑开一点高度。"
                   .format(i + 1))
    for _ in range(6):
        _APP.processEvents()
    p._refresh_rail()
    p._place_rail()
    return p


# ---------------------------------------------------------------------------
# 静态守卫：定位口径不能再退回「贴导航条顶端」
# ---------------------------------------------------------------------------

def test_static_guards():
    src = _source("spyder_copilot_chat/widgets/chat_widget.py")
    seg = src.split("def _show_message_flyout(")[1].split(
        "def _on_rail_hovered(")[0]
    check("定位改走 _flyout_y", "self._flyout_y(" in seg)
    check("旧的贴顶写法已移除",
          "max(vg.top() + 4, rg.top() - 4)" not in seg)
    # v0.8.20 把间隙从 6px 收到 2px，指针横移进清单时不再"跨空"
    check("仍贴导航条左缘", "rg.left() - size.width() - 2" in seg)
    check("仍限制高度不超滚动区",
          "min(size.height(), max(120, vg.height() - 10))" in seg)
    check("_flyout_y 是静态方法", "@staticmethod" in seg
          and "def _flyout_y(" in seg)
    rail = src.split("class MessageRail(QFrame):")[1].split(
        "class MessageFlyout")[0]
    check("刻度整组居中排布（对齐基准的由来）",
          "(h - total) // 2" in rail)


# ---------------------------------------------------------------------------
# 纯算术：居中、夹边界、极端尺寸
# ---------------------------------------------------------------------------

def test_centered_arithmetic():
    f = CW.CopilotChatPanel._flyout_y
    # v0.8.19 实拍里的真实几何：rail=(44,806), scroll≈(40,820)
    y = f(44, 806, 40, 820, 88)
    eq("实拍几何下浮层 y", y, 403)
    eq("浮层中心 = 导航条中心", y + 88 // 2, 44 + 806 // 2)
    old = max(40 + 4, 44 - 4)
    check("旧逻辑确实偏上 300+ px", y - old > 300,
          "new={0} old={1}".format(y, old))

    eq("奇数高度也居中（误差 ≤1）",
       abs((f(44, 806, 40, 820, 87) + 87 / 2.0)
           - (44 + 806 // 2)) <= 1, True)
    eq("导航条不在视口正中时仍以导航条为准",
       f(100, 400, 40, 820, 100), 250)


def test_clamp_to_viewport():
    f = CW.CopilotChatPanel._flyout_y
    # 上沿：导航条整体贴着视口顶部 → y 算成负数，夹到 view_top+4
    eq("上沿夹住", f(0, 40, 40, 820, 88), 44)
    eq("刚好不触发上沿", f(44, 100, 40, 820, 88), 50)
    # 下沿：导航条中心低于视口底部 → 夹到 view_bottom-h-4
    eq("下沿夹住", f(900, 100, 40, 820, 88), 768)
    eq("刚好不触发下沿", f(700, 100, 40, 820, 88), 706)
    # 清单比视口还高：hi < lo，宁可裁掉尾部也保住上沿
    eq("超高清单优先保上沿", f(44, 806, 40, 820, 900), 44)
    eq("视口极矮也不越界", f(44, 806, 40, 100, 88), 48)
    # 一堆组合里都不越出可见区
    for rail_top in (0, 44, 300, 700):
        for h in (40, 88, 200, 430):
            y = f(rail_top, 806, 40, 820, h)
            check("组合不越界 rail_top={0} h={1}".format(rail_top, h),
                  y >= 44 and y + h <= 40 + 820 - 4,
                  "y={0}".format(y))


# ---------------------------------------------------------------------------
# 真实面板：几何、高亮复用、开关与清空后重建
# ---------------------------------------------------------------------------

def test_panel_flyout_is_centered():
    p = _fresh(2)
    try:
        p._show_message_flyout()
        rg = p._rail.geometry()
        fg = p._flyout.geometry()
        vg = p._scroll.geometry()
        check("清单可见", p._flyout.isVisible())
        eq("两条消息两个刻度", len(p._rail._ticks), 2)
        check("浮层中心对齐导航条中心",
              abs(fg.center().y() - rg.center().y()) <= 1,
              "flyout={0} rail={1}".format(fg.center().y(),
                                           rg.center().y()))
        check("浮层在导航条左侧", fg.right() <= rg.left(),
              "{0} vs {1}".format(fg.right(), rg.left()))
        check("浮层完整落在滚动区内",
              fg.top() >= vg.top() + 4
              and fg.bottom() <= vg.top() + vg.height() - 4,
              "fg={0} vg={1}".format((fg.top(), fg.bottom()),
                                     (vg.top(), vg.height())))
        # 刻度组本身确实围绕导航条中心，居中对齐才有意义
        rects = p._rail._tick_rects()
        mid = (rects[0].center().y() + rects[-1].center().y()) / 2.0
        check("刻度组围绕导航条中心",
              abs(mid - rg.height() / 2.0) <= 30,
              "mid={0} half={1}".format(mid, rg.height() / 2.0))
    finally:
        p.close()


def test_panel_sizes_and_reuse():
    for n, height in ((2, 640), (5, 900), (8, 420)):
        p = _fresh(n, height=height)
        try:
            p._show_message_flyout()
            rg, fg = p._rail.geometry(), p._flyout.geometry()
            check("{0} 条 / 高 {1} 仍居中".format(n, height),
                  abs(fg.center().y() - rg.center().y()) <= 1,
                  "{0} vs {1}".format(fg.center().y(), rg.center().y()))
            check("{0} 条 / 高 {1} 不越面板".format(n, height),
                  fg.left() >= 0 and fg.right() <= p.width() + 1
                  and fg.top() >= 0 and fg.bottom() <= p.height() + 1)

            item0 = p._flyout._list.item(0)
            before = p._flyout.geometry()
            p._show_message_flyout(highlight=min(1, n - 1))
            check("{0} 条时换高亮不重建列表".format(n),
                  p._flyout._list.item(0) is item0)
            eq("{0} 条时换高亮不挪位置".format(n),
               p._flyout.geometry(), before)
            check("{0} 条时对应行被选中".format(n),
                  p._flyout._list.item(min(1, n - 1)).isSelected())

            p._hide_message_flyout()
            p._show_message_flyout()
            check("{0} 条收起后重建仍居中".format(n),
                  abs(p._flyout.geometry().center().y()
                      - p._rail.geometry().center().y()) <= 1)
        finally:
            p.close()


def test_nav_disabled_and_cleared():
    p = _fresh(3)
    try:
        p._config["message_nav_enabled"] = False
        p._refresh_rail()
        check("关掉开关后导航条隐藏", not p._rail.isVisible())
        p._config["message_nav_enabled"] = True
        p._refresh_rail()
        check("重新打开后导航条回来", p._rail.isVisible())
        p._clear_ui()
        p._refresh_rail()
        eq("清空会话后刻度清空", len(p._rail._ticks), 0)
        p._show_message_flyout()
        check("空会话不弹清单", not p._flyout.isVisible())
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
