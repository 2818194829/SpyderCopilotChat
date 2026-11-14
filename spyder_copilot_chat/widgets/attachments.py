# -*- coding: utf-8 -*-
"""图片附件：粘贴 / 拖入 / 选文件 → 落盘 → 编码成消息 part。

设计取舍
--------
**为什么落盘而不是只在内存里攥着 base64**

  1. 会话历史要能重放。``_messages`` 会被写进会话文件（session JSON）
     和任务快照，塞几十 KB 的 base64 进去，历史文件立刻膨胀到几 MB，
     切一次会话就卡一下。
  2. 消息里只放一个 ``spyder-file://`` 引用，真正的 base64 在
     ``ChatWorker._prepare_messages()`` 里**发请求前**才生成——那是所有
     提供方（OpenAI 兼容 / Copilot / Anthropic）唯一的公共咽喉，改一处
     就全覆盖。
  3. 同一张图重复粘贴会命中同一个 sha1 文件名，天然去重。

**part 的形状**（OpenAI 多模态格式）::

    {"type": "image_url",
     "image_url": {"url": "spyder-file://C:/Users/.../attachments/ab12....png"},
     "name": "Clipboard_Screenshot.png"}

``name`` 是给 UI 用的，发请求前会被剥掉——严格的网关会校验未知字段。
"""

import base64
import hashlib
import os

#: 认得的图片后缀（同时用于拖拽过滤）
IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"})

#: 一次最多带几张，防止手滑拖进整个截图文件夹把上下文撑爆
MAX_IMAGES = 6

#: 单张上限。超过就拒绝并提示，而不是发出去被服务端 413
MAX_IMAGE_BYTES = 8 * 1024 * 1024

#: 消息里表示「本地文件，发请求前编码」的私有 scheme
PART_PREFIX = "spyder-file://"

_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}


def store_dir():
    """附件目录：放在用户 home 下，Spyder 换 conda 环境也不会丢。"""
    path = os.path.join(os.path.expanduser("~"),
                        ".spyder_copilot_chat", "attachments")
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        return os.path.join(os.path.expanduser("~"), ".spyder_copilot_chat")
    return path


def sniff_ext(data):
    """按 magic bytes 判图片类型——扩展名和剪贴板都不给准信。"""
    if not data:
        return None
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:2] == b"BM":
        return ".bmp"
    return None


def is_image_file(path):
    return os.path.splitext(str(path))[1].lower() in IMAGE_EXTS


