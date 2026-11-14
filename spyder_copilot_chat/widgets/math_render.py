# -*- coding: utf-8 -*-
"""LaTeX 数学公式渲染 —— mathtext 排版 → PNG → QTextDocument 的 <img>。

Qt 的富文本既没有 MathML 也没有 CSS 定位，KaTeX 这类前端方案搬不过来；
WebEngine 又太重（Spyder 里还要额外装组件）。这里用 matplotlib 的 mathtext
排版引擎把公式光栅化成图片，再内联进消息：

- **只走 ``MathTextParser("agg")`` 的位图结果**，不碰 Figure / transforms。
  实测在 Spyder 的运行时里 ``fig.savefig()`` 会在 ``transforms.get_affine``
  崩掉（Windows fatal exception 0xc06d007f），而单纯解析 mathtext 没问题。
- 按 2 倍采样生成、显示时缩回 1/2，等价于给公式配了张高 DPI 图，
  在 Spyder 的高分屏缩放与放大字号下都不糊。
- 按 (公式, 字号, 颜色) 缓存 PNG 到临时目录，跨会话复用；同一公式在流式
  渲染的每一帧里都命中缓存，不会重复排版。
- 任何一步失败（mathtext 不支持的语法、缺字体、matplotlib 不可用）都返回
  None，由调用方降级成等宽原文 —— 公式画不出来也不能把整条消息拖黑。
"""

import hashlib
import logging
import os
import re
import tempfile

logger = logging.getLogger(__name__)

# 生成倍率：2 倍图，显示尺寸 = 像素尺寸 / 2
_SCALE = 2
# 单条消息里最多渲染多少个公式，超出的降级成原文，避免一条消息把人卡住
MAX_FORMULAS = 60

_parser = None
_available = None
_failures = {}          # tex -> True（同一个坏公式不再重试）
_cache = {}             # key -> (url, w, h)
_cache_dir = None
_count = 0

# mathtext 不认、但模型爱写的命令：做最小替换，别让整条公式报废
_PREPROCESS = (
    (re.compile(r"\\d?frac(?![a-zA-Z])"), r"\\frac"),
    (re.compile(r"\\(?:displaystyle|textstyle|scriptstyle)\b\s*"), ""),
    (re.compile(r"\\tag\*?\{[^{}]*\}"), ""),
    (re.compile(r"\\label\{[^{}]*\}"), ""),
    (re.compile(r"\\(?:big|Big|bigg|Bigg)[lrm]?\b"), ""),
    (re.compile(r"\\nonumber\b"), ""),
    (re.compile(r"\\hspace\*?\{[^{}]*\}"), r"\\;"),
    (re.compile(r"\\vspace\*?\{[^{}]*\}"), ""),
    (re.compile(r"\\operatorname\*?\{([^{}]*)\}"), r"\\mathrm{\1}"),
)


def _norm(tex):
    """公式源码规范化：去首尾、合并空白、替换 mathtext 不认的命令。"""
    s = (tex or "").strip()
    if not s:
        return ""
    s = re.sub(r"\s*\n\s*", " ", s)
    s = re.sub(r"[ \t]{2,}", " ", s)
    for pat, repl in _PREPROCESS:
        s = pat.sub(repl, s)
    return s.strip()


def _ensure():
    """懒加载 mathtext；导入失败就永久降级（不反复抛异常）。"""
    global _parser, _available
    if _available is not None:
        return _available
    try:
        from matplotlib.font_manager import FontProperties  # noqa: F401
        from matplotlib.mathtext import MathTextParser
        _parser = MathTextParser("agg")
        _available = True
    except Exception as exc:      # pragma: no cover - 环境缺依赖时
        logger.info("mathtext 不可用，公式将显示为源码: %s", exc)
        _parser = None
        _available = False
    return _available


def available():
    """公式渲染是否可用（matplotlib 是否装得上）。"""
    return _ensure()


def _cache_path(key):
    global _cache_dir
    if _cache_dir is None:
        _cache_dir = os.path.join(tempfile.gettempdir(),
                                  "spyder-copilot-math")
        try:
            os.makedirs(_cache_dir, exist_ok=True)
        except Exception:         # pragma: no cover - 只读临时目录
            _cache_dir = tempfile.gettempdir()
        _trim_cache(_cache_dir)
    return os.path.join(_cache_dir, key + ".png")


