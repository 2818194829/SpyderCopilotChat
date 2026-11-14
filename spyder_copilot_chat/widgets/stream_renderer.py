# -*- coding: utf-8 -*-
"""流式 Markdown 增量渲染（0.7.0）

问题：旧实现在流式期间把气泡降级成 ``setPlainText``，用户看到的是一堆
未渲染的 ``**`` 和 ```；等回复结束才整体渲染一次。原因是每来一个 delta
就 ``setHtml`` 全文，等于每帧重跑一次 Markdown 解析 + Pygments 高亮，
长回复直接卡住 GUI。

DeepSeek Harness 的做法是：把消息切成块，已完成块被 React.memo 记住，
流式时只重绘最后一个未完成的块。Qt 端等价实现：

  * ``render_blocks`` 已经带块级 HTML 缓存；
  * 本模块只负责算"这一帧新增了哪些已完成的块"和"尾巴长什么样"；
  * 消费方（MessageBubble）把新增块 append 进 QTextDocument，
    再用尾巴 HTML 覆盖上一次的尾巴。

这样每帧成本 ≈ 尾块解析，且用户从第一个 delta 起就看到排版好的文本。
"""

from .markdown_renderer import render_blocks


class StreamRenderer(object):
    """把不断增长的文本转成 (新增块, 尾块) 两段 HTML。"""

    def __init__(self):
        self._committed = 0     # 已经提交给文档的块数量
        self._tail = ""         # 上一次的尾块 HTML
        self._text = ""

    def reset(self):
        self._committed = 0
        self._tail = ""
        self._text = ""

    @property
    def tail(self):
        return self._tail

    def update(self, text):
        """返回 ``(reset, new_blocks: list[str], tail: str)``。

        ``reset=True`` 表示文本被整体替换（块数倒退），调用方需要先清空
        文档再插入 ``new_blocks``；否则直接把 ``new_blocks`` 追加到末尾，
        并用 ``tail`` 覆盖上一帧的尾块。
        """
        if text == self._text:
            return False, [], self._tail
        self._text = text

        blocks = render_blocks(text)
        closed_count = 0
        for _html, closed in blocks:
            if not closed:
                break
            closed_count += 1

        # 文本被整体替换（重试 / 编辑）时块数会倒退，直接重来
        reset = closed_count < self._committed
        if reset:
            self._committed = 0

        new_blocks = [html for html, _c in blocks[self._committed:closed_count]]
        self._committed = closed_count
        self._tail = "".join(html for html, _c in blocks[closed_count:])
        return reset, new_blocks, self._tail