def save_bytes(data, name_hint=""):
    """把字节写成附件文件，返回 ``(path, error)``。内容相同则复用。"""
    if not data:
        return None, "空数据"
    if len(data) > MAX_IMAGE_BYTES:
        return None, "图片过大（%.1f MB，上限 %d MB）" % (
            len(data) / 1048576.0, MAX_IMAGE_BYTES // 1048576)
    ext = sniff_ext(data)
    if ext is None:
        # 剪贴板有时候给的是带 alpha 的位图但没 magic（极少见），
        # 名字里带后缀就按名字来；再判不出就拒。
        ext = os.path.splitext(name_hint)[1].lower()
        if ext not in IMAGE_EXTS:
            return None, "不是可识别的图片格式"
    digest = hashlib.sha1(data).hexdigest()[:16]
    path = os.path.join(store_dir(), digest + ext)
    if not os.path.exists(path):
        try:
            with open(path, "wb") as fh:
                fh.write(data)
        except OSError as exc:
            return None, "写入附件失败：%s" % (exc,)
    return path, None


def save_qimage(image, name_hint=""):
    """把 ``QImage`` 存成 PNG（剪贴板截图走这条路）。"""
    if image is None or image.isNull():
        return None, "剪贴板里没有图片"
    from qtpy.QtCore import QBuffer, QByteArray, QIODevice

    buf = QBuffer()
    buf.open(QIODevice.WriteOnly)
    image.save(buf, "PNG")
    data = bytes(buf.data())
    buf.close()
    return save_bytes(data, name_hint or "clipboard.png")


def save_file(src):
    """把磁盘上的图片收进附件目录（拖入 / 选文件）。"""
    if not is_image_file(src):
        return None, "不是图片文件"
    try:
        with open(src, "rb") as fh:
            data = fh.read(MAX_IMAGE_BYTES + 1)
    except OSError as exc:
        return None, "读取失败：%s" % (exc,)
    return save_bytes(data, os.path.basename(src))


def to_data_url(path):
    """编码成 ``data:image/png;base64,...``。失败返回 ``(None, 原因)``。"""
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError as exc:
        return None, "附件已丢失：%s" % (os.path.basename(str(path)),)
    ext = str(path).lower().rsplit(".", 1)[-1]
    mime = _MIME.get("." + ext, "image/png")
    return "data:%s;base64,%s" % (
        mime, base64.b64encode(data).decode("ascii")), None


def part_for(path, name=""):
    return {
        "type": "image_url",
        "image_url": {"url": PART_PREFIX + str(path).replace("\\", "/")},
        "name": name or os.path.basename(str(path)),
    }


def path_of(part):
    """从 part 里取本地路径；不是本地引用就返回 None。"""
    if not isinstance(part, dict) or part.get("type") != "image_url":
        return None
    url = (part.get("image_url") or {}).get("url", "")
    if isinstance(url, str) and url.startswith(PART_PREFIX):
        return url[len(PART_PREFIX):]
    return None


def is_local_part(part):
    return path_of(part) is not None


def resolve_parts(content):
    """把消息内容里的本地引用换成 data URL，返回 ``(content, errors)``。

    - 字符串原样返回（不带图的老消息一个字节都不动）
    - 引用解析失败（文件被删）就地换成一段文字说明，而不是让整条请求 400
    """
    if not isinstance(content, list):
        return content, []
    out = []
    errors = []
    for part in content:
        if not isinstance(part, dict):
            out.append(part)
            continue
        path = path_of(part)
        if path is None:
            out.append(part)
            continue
        url, err = to_data_url(path)
        if err is not None:
            errors.append(err)
            out.append({"type": "text", "text": "[图片已失效：%s]" % err})
            continue
        out.append({"type": "image_url", "image_url": {"url": url}})
    return out, errors


def has_images(content):
    if not isinstance(content, list):
        return False
    return any(isinstance(p, dict) and p.get("type") == "image_url"
               for p in content)


def message_text(content):
    """取消息的可读文本：兼容 str 与多模态 list。"""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    chunks = []
    for part in content:
        if isinstance(part, dict) and part.get("type") == "text":
            chunks.append(part.get("text", ""))
    return "\n".join(c for c in chunks if c)


def message_images(content):
    """取消息里所有本地图片路径（用于重放气泡）。"""
    if not isinstance(content, list):
        return []
    return [p for p in (path_of(x) for x in content) if p]


def human_size(num):
    try:
        num = float(num)
    except (TypeError, ValueError):
        return ""
    for unit in ("B", "KB", "MB"):
        if num < 1024 or unit == "MB":
            return ("%d %s" % (num, unit)) if unit == "B" else (
                "%.0f %s" % (num, unit) if num >= 100 else
                "%.1f %s" % (num, unit))
        num /= 1024.0
    return ""


#: 明确不看图的模型家族（前缀匹配）。宁漏勿错——认不出来就不提示，
#: 免得用户明明换了个能看图的代理模型，却被一句「可能不支持」拦住。
_NO_VISION_PREFIXES = (
    "deepseek-chat", "deepseek-reasoner", "deepseek-coder",
    "deepseek-v3", "deepseek-r1",
    "gpt-3.5", "o1-mini", "o1-preview", "o3-mini",
    "qwen-max", "qwen-plus", "qwen-turbo", "qwen2.5-coder",
    "glm-3", "glm-4-air", "yi-large",
)


def vision_hint(model):
    """模型可能不看图时给一句提示；认不出来就返回空串。"""
    name = (model or "").strip().lower()
    if not name:
        return ""
    tail = name.rsplit("/", 1)[-1]
    for prefix in _NO_VISION_PREFIXES:
        if name.startswith(prefix) or tail.startswith(prefix):
            return (
                "⚠️ 当前模型 `%s` 不支持读图，图片会被服务端忽略或直接报错。"
                "请在设置里换成支持视觉的模型（gpt-4o / claude-sonnet / "
                "qwen-vl / glm-4v 等）。" % model)
    return ""