def _trim_cache(folder, limit=600):
    """顺手控一下临时目录的体量：换主题/改字号都会生成新图。"""
    try:
        files = [os.path.join(folder, f) for f in os.listdir(folder)
                 if f.endswith(".png")]
        if len(files) <= limit:
            return
        files.sort(key=lambda f: os.path.getmtime(f))
        for f in files[: len(files) - limit // 2]:
            try:
                os.remove(f)
            except OSError:
                pass
    except Exception:             # pragma: no cover - 清理失败不影响渲染
        pass


def _as_url(path):
    """绝对路径 → QTextDocument 能加载的 file: URL（中文路径也要能开）。"""
    p = path.replace(os.sep, "/")
    if not p.startswith("/"):
        p = "/" + p
    return "file://" + p.replace(" ", "%20")


def _write_png(path, alpha, color):
    """把 mathtext 的灰度位图写成「指定颜色 + 透明底」的 PNG。"""
    import numpy as np

    h, w = alpha.shape
    rgba = np.zeros((h, w, 4), dtype=np.uint8)
    rgb = tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))
    rgba[:, :, 0] = rgb[0]
    rgba[:, :, 1] = rgb[1]
    rgba[:, :, 2] = rgb[2]
    rgba[:, :, 3] = alpha
    try:
        from PIL import Image
        Image.fromarray(rgba, "RGBA").save(path, "PNG", optimize=True)
        return True
    except Exception:
        pass
    # 没有 Pillow 就用 Qt 自己写（Spyder 里 Qt 一定在）
    try:
        from qtpy.QtGui import QImage
        buf = rgba.copy()
        img = QImage(buf.data, w, h, 4 * w, QImage.Format_RGBA8888)
        return bool(img.copy().save(path, "PNG"))
    except Exception:             # pragma: no cover
        return False


def _fit(w, h, max_width):
    """逻辑尺寸，超过 max_width 就等比缩。"""
    if max_width and w > max_width:
        ratio = max_width / float(w)
        return max(1, int(max_width)), max(1, int(round(h * ratio)))
    return max(1, int(round(w))), max(1, int(round(h)))


def render(tex, size_px, color, max_width=None):
    """公式 → ``(file_url, width, height)``；失败返回 None。

    ``size_px`` 是正文字号（像素），公式按同一视觉字号排版；
    ``color`` 是 ``#rrggbb``，跟随主题的正文色。
    """
    global _count
    expr = _norm(tex)
    if not expr:
        return None
    if not _ensure():
        return None
    if _failures.get(expr):
        return None
    if _count >= MAX_FORMULAS:
        return None

    key = hashlib.sha1(
        ("%s|%d|%s|%d" % (expr, size_px, color, _SCALE)).encode("utf-8")
    ).hexdigest()[:20]
    hit = _cache.get(key)
    if hit is not None:
        return hit

    path = _cache_path(key)
    w_px = h_px = 0
    if os.path.exists(path):
        try:
            from PIL import Image
            with Image.open(path) as im:
                w_px, h_px = im.size
        except Exception:
            w_px = h_px = 0
        if not (w_px and h_px):
            try:
                from qtpy.QtGui import QImage
                q = QImage(path)
                if not q.isNull():
                    w_px, h_px = q.width(), q.height()
            except Exception:
                w_px = h_px = 0

    if not (w_px and h_px):
        try:
            import numpy as np
            from matplotlib.font_manager import FontProperties
            _ox, _oy, _w, _h, _d, image = _parser.parse(
                "$%s$" % expr, 72 * _SCALE,
                FontProperties(size=size_px), antialiased=True)
            alpha = np.asarray(image)
            if alpha.ndim != 2 or not alpha.size:
                raise ValueError("empty bitmap")
            h_px, w_px = alpha.shape
            if not _write_png(path, alpha, color):
                raise IOError("png write failed")
        except Exception as exc:
            _failures[expr] = True
            logger.info("公式渲染失败，降级为原文: %r (%s)", expr[:60], exc)
            return None

    _count += 1
    # 逻辑尺寸 = 像素 / 倍率；除不尽时向上取整，避免右侧被裁一条
    lw, lh = _fit(w_px / float(_SCALE), h_px / float(_SCALE), max_width)
    url = _as_url(path)
    hit = (url, lw, lh)
    if len(_cache) > 400:
        _cache.clear()
    _cache[key] = hit
    return hit


def img_html(tex, size_px, color, display=False, max_width=None):
    """公式 → ``<img>`` 片段；失败返回 None（调用方降级成等宽原文）。"""
    res = render(tex, size_px, color, max_width=max_width)
    if res is None:
        return None
    url, w, h = res
    align = "middle"
    return ('<img src="%s" width="%d" height="%d" align="%s">'
            % (url, w, h, align))


def reset_count():
    """每条消息开始渲染前调一次：重新计算公式配额。"""
    global _count
    _count = 0


def clear_cache():
    """卸载/换主题时清掉进程内缓存（磁盘缓存保留，跨会话可用）。"""
    _cache.clear()
