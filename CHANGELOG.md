# 更新日志

只记录对使用者有影响的变更。最新的在最上面。

## v0.8.11：Stable Edition（内联 diff 把整个 IDE 打崩了）

用户的崩溃栈：

```
lsp_mixin.py:639 in _process_code_analysis     data._selection()
editor_diff.py:157 in __getattr__              raise AttributeError(name)
AttributeError: _selection
  → lsp_mixin.manage_lsp_handle_errors
  → LSPHandleError: Error when processing linting
```

不是 linting 的锅，是我们挂到 `block.userData()` 上的块标记**继承错了基类**。

Spyder 会在每块的行内数据上调 `_selection()` 来画错误下划线：

```python
data = block.userData()
data.selection_start = start
data.selection_end = end
self.highlight_selection("code_analysis_underline",
                         data._selection(), ...)   # ← 这里
```

v0.8.9 的修法只做了「照抄字段」——`DiffBlockMark` 仍直接继承裸 `QTextBlockUserData`，只在 `__init__` 里手动调了一次 Spyder 的 `BlockUserData.__init__`。字段是齐了，**`_selection` 这个方法不在 MRO 里**：属性查找掉进 `__getattr__` 的下划线分支抛 `AttributeError`，Spyder 用 `manage_lsp_handle_errors` 包成 `LSPHandleError`，linting 整条链路炸掉，编辑器跟着瘫。

| 改动 | 说明 |
|---|---|
| 真继承 | 模块级先算出 `_MarkBase`（Spyder 的 `BlockUserData` 在就继承它），`class DiffBlockMark(_MarkBase)`。基类必须在 `class` 语句里写死，运行时补不了 |
| 真 editor | `mark` 现在指向真正的编辑器。`_selection()` 内部要调 `self.editor.document()` / `textCursor()`，传 `None` 是下一颗雷 |
| `__getattr__` 收窄 | dunder 照常抛 `AttributeError`（sip / `copy` 靠它判「没有」）；其它未知的下划线属性返回**可调用兜底**，既不抛异常也不返回 `None`（`None` 会被当方法调用，又是 `'NoneType' object is not callable`） |

新增 `test_v091.py` 32 项：结构守卫（`issubclass` + `_selection` 在 MRO 里）、复刻 `_process_code_analysis` 下划线路径、行号越界、Spyder 各消费点（`is_cell_header` / `document_cells` / `get_current_warnings` / 面板字段）、`__getattr__` 语义。另加 `probe_v091.py` 真机探针：在真 `CodeEditor` 上塞 `_diagnostics` 跑 `finish_code_analysis()`，直接复刻用户那次调用栈。

全量 v077–v091 共 **436 项**全绿。

## v0.8.10：Visible Edition（工具条终于说人话）

编辑器顶上那条紫色小条以前只写「+2 处修改」，旁边是「接受修改 / 撤销」。三个问题一眼能看出来：不知道一共改了几个文件、多文件时「全部接受」按钮会突然冒出来、以及**没有真正的「全部拒绝」**——「撤销」只作用于当前这个文件。

现在长这样：

```
2/3 文件 · +16 −6 · 2 处修改        [接受全部修改]  [不接受本次修改]
```

| 改动 | 说明 |
|---|---|
| 文件计数 | 标签报「现在是第几个 / 一共几个」，切了几次 tab 也知道还剩几个没决定 |
| 两个总开关 | 「接受全部修改」「不接受本次修改」常驻，语义是**对本批改动**一次性决定，不再有隐藏按钮 |
| 快捷键对齐 | `Ctrl+Enter` = 接受全部修改，`Ctrl+Backspace` = 不接受本次修改。此前两个快捷键是文件级的，和按钮语义打架 |
| 宽度感知 | 标签按可用宽度三级降级：全量 → 去掉 `+16 −6` → 只留「2/3 文件」。按钮永远先占位，515px 的窄编辑区也不会把「不接受本次修改」挤出边界 |
| 不再通栏 | 文本短就收窄成悬浮胶囊，不再拉满整个编辑器宽度压住第一行代码 |

顺带修掉一个只有出图才会暴露的布局 bug：判断按钮是否可见用了 `isVisible()`，而工具条是编辑器视口的子控件，首次 `refresh()` 发生在视口 `show()` 之前——那时所有子控件的 `isVisible()` 都是 `False`，标签于是把整条撑到 717px（实测 717 vs 544），把编辑器盖掉一大块。改用 `isHidden()`（只反映「有没有被显式 hide」）。

新增 `test_v090.py` 41 项：单文件/多文件文案、两个总开关的可见性与文字、点「不接受本次修改」后三个文件磁盘内容全部回到修改前、点「接受全部修改」后幽灵行清空、窄视口不越界。全量 v077–v090 共 **404 项**全绿（v077/v078 为出图/主题检查，不计入）。

## v0.8.9：Wired Edition（内联复核其实一次都没接通）

截图里那句 `编辑器内联未生效：文件 — staging 异常：'NoneType' object is not callable`，注意路径是空的——聊天里那个「文件」是空 basename 的兜底文案。路径为空说明异常发生在 `review_files` **循环之前**，跟装饰、可见范围、diff 计算一点关系都没有。

真因是段名遮蔽：

```python
# plugin.py，on_initialize()
self._review_hub = None          # ← 实例字段

# 同一个类里
def _review_hub(self):           # ← 同名方法
    ...
```

实例字典优先于类，所以 `review_files` 里的 `self._review_hub()` 实际在调用 `None`，直接抛 `TypeError`。**自 v0.7.9 引入内联 diff 起，这条链路每一步都断在这里**：红色绿色从来没进过编辑器，异常被外层 `except` 兜成一句「staging 异常」，看起来像装饰没画出来。之前所有关于可见范围过滤、装饰重挂的改动都是对的，但根本没被执行到。

| 改动 | 说明 |
|---|---|
| 方法改名 | `_review_hub()` → `_ensure_review_hub()`；`ReviewHub` 先在局部变量建好再赋给字段，避免中途又被自己遮住 |
| 单文件隔离 | 循环体单独 try。一个文件取不到编辑器不再让整批失败，失败项带上文件名和异常类型 |
| 文案修正 | 路径为空时不再硬凑「文件 — 」前缀，直接给原因 |
| 静态守卫 | 新增全包扫描：**类方法名不得与实例字段同名**。这类遮蔽只有走真插件对象才暴露，单测 `ReviewHub` 永远测不到 |

新增 `test_v089.py` 直接拿 `CopilotChatPlugin.review_files` 去跑（注入假 editorstack / 假 editor），18 项全绿；全量 v077/v079–v089 共 352 项全绿。

## v0.8.8：Readable Edition（字号终于真的能调）

反馈是「字号太小，而且还不能调整字体」。查下来不是一个 bug，是三个叠在一起：

| # | 问题 | 后果 |
|---|---|---|
| 1 | Markdown 的**段落 / 引用 / 列表**没写 `font-size` | 它们回落到 `QTextDocument` 的默认字体。中文 Windows 上是**宋体 9pt（≈12px）**，比设定值小一大截 |
| 2 | `get_provider_config()` 漏了 `ui_theme` / `ui_accent` / `ui_density` / `tool_density` | 「外观」页选了浅色、紧凑，保存后 `_apply_theme` 里 `.get()` 永远拿默认值，一点反应都没有 |
| 3 | 字号入口埋在「设置 → 外观」 | 用户根本找不到；也没有滚轮 / 快捷键 |

第 1 条是「调不动」的真正原因：只有**标题和代码块**写了字号，所以把「正文字体大小」从 14 拉到 32，你会看到代码变大、正文纹丝不动——看起来就像设置失效。

| 改动 | 说明 |
|---|---|
| 段落 / 引用 / 列表补字号 | 现在正文严格等于 `font_size`；列表容器带字号、`<li>` 继承 |
| 文档默认字体兜底 | `MessageView` 每次 `setHtml` 后把文档默认字体对齐主题。`<br>` 空行、流式期间的尾块这些"没写在 HTML 里"的文本也跟着正文走，不会突然掉回 9pt |
| 顶栏 Aa 菜单 | 正文字号档位 12–36、界面字号 ±、字体族（系统 / 雅黑 / 思源 / 等宽）、一键恢复默认。当前档位带勾选 |
| 快捷键 | `Ctrl+=` 放大、`Ctrl+-` 缩小、`Ctrl+0` 还原；焦点在输入框里也生效（`WidgetWithChildrenShortcut`） |
| Ctrl + 滚轮 | 在对话流上滚动一格 = 一档，对标浏览器 / VS Code |
| 配置穿透修正 | 外观四项 + `font_family` 进入 `get_provider_config`，外观页现在真的能换主题 / 密度 |
| 过程面板摘要行 | 原来写死 12px，界面字号调到 18 时它还是 12。改成跟随 `ui_font` |
| 设置页 | 正文字号上限跟 `theme.FONT_STEPS` 对齐（36），新增「字体」下拉 |

调字号只改 `config.json` 的 `font_size` / `ui_font_size` / `font_family` 三个键，并且**不会**把 `get_provider_config()` 派生出来的只读字段（label / chat_endpoint / supports_agent…）写回配置文件。

同一段对话，14px vs 20px：

![字号对比](docs/images/font-size-compare-dark.png)

测试：`dist/preview/test_v088.py` 40/40，覆盖 HTML 带字号、文档默认字体、外观四项穿透、字体族、Aa 菜单档位与勾选、三个快捷键、滚轮两向、落盘不污染配置、已存在消息按新字号重排。

## v0.8.7：Blind Write Edition（用 run_command 写盘也要看得见红绿）

反馈是「改了文件，编辑器里一点颜色都没有，连一句失败提示都没有」。

查审计日志，答案很直白：

```
event=tool_requested  tools=["run_command", "run_command"]
event=tool_result     tool="run_command"  risk="write"
```

弱模型（deepseek-flash 一类）几乎不用 `edit_file`，而是

```bash
python -c "open(r'D:\...\tank_battle.py','w').write(...)"
```

这类命令的参数里**没有 filepath**，`affected_files` 解析为空 ⇒ 写盘前快照为空 ⇒ `_stage_editor_reviews` 直接 `return`。整条预览链路静默哑火，而且因为「没有 items」，连那句「编辑器内联未生效：…」都不会出现——这是最难查的一种失败。

| 改动 | 说明 |
|---|---|
| 命令反推路径 | `affected_files` 新增 `paths_from_command`，从命令文本里抓源文件。**双引号 / 单引号各扫各的**：`python -c "…open(r'D:\a b.py')…"` 这种嵌套写法用 alternation 写成一个正则，双引号会把里面的单引号整段吃掉，结果只能抓到 `b.py` |
| 目录兜底快照 | 一批里只要有 `run_command`，执行前就扫一遍项目源文件的 `(mtime_ns, size)`，事后比对出真正被改的文件。不依赖模型怎么写文件。上限 4000 文件 / 12 MB / 单文件 512 KB |
| 未保存门禁修正 | `stage()` 原来的判据是 `isModified()`。可正常复核时磁盘已经是新内容、buffer 还是旧内容，`isModified` 一置位就永远复核不了。改成判断「buffer 是否还停在改动前的原文」 |
| 最多铺开 8 个文件 | 一次 `run_command` 可能碰到几十个文件，开二十张 tab 比看不到更糟；py 优先，其余只进对话卡片 |
| `/diffdemo` 自检 | 对当前文件做一份**副本**造三处假改动并预览，不动原文件。一条命令就能分清「渲染没生效」还是「触发链路没走到」 |
| 装饰自愈 | 切前台 tab 发生在 `stage()` 返回之后，那一刻可见范围可能还是空的，装饰会被 manager 整个丢掉。350ms 后核对一次 `extraSelections`，没上色就重滚重挂 |

顺带修了两个自己引入的 bug：`_changed_by_scan` 从 `self._review_scan` 读快照，而 `_stage_editor_reviews` 开头已经把它取走清空了——于是永远判定「什么都没改」；`_own_selection_count` 用 `kind` 认亲，可 `extraSelections()` 返回的是 C++ 侧拷贝，挂在上面的自定义属性到那一步早就丢了，只能按光标位置比对。

效果（装饰走真实的 `spyder.TextDecorationsManager`，过滤规则与现场一致）：

![内联 diff 深色](docs/images/inline-editor-dark.png)

测试：`dist/preview/test_v087.py` 30/30，覆盖命令反推（引号 / 无引号 / `--opt=val` / 重定向 / 带行号 / URL 反例）、`include_shell` 开关、目录快照、端到端 items、无改动不造 diff、8 个上限与 py 优先、`isModified` 放宽、`/diffdemo` 只碰副本。

## v0.8.6：Stopped Clock Edition（计时该停就停）

反馈是截图里答案已经写完了，头部还挂着 `▾ 正在处理 1m38s`，秒数一直涨。

根因不是计时器坏了，是**那段过程再也没人收尾**：审批通过后 `_on_approval_completed` 另起了一条气泡继续写答案，旧气泡（带着还在走表的面板）被就地丢掉。它已经不是 `_current_bubble`，`_finalize` 自然收不到它，于是永远停在「正在处理」。

| 改动 | 说明 |
|---|---|
| 停表进 `finally` | `finish_process()` 里渲染 / 排版抛异常也必须停表——一个 Markdown 异常就能让秒数永远涨 |
| 换气泡时就地收尾 | 审批通过、转向这些「另起一条气泡」的分支，先把旧面板定性为已完成 |
| 兜底扫描 | `ProcessPane._live` 记录所有未收尾的面板，`_finalize` / 清空会话时统一停表：漏掉哪个分支都不会留下一直走的钟 |
| 等审批时暂停 | 新增 `pause / resume`：挂起期间文案变成 `▾ 等待批准 · 已用 1m23s`，用户看 diff 的时间不算 Agent 的工时 |
| 计时改累计制 | 「结束时间 − 开始时间」减不出停下来过多久，改成 `累计 + 本次起点`，暂停 / 恢复都精确 |

`finish()` 幂等：重复调用不会把耗时越加越长。

测试：`dist/preview/test_v086.py` 31/31，覆盖正常收尾、幂等、暂停与恢复、换气泡、兜底扫描、清空 / 转向、渲染异常。

## v0.8.5：Reachable Edition（批准按钮必须够得着）

反馈是「批准按钮有些时候按不到」。截图里能看到两种状态：卡片比对话流高时，`批准 / 拒绝` 落在折叠线以下，界面只剩一句「请在上方审批卡中确认」；卡片把内容顶得比视图宽时，底部多出一条横向滚动条，又压掉一截高度。

三处根因，缺一处都会复发：

| 根因 | 改动 |
|---|---|
| 差异区高度是常数 190，不随视图高度变化 | `ApprovalCard.fit_to_height()`：按视图高度实测分配，先压差异、再折差异、最后才允许超出 |
| 长路径把卡片最小宽度顶出视图 | `ElidedLabel` 中间省略路径；对话流禁用横向滚动条 |
| 唯一入口长在卡片底部，滚不到就点不到 | 输入框上方新增固定审批条：数量、`查看`、`全部拒绝`、`批准 N 项` |

快捷条只在挂起期间出现，五条退出路径（决定、取消、转向、新会话、清空会话）统一走 `_set_awaiting_approval(False)`，不会留下一条指向已删卡片的条。

安全语义没有放松：危险操作（删除 / 危险命令）**不参与**批量批准，快捷条这时只显示「还有 N 项危险操作」和 `查看差异`，必须到卡片里看过 diff 再逐项点。用户自己展开过某条差异后，自动收放不再干预。

| 细节 | 说明 |
|---|---|
| 实测而非估算 | 卡片固定开销按布局实际高度算；提示文案在 460px 里会折成三行，常数估算差的几十像素正好够把按钮顶出视野 |
| 滚动按数值算 | `ensureWidgetVisible` 在卡片刚插入时几何还是旧的，会静默不动；改成直接算滚动条位置 |
| 只剩危险项时 | 隐藏批量批准，按钮变成 `查看差异` |

测试：`dist/preview/test_v085.py` 35/35，覆盖省略标签、按高度分配与紧凑降级、危险项语义、快捷条驱动卡片、五条退出路径、横向滚动策略。

## v0.8.4：Fold Edition（过程折起来，只留结论）

一轮 Agent 的工具卡片、结果行、中间叙述原本全部拼进同一个 Markdown 串，等于把日志和答案写在同一张纸上：任务一长，要先滚过几十行参数 JSON 才看得到结论，而且收不起来。

现在每条回复顶部有一行摘要：运行中是 `▾ 正在处理 0m41s`，结束自动收成 `▸ 已完成 5m1s · 6 步`，点一下再展开。

| 改动 | 说明 |
|---|---|
| 新增 `ProcessPane` | 过程折叠面板，运行中展开、结束收起，摘要保留耗时与步数 |
| 正文分两段存储 | `_say` 是尚未定性的尾部叙述：工具调用前归档进面板，流结束时定性为结论 |
| `userOverrode` | 用户手动点过箭头之后，自动策略不再改动折叠状态 |
| 失败不折 | 过程里出现 `✕` / 失败时保持展开，不让报错被折进「已完成」 |
| 工具行压成一行 | `▸ read_file · editor_diff.py`，完整参数仍在审批卡与审计日志里 |

折叠只是最后一步，真正的前提是**归属**：叙述文本后面若还跟着工具调用，它属于过程；这一轮到此为止，它才是结论。只靠 CSS 做不到这件事。

测试：`dist/preview/test_v084.py` 24/24，覆盖归档边界、自动收起、用户接管、失败保持展开、卡片不被误归档、多轮顺序。

## v0.8.3：Visible Range Edition（红绿行终于真的出现了）

真实 Spyder 里既不显示红绿行、也没有按钮条，根因不在渲染代码，而在 Spyder 自身的装饰管线：

- `TextDecorationsManager._update()` 只把 `editor.get_buffer_block_numbers()`（可见行 ± 半屏）范围内的装饰推给 `setExtraSelections()`，范围外直接丢弃
- 而 `_update()` 没有别的触发者：`decorations.update()` 只在增删装饰时被调用，`CodeEditor.update_decorations()` 在整个 Spyder 6.1.8 里没有任何调用点

旧 `stage()` 的顺序正好踩中这个限制：`setPlainText()`（视图回到第 1 行）→ 挂装饰（此时过滤范围是文件开头）→ 才滚到改动处。改动在几百行开外时，所有装饰在过滤阶段就被丢光——文档确实被重写了、幽灵删除行也在，滚过去却是一片素色。

| 改动 | 说明 |
|---|---|
| 调换 stage 顺序 | 先 `_scroll_to_first_hunk()`（改用 `centerCursor` 居中），再挂装饰 |
| 新增 `_refresh_decorations()` | 接到滚动条 `valueChanged`、光标移动、标签页 `Show` / `Resize`，换一批可见行就重算一次 |
| 复核文件切到前台 | 没打开过的文件是 `load(path, set_current=False)` 进入的后台标签页，红绿行和按钮条都画在看不见的那一页里 |

测试：`dist/preview/test_v083.py` 13/13。第一条就是复现——「装饰对象为整篇算出 3 条，但视图在顶部时推给编辑器的 = 0 条」。

## v0.8.2：Diagnostics Edition（内联失败不再静默）

旧版所有失败分支都是 `return False`，上层 `continue`，用户看到的只是「什么都没发生」。这版把每种失败都说出来：

- `InlineReview.fail_reason`：未保存改动 / 读不到新内容 / 无差异 / 写文档异常，四种文案
- `ReviewHub.last_failures`；`plugin.review_files()` 的返回值从 `int` 改成 `{"staged", "failures"}`
- 失败原因带回聊天流直接显示，同时自动回退到对话内红绿卡片
- 工具栏不再等 viewport 的 `Resize` 事件，staging 结束后主动排一拍点亮
- `EditorStack.load()` 的返回值直接用（Spyder 6 本来就返回 editor 实例），只在没有返回值时才回退重扫

测试：`dist/preview/test_v082.py` 22/22，含「用假 manager 跑真实的 `TextDecoration` 路径，确认产出 6 条装饰并挂到 `extraSelections`」。

## v0.8.1：Chat Diff Edition（删行标红、增行标绿，两处都看得见）

Agent 每写一次文件，改动都会被解析成统一的**行模型**（旧行号 / 新行号 /
标记 / 正文 / 词级高亮区间），然后从两个出口渲染：

| 出口 | 形态 | 适合 |
|---|---|---|
| 对话内卡片 | 每个文件一张红绿行表格，像 Cline / Roo Code | 回看、留痕；文件没在编辑器打开也能看到 |
| 编辑器内联 | 真实 buffer 里的幽灵删除行 + 逐处 ✓/✗ | 逐处接受 / 撤销（Cursor 式） |

两种形态都由**写盘前的那份旧文本快照**驱动——Agent 一旦落盘，被删掉的那几
行就再也没有第二处可查，所以快照必须发生在写盘之前。设置里「对话内 diff」
有三档：智能（编辑器已高亮就不重复，默认）/ 总是 / 关闭。

这版还修掉了一个根色问题：深色主题的 diff 底色原本是拿 `success` / `danger`
按 84% 混背景混出来的 `#243b3c` / `#3a3037`——一个是脏青、一个是脏紫，在深蓝
灰编辑器底上根本分不出谁红谁绿。现在红绿各定一组色相明确的底 + 词级加深档：

| 主题 | 删除行 | 新增行 | 词级（删 / 增） |
|---|---|---|---|
| 深色 | `#3d2325` | `#1d3428` | `#5f3033` / `#2f5c42` |
| 浅色 | `#fce3e2` | `#dff3e5` | `#f4b7b4` / `#a6dcb8` |

## v0.8.0：Autonomy Edition（不再跑到 25 轮就停下来）

旧版每批工具调用算一轮，累计 25 轮就强制结束并提示「请回复『继续』」。读
5 个文件、搜 3 次、改 2 个文件就到底——一个正常重构任务必然被切成几段，
与主流代码 Agent 的体验正好相反。v0.8.0 换成三层预算：

| 层级 | 默认值 | 行为 |
|---|---|---|
| 软阈值 | 40 轮 | 只往上下文塞一条「尽快收束」注记，气泡显示一次 🟡，**不中断** |
| 硬预算 | 200 轮（0 = 不限） | 真正兜底，用满后进入自动续跑 |
| 自动续跑 | 5 次 | 自动续期、轮次归零、注入「任务状态未丢失」，继续跑 |

只有真正的死循环才硬停：**写 / 执行类**调用以同样参数连续重复 3 次会停止；
**只读类**重复只警告一次，让模型自己换招（旧版不区分，读文件重试 3 次就把
整个任务掐断）。内置角色（探索 / 调研 / 审查 / 任务 / 规划 / 橡皮鸭）的轮次
上限统一改为 0 = 跟随全局预算——角色边界本来就该由工具集表达，而不是用
8 / 10 / 25 这类小数字硬切。

设置入口：**设置 → Agent → 自主循环预算**。

## v0.7.9：Inline Diff Edition（修改直接标进代码编辑区）

Agent 写盘之后，改动不再只活在聊天面板的审批卡里：文件在编辑器里就地
高亮——新增行整行绿底，被删的原文以「幽灵行」插回原位（红底 + 删除线），
相邻删/增行做词级加深，左侧一条红绿竖纹。决定权也搬进编辑器：

- 视口顶部常驻「接受修改 / 全部接受 / 撤销」
- 悬停到某一处修改，行尾浮出 ✓ / ✗（接受 / 撤销这一处）
- 快捷键对齐 Cursor：Ctrl+Enter 全部接受，Ctrl+Backspace 全部撤销
- 撤销会把旧内容恢复回磁盘（走 Spyder 自己的保存管线），并给模型回灌
  一条「用户已撤销」的事实消息
- 渲染走 Spyder 原生 TextDecoration 管理器，不会被 IDE 的高亮刷新冲掉
- 复核期间拦截 Ctrl+S / Ctrl+Z（幽灵行不该被存进文件，撤销会打散标记）；
  编辑器里有未保存内容时直接放弃接管，不碰用户 buffer
- 设置里可关掉「编辑器内联 diff」，退回聊天面板的纯 diff 审批卡

### 实现笔记：Qt 里没有 zone widget，全靠文档手术

Spyder 没有 VS Code 那套 inline diff 控件，也没有 VS Code 那套
"pending edit" 缓冲区——被删的行在磁盘上已经不存在了，想在编辑器里
标出来只能把它**重新插回去**：

1. **staging**：先在内存里拼出「上下文 + 幽灵行 + 新增行」的最终行序列，
   `setPlainText` 一次性写入，再按行号打 `userData` 标记。边插入边标记
   会因 `QTextBlock` 身份漂移而挂错行。
2. **删块**：必须一次倒序删，并用**字符 position 快照**重定位
   （`QTextBlock` 按 index 引用，块合并会让对象失效或指向别的行），
   再用 `userData` 的 kind 验证没有认错行。
3. **块合并会让 `userData` 归属漂移**：处理完的 hunk 残留标记一律重扫清除，
   否则会话永远不结束。

### PyQt 所有权：四个 C 层崩溃的坑

这一版最花时间的部分，崩溃全部发生在 C++ 层，Python 侧连 traceback
都没有（进程直接以 127 退出）：

| 坑 | 现象 | 修法 |
|---|---|---|
| `QTextBlockUserData` 是 QObject | `setUserData(DiffBlockMark(...))` 的临时对象表达式结束就没了 Python 引用 → PyQt 当场析构 C++ 对象 → 块里留下悬空指针 → 文档销毁时 double free | 把每个 mark 的 Python 引用攥在手里，等块真的从文档消失再放手 |
| event filter 的归属 | 编辑器析构时 Qt 会回调名单里每个 filter 的 `removeEventFilter`；若 filter（review）先被回收，那次回调就是访问野指针 | review 析构时自己先摘 filter |
| `deleteLater()` + 释放 Python 引用 | 前者把销毁排进事件队列，后者让 PyQt 当场析构，队列里那一笔稍后再执行就是野指针 | 清理 widget 只 `hide`，析构交给 GC |
| 改文档前没摘装饰 | `setPlainText` 重建所有 `QTextBlock`，旧装饰里的 `QTextCursor` 立刻悬空 | 任何改文档的动作之前先摘装饰 |

## v0.7.8：Clarity Edition（字号跟上设置，图标换成矢量）

问题不在"字号默认值太小"，而在**有一块界面根本不读设置**：任务面板把
`#1a1a2e`、`12px`、`📋 ✅ 🔄` 全写死在内联 QSS 里。用户把界面字号调到 18px、
正文调到 19px，代码卡片跟着放大了，任务行仍然是 12px 的彩色 emoji——
在满屏 17~19px 的文字里像另一个程序。

| 位置 | 之前 | 现在 |
|---|---|---|
| 任务面板字号 | 写死 12px | 标题 `ui_font`、条目 `ui_font - 1`，行高与图标同步 |
| 状态图标 | 彩色 emoji（📋 ✅ 🔄 ⬜ ❌） | 矢量：实心圆钩 / 细环带点 / 空心圆 / 叉 |
| 胶囊箭头 | 文案里拼字符 `▾` | 真图标（chevron），图标 + 文案 + 箭头三段式 |
| Act 标记 | 无 | 闪电图标；Plan 用眼睛 |
| 发送按钮 | 文字 + 零星图标，比胶囊高一截 | 胶囊 + 向上箭头，与左侧胶囊严格等高 |
| 气泡内状态 | `✅`/`❌`/`⚠️`/`🔧`/`💾` | `✓`/`✕`/`!` 或直接去掉（正文 emoji 不动） |

几处不那么显眼、但决定成败的点：

- **双色图标**：完成的圆钩是"实心圆 + 背景色钩子"，`make_icon()` 新增 `hole`
  参数走第二个画笔。单色描边画不出 GitHub / Cline 那种 check-badge。
- **`▾` 字符不能用**：同一个码位在 Segoe UI 里偏上、在中文字体里偏下，
  18px 下直接贴着胶囊上沿。换成图标后，箭头的位置由布局算，与字号无关。
- **父控件上的无选择器声明会传染给子控件**：容器写
  `setStyleSheet("background: transparent")` 会把卡片背景一起刷掉，
  面板只剩 1px 边框。改用具名规则 `QWidget#todoDock`。
- **`transparent` 依旧是雷**（v0.7.4 的老坑）：行底色一律给显式色值。
- **面板左右留白靠"透明容器 + 内嵌卡片"**，不是给卡片加 margin——
  QFrame 的背景覆盖整个 rect，margin 只会把内容挤进去、底色还是满宽。

## v0.7.7：Diff Edition（删除的行整行标红，新增的整行标绿）

之前的差异预览是「一段 `<pre>` 里套 `<span style="background:...">`」。
Qt 的富文本只给**文字**上色，底色铺不满一整行——看着像荧光笔划过一行字，
而不是 GitHub / VS Code 那种"这一行被标红"。

现在一行就是一个 `<td>`，整行底色交给单元格画，左侧三列分别是旧行号、
新行号、`+/-` 标记：

| 输入 | 表现 |
|---|---|
| ` ```diff ` / ` ```patch ` 围栏 | 直接按补丁渲染，不重算 |
| 没标语言的 `@@` 补丁 | 靠块头自动认出（普通代码里的 `-` 不会被误判） |
| git 的 `--- a/x` + `+++ b/x` | 收成一行文件头，不占正文 |
| Codex 的 V4A（`*** Update File:`） | 同一套渲染；没有 `@@` 就不显示行号 |
| 超过 400 行 | 折叠，末尾标出还剩多少行 |

实现分三层，和 Cline / Roo Code 的思路一致：

1. **表示层**把三种来源归一到「行模型」：`type(+/−/空格) + 旧行号 + 新行号 + 正文`。
2. **计算层**只有拿到 old/new 两段原文时才跑 `difflib`，并把结果格式化成
   unified diff 再喂回同一个行模型——所以补丁和"算出来的差异"走同一条渲染路径。
3. **渲染层**按行类型铺底色，颜色全部取主题 token（换肤时 `recolor_html`
   能整体映射过去）。

几个不那么显眼、但决定成败的点：

- **行号是两个计数器**：`-` 走旧号、`+` 走新号、上下文两边都走。V4A 补丁
  没有绝对行号，此时宁可不显示行号，也不编造——只有 `@@ -a,b +c,d @@`
  或明确的 `*** Add File` 才开始计数。
- **词级高亮**：相邻的 `-`/`+` 块按行配对后再跑一次 `SequenceMatcher`，
  只把真正变了的字符加深一档。否则改一个字符，整行都是重色，眼睛还得自己找。
- **嵌套表格的宽度陷阱**：Qt 不把外层 `width="100%"` 传导给嵌套表格，
  整行底色会缩到内容宽度。所以代码卡片里把行**直接摊在外层表格**里
  （5 列：旧号 / 新号 / 标记 / 正文 / 复制位），而不是再套一层 `<table>`。
- **绝不写 `transparent`**：Qt 会把它当成白色整块刷上去，暗色主题下就是一条
  白带。每一行都给显式色值。
- **按"实际画出的行"报行数**：`---`/`+++` 和 V4A 的 `*** Begin Patch` 都被
  吃掉了，用原文行数会虚高。

顺带修了 `recolor_html` 的"接力"问题：深色背景 `#19232d` 换成浅色 `#fafafa`
之后，浅色主题的正文色恰好也是 `#19232d`，旧实现会沿着替换链把它再改回去。
改成两趟替换（先落占位符，再换新色）。

## v0.7.6：Math Edition（公式终于在气泡里排版出来了）

之前 `$$V = \frac{1}{n} R^{2/3} S^{1/2}$$` 会原样出现在消息里——连反斜杠一起。
原因很直接：**Qt 的富文本没有 MathML，也没有能用的 CSS 数学排版**，
KaTeX / MathJax 那套前端方案搬不过来，WebEngine 又太重。

现在的做法：用 matplotlib 的 **mathtext** 排版引擎在后端把公式排好、光栅化成
PNG，再以 `<img>` 内联进 `QTextDocument`。

| 输入 | 表现 |
|---|---|
| 独占一行的 `$$...$$` | 居中成行，上下各留 6 / 10px |
| 正文里的 `$...$` | 跟随文字，与表格单元格里的符号同样处理 |
| `$100 和 $200` | **不**当公式（首尾都要求非空白、非 `$`） |
| mathtext 排不出来的语法 | 降级成等宽源码，不留白、不吞正文 |
| 超宽公式 | 等比缩到 460px 以内，窄侧栏不横向溢出 |

几个必须踩过的坑：

1. **不能用 `Figure.savefig()`**。这条路上 `transforms.get_affine` 会让进程直接
   崩掉（`Windows fatal exception: code 0xc06d007f`，无 traceback）。改成只取
   `MathTextParser("agg").parse()` 的位图结果——mathtext 自己排版、自己出位图，
   完全不碰 Figure / transforms / patches。
2. **公式必须在 HTML 转义之前摘出来**。`\frac{a}{b}`、`x < y` 一旦被转义成实体，
   mathtext 就读不懂了；所以 `_inline()` 的第一步是 `_stash_math()`，用占位符
   换掉公式，等样式套完再换回图片。
3. **2 倍采样**。按 `dpi = 144` 生成、显示时宽高各除以 2，等价于给公式配了张
   高 DPI 图；字号放大到 17px 也不糊。
4. **颜色跟随主题**。位图本身是灰度 alpha 蒙版，写入磁盘时填成当前主题的正文色，
   背景透明——深浅两套主题各缓存一份，切换主题不串色。
5. **缓存到临时目录**。按 `(公式, 字号, 颜色)` 哈希命名，跨会话复用；流式渲染
   每帧重绘尾块时全部命中（同一公式 6.0ms → 0.0ms）。目录里超过 600 张会
   自动删最旧的一半。

依赖是**可选的**：`pip install "spydercopilotchat[math]"` 会带上 matplotlib；
没装也能用，公式退回等宽源码，其他功能不受影响。

## v0.7.5：Chinese Edition（Agent 选择器说中文了）

界面早就全中文了，只有 composer 里那个 Agent 菜单还挂着一串英文：

```
Default Agent / Explore Agent / Research Agent / Code Review Agent
Task Agent / Plan Agent / Rubber Duck Agent
```

现在改成：

| 数据层 name | 菜单全名 | 胶囊短名 | 说明 |
|---|---|---|---|
| `default` | 默认助手 | 默认 | 不叠加任何预设提示词 |
| `explore` | 探索助手 | 探索 | 快速代码库探索和搜索（快速模型） |
| `research` | 调研助手 | 调研 | 深度代码库调研和分析（最强模型） |
| `code_review` | 代码审查 | 审查 | 审查代码更改，发现问题和改进机会 |
| `task` | 任务执行 | 任务 | 快速运行命令、构建和测试 |
| `plan` | 方案规划 | 规划 | 先调研后实施 — 分析再编码 |
| `rubber_duck` | 橡皮鸭 | 橡皮鸭 | 设计评审和逆向思维伙伴 |

改动点：

- `_BUILTIN_AGENTS` 的 `display_name` 全部换成中文，并新增 `short_name` 字段，
  胶囊不再靠去掉 `" Agent"` 后缀来推短名（`_agent_short_label` 优先读它）
- 各 Agent 系统提示词的自称同步中文化（`你是探索助手（Explore）`），英文名以
  括注保留，方便与文档、日志对齐
- 菜单 tooltip、「点击切换助手」等旁白一并中文
- **数据层不变**：`name` 仍是 `explore` / `code_review` / ...，所以
  `.agent.md`、配置和历史记录都不受影响

## v0.7.4：Markdown Edition（工具卡片之后的回复终于肯渲染了）

实拍里一条 Agent 回复从第一次工具调用之后就"废了"：`🔧 list_directory` 卡片后面
冒出字面的 ` ```合适。 `，再往后所有内容——`**粗体**`、`| 表格 |`、`` `行内代码` ``、
`## 标题`——全部原样吐出来，还是等宽字体。

**根因不在解析器，而在拼接。** 工具卡片是插件以文本形式拼进气泡的：

```python
tool_blocks.append(f"🔧 **{fn}**\n```json\n{args}\n```")   # 末尾没有换行
self._current_bubble.append("\n\n---\n" + "\n".join(tool_blocks))
```

卡片前面补了空行，**末尾没补**。下一段流式正文直接粘在收尾的 ``` 上：

```
{"path": "."}
```合适。          ← 这一行不是合法闭合围栏
```

CommonMark 要求闭合围栏整行只有反引号。于是这个围栏一路吞到消息末尾：后面的
标题、列表、表格全成了代码块正文——用户看到的就是"Markdown 完全不显示"。

三处修复：

| 位置 | 改动 |
|---|---|
| `MessageBubble.append_block()` | 工具卡片 / 状态行 / 检查点这类"整段"内容统一走它，**前后各保证一个空行**；流式 delta 仍走 `append()`，首尾相接 |
| `markdown_renderer._fence_break()` | 围栏容错：行首有足够长的同一字符就算闭合，粘在后面的正文**补回文档流**继续解析；` ```python ` 这种语言标注不算闭合，代码块里的示例围栏不被误伤 |
| `markdown_renderer._style_pre()` | 代码块白底的关键：Qt 把 `<pre>` 上的 `background: transparent` 当成**白色整块刷上去**，盖掉了单元格底色（暗色主题下成了白底 + monokai 浅色字，几乎看不见）。改为删掉 `<pre>` 上的 `background` / `line-height` |

顺手：工具参数按 JSON 缩进展示并限长 700 字符，卡片头从 `Code · 1 行` 变成
`JSON · 3 行`；`create_file` 的 9KB 内容不再挤成一行又被砍半句。

## v0.7.3：Readable Edition（把读不全的下拉框换成胶囊 + 菜单）

实拍截图里，composer 的 Agent 选择器弹出后七个条目全成了
`Def…ent` / `Exp…ent` / `Res…ent` / `Co…nt` / `Tas…ent` / `Pla…ent` / `Rub…ent`，
一条都读不全。查下来是 `QComboBox` 在窄侧栏里的三个硬伤叠在一起：

| 现象 | 根因 |
|---|---|
| 弹出列表只有 78px 宽，条目被中部省略 | popup 是独立窗口，宽度按**控件自身宽度**算，跟最长条目无关 |
| 行高只有 11px，行与行互相压字 | `QComboBoxDelegate` 不认 QSS 的 `::item { padding }`；换自定义 delegate 又会让行矩形与布局步进对不上（实测 height 25 / 步进 17） |
| 权限徽章（"自动"/"按风险"）叠在下拉框上 | `sizePolicy = Ignored` 让 `QHBoxLayout` 把它记成 0 宽，后面的控件就压过来了 |

结论：**不要把这版 QComboBox 塞进窄侧栏**。改成和左边 Plan/Act 同一套交互：

- **胶囊**：`QToolButton#agentPill`，显示短名 + `▾`（Default / Explore /
  Research / Review / Task / Plan / Rubber Duck），tooltip 里是全名与描述
- **菜单**：`QMenu` 尺寸由内容决定，七项全名一次排开、当前项带勾、菜单向上弹
  （composer 贴的是面板底部）
- **对齐**：`_align_composer_row()` 把 Plan/Act 胶囊、Agent 胶囊、权限徽章按三者
  sizeHint 的最大值统一高度（原来是 26 / 22 / 32 三种高度）
- **宽度**：胶囊上限按当前名字的实际长度给，面板窄到 330px 以下才省略，
  否则 "Rubber Duck ▾" 会变成 "Rubb…ck"

## v0.7.2：Inline AI Edition（AI 从侧栏走进代码区）

主流的 AI 编辑器其实有两套完全不同的机制，这一版把它们都补上了：

| | Tab 联想 | Ctrl+K 加注释 |
|---|---|---|
| 本质 | inline completion，只补光标处 | inline chat，整段替换 |
| 上下文 | 光标前后几 KB | 选中块 / 整个函数 |
| 产出 | 灰字，Tab 才写进文档 | diff，点「应用」才写进文档 |
| VS Code | `registerInlineCompletionItemProvider` | `stream.textEdit` + 原地 diff |
| 本版 | `fim/ghost.py` | `widgets/inline_edit.py` |

### Tab 灰字（`fim/ghost.py`）

VS Code 里扩展只返回文本，渲染、合并、Tab 接受全由编辑器内核负责
（`InlineCompletionsModel` → `computeGhostText` → 装饰器）。Qt 富文本没有
"画一段还不存在的文字"的能力，所以这里自己补了三层：

- **请求**：复用已有 FIM provider（Copilot / Ollama / OpenAI 兼容都支持），
  打字停顿 320ms 触发，只送光标前后 3 KB / 0.8 KB 的窗口，不读整份文件。
- **渲染**：在 `CodeEditor.viewport()` 上盖一层透明子控件，`QPainter` 把候选
  画在 `cursorRect()` 之后。灰字**不在文档里**，所以不进撤销栈、不触发
  lint、不影响语法高亮、也不会把光标推走。
- **接受**：`Tab` 插入（包在一个 `beginEditBlock` 里，`Ctrl+Z` 一次撤销），
  `Esc` 丢弃。先于 `CodeEditor.keyPressEvent` 拦截，所以不会吃掉缩进逻辑。

几个必须处理的细节，都是实拍里踩出来的：

- **让位给 Spyder 自带补全**：`is_completion_widget_visible()` 为真时不请求，
  对应 VS Code 的 `Yielding` 状态。
- **候选清洗**：模型偶尔把光标右边的代码再抄一遍，按最长公共前缀裁掉
  （同 `computeGhostText`）；纯空白候选直接丢弃。
- **迟到结果丢弃**：每次请求带 generation，回来时对不上就扔掉，避免旧结果
  覆盖新输入。
- **多行候选只能在空行上展开**：Qt 无法为未插入的文本预留行高，硬画会盖住
  下面已有的代码。所以第一行总是画，后续行只在目标 block 是空行时逐行展开，
  文件末尾之外按行距继续画。
- **窄窗口省略**：用 `QFontMetrics.elidedText` 截断，不会画出边界外。

### Ctrl+K 就地改写（`widgets/inline_edit.py`）

对标 Trae 的 `Cmd+I` / Cursor 的 `Cmd+K`：有选区改选区；光标落在 `def` /
`class` 行上时取整个块（"给这个函数加注释"的直觉）；否则只改当前行。

流程是**先 diff 再落盘**：输入要求 → 后台 `QThread` 请求（GUI 不阻塞）→
`unified_diff` 渲染 → 点「应用」才用 `QTextCursor` 替换（一个编辑块，
`Ctrl+Z` 一次全撤回），点「取消」文档一个字符都没动。

没做成 Trae 那种"就地叠加 diff"，是因为 Qt 没有 zone widget，也没法在文档上
叠加临时视图；先看 diff 再落盘是代价最低、且不会在预览期间把撤销栈弄脏的做法。

> `Ctrl+I` 在 Spyder 里是「inspect current object」，所以这里用 **Ctrl+K**。

### 配置

FIM 默认后端改为 **auto**：聊天用 Copilot 就走 Copilot FIM，否则复用聊天面板
的 `base_url` / `api_key` / `model` 走 OpenAI 兼容接口——不用再单独配一遍。
默认值也按"低延迟"调过：`max_tokens 160`、`temperature 0`、`timeout 6s`。
开关在 `首选项 → 补全与静态检查 → Copilot FIM`。

## v0.7.1：Cursor Context Edition（让面板知道你在看哪一段）

Cline 并不读鼠标坐标：鼠标点击先由编辑器自己转成文本偏移，扩展只读
`window.activeTextEditor.selection`，再把 `(uri, range)` 解析成项目内的相对路径。
Qt 侧完全对应得上，所以这一版把这条链路补上：

| 环节 | Cline（VS Code） | 本插件（Spyder） |
|---|---|---|
| 光标 | `editor.selection` | `codeeditor.textCursor()` |
| 空选区 | 上下各扩 3 行 | 同（`EXPAND_LINES = 3`） |
| 文件 | `document.uri.fsPath` | `finfo.filename` |
| 项目 | `getWorkspaceFolder(uri)` | `Plugins.Projects.get_active_project_path()` |
| 越界 | `path.relative` 以 `..` 开头则丢弃 | `editor_context.relative_to()` 同样返回 `None` |
| 变化通知 | `onDidChangeTextEditorSelection` | 400ms 轮询「文件名+行列+选区+revision」签名 |

行为：

- 输入框上方一颗可点胶囊：`tasks.py:42-58 · 17 行`，点开可切
  **跟随光标 / 仅选中内容 / 整个函数·类 / 整份文件 / 不附加**，写进
  `config.json` 的 `editor_context_mode`，重启保持。
- 发送时在用户消息前注入 `<editor_context file="…" lines="42-58" focus="选中片段">`，
  **只进模型消息，不进气泡**——对话里仍然只显示你打的字。
- 上限 200 行 / 8 KB（整文件 400 行），超出时保留光标附近并标 `truncated="true"`；
  项目外的文件直接不附加；未保存的新文件按 `<untitled>` 附加、标 `saved="false"`。
- 面板不可见时轮询直接返回，编辑器里零开销。

顺手修掉一个一直存在的视觉 bug：Qt 在 `border-radius` 大于控件高度一半时
**不会夹取，而是直接画成直角**。之前 `pill = max(9, ui - 3)` 在 16px 字号下算出
13px，套在 18px 高的 chip 上，「自动」徽章、模型胶囊、上下文胶囊全是方块。
现在按字号推算控件高度再取一半，任何字号下都是真胶囊。

## v0.7.0：Native Render Edition（照着 harness 的渲染栈重做内容层）

先把 DeepSeek Harness（WorkBuddy 5.11 的 Electron 客户端）的聊天面板拆开看了一遍：
`renderer/assets/markdown-renderer-*.js` 里是一套标准的 **unified 双阶段管线**——
`remark-parse + gfm + breaks + math + linkify → rehype-sanitize → rehype-katex → rehype-highlight(lowlight)`，
代码块被 `components.pre` 换成独立组件（表头 + 语言/文件名 + 复制/应用），
Mermaid 在流式期间先显示源码、`complete` 后才切图，整棵树用 `React.memo` 按块缓存。

Qt 富文本没有 KaTeX/Mermaid 引擎，也不支持 `border-radius`，所以能搬的是**结构和排版**，
不是照抄实现：

| harness 的技术 | 本版落地方式 |
|---|---|
| 块级解析与行内解析分离 | `parse_blocks()` 先切块（fence / 标题 / 列表 / 表格 / 引用 / 段落），块内才跑行内正则。旧实现逐行正则，代码块要靠 `%%CODEBLOCK_n%%` 占位符绕开，嵌套列表、任务列表、表格对齐都做不了 |
| GFM 插件集 | 补齐有序列表、二层嵌套、`- [x]` 任务列表、`~~删除线~~`、裸链接自动成链、表格 `:--:` 对齐 |
| 代码块组件（header + actions） | 两行表格：表头「Python · 5 行」+ 右侧「复制」；`<a href="copilot-copy:N">` 由 `MessageView` 接管点击，复制后原地变「已复制」 |
| 排版 token（13/21、标题同级加粗、表格 8/16） | 正文行高 1.62；标题只加粗不放大（h1/h2 仅 +1~2px，去掉下划线大标题）；段间距 8px；表格 5/10 内边距 + 表头底色；引用做成左轴 callout；分隔线压到 1px |
| 块级 memo + 流式渲染 | `StreamRenderer` + `render_blocks()` 的块级 HTML 缓存：流式时只重算"未完成的尾块"，已完成块直接复用。回复**边到边就是排版好的**，不再是纯文本等到最后才刷一次 |
| ErrorBoundary 降级 | 单块渲染异常 → 该块降级为等宽纯文本，不拖垮整条消息 |

实测（离屏，430px 宽，约 400 行含代码的回复）：

| 指标 | 0.6.2 | 0.7.0 |
|---|---|---|
| 流式期间显示 | 纯文本（`**`、``` 原样露出） | 已排版 Markdown |
| 每帧成本 | 纯文本插入 ≈ 0 | ≈5 ms（块缓存命中后） |
| 流式结束与静态渲染高度 | 需整体重绘一次 | **完全一致**（376.0 vs 376.0 px） |
| 长行代码 | `pre-wrap` 折行 | 同左，且表头与代码区同宽 |

顺手修掉一个实拍里一直存在的问题：欢迎卡在第一条消息后不会消失，
整段对话上方永远顶着一张空状态卡片（`_clear_ui()` 里"保留 index 0"的副作用）。

## v0.6.2：Fix Edition（把 0.6.1 的实拍问题逐条修掉）

0.6.1 的配色方向是对的，但装到真实 Spyder 里露出了五个硬伤，这一版逐个修：

| 现象 | 根因 | 修法 |
|---|---|---|
| 暗色 IDE 里整块面板是**白的** | Spyder 6 只给主窗口挂 qdarkstyle **样式表**，从不设置 `QApplication` 调色板，`palette().Window` 永远是 `#efefef` → 亮度判断永远得到"亮色" | `detect_dark()` 改问 Spyder 自己：`spyder.config.gui.is_dark_interface()`，取不到再退回 `CONF.appearance.ui_theme`、样式表特征色、最后才是调色板 |
| 顶栏出现一条**黑条** | `mark` 是个没起名字的 QLabel，`QSizePolicy` 倾向 + 布局无弹簧 → 被拉到 300px 宽；宿主样式表 `QWidget{background:#19232d}` 又把它刷成深色 | 顶栏加 `addStretch()`、图标固定 20px 并命名；QSS 顶部加"宿主样式表隔离"段，把 `QWidget/QLabel/QMenu::item` 的默认值写死 |
| 一条短消息的**气泡吃掉半屏** | `MessageBubble` 默认 `Preferred` 垂直策略，`_inner` 布局把剩余高度分给了它 | 改成 `Maximum`：只能收到 `sizeHint` 高度 |
| 代码块有**横向色带**、长行**溢出** | Pygments 的 `<div class="highlight" style="background">` 被 Qt 按文本块分别上色；`<pre>` 是 `white-space: pre` | 摘掉外层 div、`<pre>` 改 `pre-wrap`；内边距改用表格 `cellpadding`（Qt 会忽略 `<pre>/<div>` 的 padding） |
| 350px 侧栏里**挤爆** | 最小宽度 790px（用量文本 602 + composer 控件 594 + 模型胶囊 274） | 全部改为可压缩 + 省略号；模式胶囊去掉多余菜单箭头；窄面板收起权限徽章与进度条。最小宽度 **790 → 358px** |

顺带完成的界面打磨：

* 顶栏去掉与 dock 标签重复的「Copilot Chat」标题，只留 ✦ 与模型胶囊，右侧是「新对话 / 更多」。
* 溢出菜单重排：图标与文字留 30px 间距、分隔线、hover 用强调色，不再是一列灰条。
* 空状态从"五行说明文字"改成"一句介绍 + 三个可点击起手式"，点一下直接回填输入框。
* 宽面板下对话列收窄到 860px 并居中，正文不再横跨整个侧栏。
* 用量行文案压缩（`5.9k/1.0M · 1% · 本轮 0 · 估算`），恢复任务按钮改为安静的文字按钮。

## v0.6.1：Native Edition（配色与 IDE 同源）

v0.6.0 把界面做轻了，但底色仍是我自己调的一套近黑蓝（`#0f1720`）+ 青碧强调色。
放进 Spyder 里就是两块颜色：IDE 是 `#19232d` 的深蓝灰 + 蓝色高亮，面板是更黑更冷的另一套，
边框一挨着就露出"外挂"感。

Spyder 6 的界面其实由 **qdarkstyle** 绘制（`spyder/config/appearance.py` 里直接引用
`DarkPalette` / `LightPalette`），所以真正的解法不是再调一套深色，而是**把 IDE 的调色板当成唯一色源**：

| 项目 | v0.6.0 | v0.6.1 |
|---|---|---|
| 底色 | 硬编码 `#0f1720`（比 IDE 更黑更冷） | 读 `QPalette.Window/Base`，Spyder 下即 `#19232d`，与编辑器**同色** |
| 中性色阶 | 手调 6 个近似色，越往亮越偏灰 | 由底色按 **HSL 亮度**分级派生，保留深蓝灰色相（`surface` `#1f2c38` → `#273746`…） |
| 正文色 | `#e6edf4`（偏冷白） | 读 `QPalette.Text`，Spyder 下即 `#dfe1e2`，与 IDE 文字一致 |
| 强调色 | 青碧 `#4fd1c5`（绿系，最刺眼的一处） | **Spyder 蓝** `#4fa8ea`；实心按钮另用深一档 `#1a72bb` 保证白字 4.5:1 |
| 语义色 | 高饱和荧光绿 / 红 | 降饱和的 `#5cb98c` / `#dfa948` / `#e8736b` / `#4fa8ea` |
| 换肤 | 只有 `auto` 判断亮/暗 | 跟随自定义主题：底色、正文、高亮色相都从调色板取（色相偏离预设 25° 以上才接管强调色） |
| 审批卡 | 整圈强调色描边 + 绿色「批准」 | 左侧 3px 强调轴 + 中性描边；「批准」改为蓝色主按钮 |
| 差异区高度 | `行数 × 16` 估算，字号一变就空白或截断 | 取文档实际排版高度，随宽度重算 |

强调色预设同时扩到 6 组，默认「Spyder 蓝 · 跟随 IDE」；
其余（青碧 / 靛蓝 / 紫罗兰 / 琥珀 / 石墨）保留给想区分品牌的场景。

## v0.6.0：Interface Edition（界面重构）

### 调研：主流代码 Agent 的界面共识

| 产品 | 观察到的界面决策 | 本版落地 |
|---|---|---|
| Cline 3.x | Plan / Act 在输入框附近；审批卡内联在对话流，diff 绿增红删；Checkpoint 的 Compare / Restore 贴在每个步骤旁 | Plan/Act 移到底部 composer；审批卡扁平化并保留逐项确认、dry-run diff、撤销 |
| Cursor 3.4 | Compact / Balanced / Detailed 三档工具痕迹密度；全屏时聊天变浮动提示词列；Review pill 显示变更数 | `tool_density` 三档；工具/系统消息默认折叠成一行摘要；上下文与用量常驻状态条 |
| GitHub Copilot Chat / VS Code 1.110 | 重做的 model picker；终端工具输出折叠成摘要头；Session Target / Agent / Model / Permissions 集中在输入框；"less visual clutter from tool output" | 顶栏只留品牌 + 模型 chip；次要动作收进溢出菜单；模式 / Agent / 权限 / 发送集中在 composer 控制行 |
| Windsurf Cascade | 深海军蓝 + **单一**生物荧光强调色；pill 按钮、6px 卡片、克制阴影、4px 间距基数 | 语义 token 化；默认青碧强调色，另提供 4 组预设；圆角与间距由密度统一控制 |
| Continue.dev | `chat.background` / `code.block` / `button.primary` 等语义主题 token，亮暗可切换 | `widgets/theme.py` 建立完整 token 表，亮/暗双主题 + 自动跟随 IDE |

### 界面改动

| 位置 | 旧版 | v0.6.0 |
|---|---|---|
| 配色 | 40 多处硬编码 `#1e1e1e / #252526 / #007acc`，只有暗色，与亮色主题 Spyder 冲突 | 语义 token（`bg / surface / text / accent / danger`…）+ 亮/暗双主题 + `auto` 跟随宿主调色板，5 组强调色 |
| 图标 | 彩色 emoji（👤🤖🛡🗺⚙）与 `✕ ↩ ⚡` 文本符号，跨平台不一致 | 26 个自绘矢量线性图标（3× 超采样、统一 1.7px 圆头描边），随主题取色 |
| 顶栏 | 10 个文字/emoji 按钮挤一行，窄侧边栏会溢出 | 品牌 + 模型 chip + 新对话 + 单一溢出菜单（10 项），干净且不会挤爆 |
| 输入区 | 输入框、按钮栏、状态条、上下文提示各自独立成行且各自带边框 | 一张圆角 composer 卡片容纳输入、附件 chip、模式/模型/权限控制与发送按钮；高度随内容自适应 |
| 消息 | 每条消息都带 "You / Copilot / 复制 插入 重试" 全量 chrome，气泡层层嵌套 | 用户消息用强调色左轴卡片；AI 回复为无边框正文；操作按钮改为极淡矢量图标；工具输出默认折叠 |
| 空状态 | 一整段 Markdown 说明文字 | 结构化欢迎卡：品牌、能力 chips、当前模型、快捷键 |
| 审批卡 | 深色方块 + 硬编码色 + emoji 标题 | 语义徽章（只读 / 写入 / 高 / 危险）、文件名摘要、可折叠 diff、按钮层级分明；diff 高度随内容自适应 |
| 代码渲染 | VS Code Dark+ 写死；Pygments 高亮在亮色主题下是一块黑斑；代码块被逐行 Markdown 解析拆散 | Pygments 按主题选 monokai / friendly；背景与文字取 token；代码块整段透传，不再被拆成多个 `<p>` |
| 次要面板 | Todo / 代码审查 / 历史 / 审计 / 内联聊天各写一套深色 QSS | `harmonize()` 统一重着色，独立窗口额外安装整套 QSS |

### 新增设置（设置 → 外观）

`ui_theme`（`auto` / `dark` / `light`，默认自动）、`ui_accent`（青碧 / 靛蓝 / 晴蓝 / 紫罗兰 / 琥珀）、
`ui_density`（紧凑 / 舒适 / 宽松）、`tool_density`（Compact / Balanced / Detailed）。
原「聊天框字体大小 / 界面字体大小」从 Provider 页移到外观页，**逻辑与配置键不变**。
主题、强调色、字号保存后立即重绘（已渲染的 Markdown / diff 会重新着色，不需要重启）；
密度与工具痕迹密度在新建面板时完全生效。

### 兼容性

所有既有信号、方法名与配置键保持不变：P0 的非阻塞渲染 / 验证闭环 / 任务恢复，
P1 的规则 / Skills / 审计 / 沙箱 / 仓库索引 / 分模型，以及 Plan / Act、逐项审批、
检查点撤销、斜杠命令、MCP、Git、内联聊天全部回归通过。

### 验证（2026-11-13）

- 源码编译 + 12 项离屏检查全过：设计 token、26 个图标、颜色映射、Markdown 亮暗渲染、
  遗留面板换肤、面板构造、三段式布局、溢出菜单、composer、主题即时切换、消息折叠、
  模式选择器、审批卡、设置页 6 个 tab。
- 真实 Qt 离屏截图（加载系统字体后）：`dist/preview/ui-dark.png` / `ui-light.png`。
- 修复了旧版代码块被逐行解析、Pygments `.highlight` 背景不随主题、以及窄侧边栏
  文本宽度下限 400px 撑破布局三个渲染缺陷。

## v0.5.6：落地 P1 五项治理能力

### 1. 规则文件 + 渐进式 Skills

- 规则自动加载：`AGENTS.md` / `CLAUDE.md` / `GEMINI.md` / `.clinerules` /
  `.cursorrules` / `.github/copilot-instructions.md`、
  `<项目>/.spydercopilot/rules/*.md`（或 `.clinerules/`、`.claude/rules/`）、
  `~/.spyder_copilot_chat/rules/*.md`。条件规则支持 frontmatter
  `globs:` / `applyTo:`，按当前编辑文件过滤；来源与预算（默认 12000 字符）
  都写在系统提示词里。
- Skills 三层来源：内置 `prompts/skills/`、用户级
  `~/.spyder_copilot_chat/skills/<name>/SKILL.md`、项目级
  `<项目>/.spydercopilot/skills/`。**只有一行描述常驻上下文**，模型命中后
  调 `load_skill` 工具才加载正文；另有 `list_skills` /
  `list_project_rules` / `search_repo_map` 三个只读元工具（Plan 模式也放行）。
- 顺带修复打包缺陷：此前 `pyproject.toml` 没配 `package-data`，wheel 里只有
  `.py`，**安装版的内置 Skills 与 prompt 模板一直是空的**。现补上
  `[tool.setuptools.package-data]`，wheel 内含 17 个 `.md` 资源。

### 2. 结构化审计日志

`~/.spyder_copilot_chat/audit/YYYY-MM-DD.jsonl`，每行一个事件：
`task_start` / `request` / `tool_requested` / `approval_requested` /
`approval_decision` / `blocked` / `tool_result` / `verification` / `usage` /
`redirect` / `cancelled` / `task_done` / `error` / `repo_index`。
写入在独立守护线程 + 队列完成（GUI 零阻塞），自动脱敏 API Key / token。
工具栏「🛡 审计」或「设置 → 治理」可按事件过滤、刷新、导出 CSV、打开目录。

### 3. 风险分级权限与工作区沙箱

| 级别 | 典型动作 | 用户摩擦 |
|---|---|---|
| `read` | 读文件、搜索、符号、元工具 | 免批 |
| `write` | 写文件、补丁、本地命令 | 审批卡片 + diff 预览，可批量批准 |
| `danger` | 删除、委派执行、MCP、`rm -rf`/`git push --force` 等危险命令 | ☠ 标记，**必须逐项点「✓ 批准」**，批量按钮不覆盖 |

- 即使勾选「自动执行工具」或 `approval_policy=none`，**危险操作仍强制逐项确认**。
- 沙箱校验所有路径参数（含 `apply_patch` 的 `+++` 行与批量替换），
  越出项目根即拦截并把原因回灌给模型；可用「额外允许目录」放行。
- 危险命令默认拦截，可在「设置 → 治理」显式放开。

### 4. 仓库结构索引

后台构建「目录树 + 顶层符号」（Python/JS/TS/Go/Rust/Java/C#/Markdown），
缓存到 `~/.spyder_copilot_chat/index/<hash>.json`；主线程**只读内存/磁盘缓存**，
不遍历文件系统。注入系统提示词时受字符预算控制（默认 6000），
模型可用 `search_repo_map` 按文件名/符号检索。工具栏「🗺 索引」可重建。

### 5. Plan / Act 分模型

「设置 → 治理」开启后，Plan 与 Act 可分别配置模型（留空则回退全局模型），
切换模式时自动生效并在顶部标签显示当前模型；每轮请求与审计都记录实际模型。

### 新增设置（设置 → 治理）

`rules_enabled`(开)、`rules_budget`(12000)、`skills_enabled`(开)、
`repo_index_enabled`(开)、`repo_index_budget`(6000)、
`sandbox_enabled`(开)、`allow_dangerous_commands`(关)、
`extra_allowed_roots`(空)、`audit_enabled`(开)、
`mode_models_enabled`(关)、`model_plan`、`model_act`；
审批策略新增 `risk`（推荐）。改动即时生效，无需重启。

> 注意：默认开启沙箱后，读写项目外的绝对路径会被拦截。若确实需要访问
> 其它目录，请加入「额外允许目录」，不要直接关闭沙箱。

## v0.5.5：落地 P0 四项工程底线

调研 2026 年主流代码 Agent（Cline / Roo Code、Cursor、Claude Code、
GitHub Copilot CLI、Aider、OpenHands）后，确认差距不在工具数量而在
harness 质量。本版修复此前「长回复 / 工具执行导致 Spyder 未响应」的根因，
并补齐监督式交互的基础设施。

### 1. 非阻塞渲染与执行

| 位置 | 旧行为 | 新行为 |
|---|---|---|
| SSE delta | 每个 token 发一次跨线程信号 | worker 端按 30ms / 256 字符合并，UI 端再按 30ms 合并 |
| 消息气泡 | 每 token 对全文 `render_markdown()` + `setHtml()`，O(n²) | 流式期间 `QTextCursor` 增量插入纯文本，**结束时只全量渲染一次** |
| 滚动 | 每 token 排一个 10ms 定时器 | 幂等合并，最多一个待处理滚动 |
| 工具执行 | `execute_tool()` 同步跑在 GUI 线程（命令最长 60s） | `QThreadPool` 后台批量执行，结果经 QueuedConnection 回主线程 |
| 检查点 / 历史写盘 | 主线程同步文件 IO | 一并移入后台线程 |
| MCP `stdout.readline()` | 无超时，可永久阻塞 | 守护线程 + 30s 有界等待 |

### 2. 验证闭环（写后自检、失败回灌）

批次里出现写操作后，自动在后台对改动过的 `.py` 文件执行
`python -m py_compile`（可在设置里换成 `pytest -q` 等自定义命令，
`{files}` 会替换为改动文件）。结果以一条结构化消息回灌给模型：
失败时附带完整 stdout/stderr，下一轮据此自行修复；成功时要求直接总结。
单个任务最多验证 2 次，避免验证—修复死循环。

### 3. 中断 / 转向 / 任务恢复

- 运行期间输入框**保持可用**；输入新指令再发送即中断当前响应并就地转向，
  旧 worker 断流、后台工具批次收到取消标志，已完成的上下文全部保留。
- 「取消」不再只是置标志：会 flush 缓冲、结束流式渲染，并把任务写为
  `interrupted` 快照。
- 快照落在 `~/.spyder_copilot_chat/tasks/active_task.json`（原子替换写），
  含消息列表、轮次、模式、用量、项目根目录。重启 Spyder 后工具栏出现
  「恢复任务」，点击即可载入上下文继续；正常完成或新对话会清除快照。

### 4. 上下文 / token / 成本状态条

输入框上方常驻一行：上下文占用率（进度条）、本轮 token、累计费用。
若服务端在流里返回 `usage` 则用精确值并标注「精确」，否则按字符估算
（中文 1 字 ≈ 1 token，其余 ≈ 3.6 字符 1 token）并标注「估算」。
费用默认不显示，需在设置里填「元/百万 token」单价。

### 新增设置（设置 → Agent）

`stream_throttle_ms`(30)、`verify_on_write`(开)、`verify_command`(空)、
`verify_max_attempts`(2)、`context_window`(DeepSeek 1M / 其它 128K)、
`price_input_per_million`、`price_output_per_million`、
`task_persistence`(开)。改动即时生效，无需重启。

### 验证结果（2026-11-13）

- 源码编译、离屏导入、设置对话框与面板构造 + 事件循环、`find_external_plugins()`
  与两个 entry point 均为 0.5.5。
- 渲染：300 次 `append` 只触发 **1 次** 全量 `render_markdown`；100 个 SSE delta
  合并成 2 个跨线程信号（原本 100 个）。
- 线程：`submit` 回调确认回到 GUI 线程；MCP 沉默服务器按超时返回；工具批次、
  检查点快照与取消标志正常。
- 验证闭环：`py_compile` 正/反例均通过；写操作触发后台验证，失败结果以
  `user` 消息回灌后才启动下一轮（不会提前请求）。
- 状态与 UI：任务快照 save/load/clear/resumable、设置页新控件保存、运行中转向。
- 真实 DeepSeek 流式调用：HTTP 200，正文 `OK`，并收到服务端 `usage`
  （prompt 259 / completion 1 / total 260 tokens）——状态条自动显示「精确」。
- 测试产生的临时任务快照已清理，不会误触发「恢复任务」。

## v0.5.4：按 DeepSeek 官方文档重设模型配置

依据 <https://api-docs.deepseek.com/zh-cn/>（2026-11-13 核对）及实测
`GET /models`，插件里的旧模型名已失效，配置整体更新如下：

| 项目 | 旧值 | 新值（官方文档） |
|---|---|---|
| model | `deepseek-chat` | `deepseek-flash`（DeepSeek-V4.1-Flash-1017） |
| 可选模型 | `deepseek-chat` / `deepseek-reasoner` | `deepseek-flash` / `deepseek-v4-pro` |
| API 地址 | 空（靠默认值兜底） | `https://api.deepseek.com` |
| 对话端点 | `/v1/chat/completions` | `/chat/completions`（`/v1` 仍兼容） |
| temperature | 0.7 | 0.0（官方：代码生成/数学解题推荐 0.0；默认 1.0） |
| max_tokens | 4096 | 8192（官方非思考模式默认 8K；上限 393216） |
| 思考模式 | 未设置 → 服务端默认开启 | 显式 `thinking=disabled` |

官方要点：两个模型均为 **1M 上下文 / 最大输出 384K**，支持 Tool Calls、
JSON Output、Responses API；`deepseek-flash` 另支持图像理解。思考模式默认
开启（effort `high`），可用 `thinking: {"type": "enabled|disabled"}` 与
`reasoning_effort: low|high|max` 控制；**思考模式下 temperature 不生效**，
且携带 `tools` 的请求必须在后续轮次完整回传 `reasoning_content`（否则 400）。
因此本插件默认显式关闭思考模式，保证 temperature 生效、Agent 工具调用与
FIM 补全稳定；可在「设置 → Provider → 思考模式」自行开启，开启后输出预算
自动提升到官方默认 64K，避免思维链吃光 `max_tokens` 导致回答为空。

FIM 补全按官方文档同步：模型 `deepseek-flash`，仅**非思考模式**支持，官方
原生端点是 `https://api.deepseek.com/beta`、最大补全 4K。本插件仍以
`/chat/completions` + 填空提示词实现，并自动带上 `thinking=disabled`。

`~/.spyder_copilot_chat/config.json` 已同步写入上述配置（API Key 原样保留）。
实测 `deepseek-chat` / `deepseek-reasoner` 目前仍能返回 200，属于兼容别名，
但官方模型列表与 API 参考已不再列出，也不对应当前能力/价格，故不再使用。
实测 `POST /chat/completions` 与 `/v1/chat/completions` 均可访问，插件统一用前者。

## v0.5.3 修复：首选项 TypeError（真正的根因）

现象：打开「工具 → 首选项」时 Spyder 抛

```
File "...\spyder\widgets\config.py", line 310, in load_from_conf
    spinbox.setValue(self.get_option(option, default, section=sec))
TypeError: setValue(self, val: int): argument 1 has unexpected type 'float'
```

**这才是前两次崩溃的真正根因**，v0.5.2 里判定「根因在 Spyder、不在插件代码」
的结论不准确，这里更正。完整因果链：

1. 插件的 FIM 配置页里 `fim_timeout` 用 `step=1`（int）创建控件；Spyder 的
   `create_spinbox` **只按 `step` 的 Python 类型**选控件：int → `QSpinBox`
   （只接受 int），float → `QDoubleSpinBox`。
2. 但 `CONF_DEFAULTS` 和用户 `spyder.ini` 的
   `provider_configuration['copilot_fim_provider']` 里 `fim_timeout` 存的是
   **float `8.0`**。`load_from_conf` 把该值直接交给 `QSpinBox.setValue` →
   `TypeError`。
3. 异常发生在 `create_dialog` 内部，使对话框创建**中途断裂**：`finished`
   回调没挂上、`container.dialog` 留下半初始化引用 → 下次打开首选项就在
   `self.dialog.resize(...)` 报 v0.5.1 时看到的
   `RuntimeError: wrapped C/C++ object of type ConfigDialog has been deleted`。

所以两个报错是同一个问题的两副面孔。v0.5.2 的兜底剥掉了第一层（悬空引用），
藏在后面的 `TypeError` 才在 `guarded_create_dialog` 栈里暴露出来。

v0.5.3 做三层修复：

| 层 | 修复 |
|---|---|
| 治本 | FIM 配置页 `fim_timeout` 改用 `default=8.0 / min=1.0 / max=30.0 / step=0.5`（float）→ 生成 `QDoubleSpinBox`，与存储类型一致 |
| 通用兜底 | 插件加载时对 `QSpinBox.setValue` 做一次幂等包装：**仅当传入 float 时取整**，其余原样透传；覆盖 Spyder 自带配置页、其它 provider 和未来新增项，避免同类问题再次打断首选项创建；插件卸载时恢复 |
| 已有兜底 | 保留 v0.5.2 的对话框悬空引用防御（打开前检测 / 创建失败清空 / finished 置空） |

不修改任何 Spyder 安装目录文件（该目录对本账户只读）。
`QDoubleSpinBox` 不是 `QSpinBox` 的子类，兜底不会影响浮点控件。

## v0.5.2 修复：Spyder 首选项对话框悬空引用

现象：打开「工具 → 首选项」时 Spyder 抛
`RuntimeError: wrapped C/C++ object of type ConfigDialog has been deleted`
（栈在 `preferences/widgets/container.py:88` 的 `self.dialog.resize(...)`）。

`ConfigDialog` 带 `WA_DeleteOnClose`，容器只靠 `finished` 回调里的
`self.dialog.disconnect(); self.dialog = None` 清理引用；回调一旦没走完，
`self.dialog` 就成了指向已销毁 C++ 对象的悬空包装，下次打开首选项必崩。

> 注：当时依据「traceback 中没有 `spyder_copilot_chat` 帧」判定根因纯粹在
> Spyder 侧。v0.5.3 证实这一层只是**后果**：中断 `create_dialog` 的异常来自
> 插件 FIM 配置页的 `fim_timeout` 类型不匹配（见上节）。此处的防御仍然必要，
> 它保证任何配置页异常都不会留下悬空引用引发二次崩溃。

Spyder 安装目录的 ACL 只允许 Users 读取，无法修补其源码，因此插件在
`on_preferences_available` 时对 `PreferencesContainer.create_dialog` 做防御包装：

| 防御 | 行为 |
|---|---|
| 打开前检测 | 访问 `dialog.isVisible()`，抛 `RuntimeError` 就把 `container.dialog` 置空，让 Spyder 重建对话框 |
| 创建失败兜底 | `create_dialog` 抛异常时清空 `container.dialog`，避免半初始化引用残留到下一次 |
| 关闭兜底 | 额外连接一个 `finished` 回调把 `container.dialog` 置空，不依赖 Spyder 自己的 `disconnect()` 是否成功 |

不修改 Spyder 任何文件；插件禁用后行为完全恢复原状。
若当前 Spyder 已经处于悬空状态，**重启一次 Spyder** 即可清空该运行时状态。

## v0.5.1 修复

| 问题 | 修复 |
|---|---|
| `__init__` 调用不存在的 `_start_models_refresh_if_needed`，面板一实例化就抛 `AttributeError`，Spyder 里表现为「插件没装 / 面板打不开」 | 补上安全实现：仅 Copilot provider 才后台拉模型，异常只记日志；补齐 `_on_models_loaded` / `_on_models_failed` 回调 |
| `HookEngine.__init__` 调用不存在的 `_load_hooks` | 改为调用实际存在的 `load_hooks()` |
| 安装路径被误判 | 核实 Spyder 自身环境目录是只读 ACL，并非插件没装；重新正规打包安装到 Spyder 加载的 user site，并清理旧 dist-info，保证只有一个 0.5.1 元数据 |
| 验证不足 | 增加离屏构造面板 + 事件循环 + 设置页 + 插件发现 + 审批卡片 / 检查点的实测 |

## v0.5.0 更新：Cline 式工作流

调研 Cline 后落地的核心差异（旧版是「批量弹窗确认 + 直接落盘」）：

| 改进 | 说明 |
|---|---|
| **Plan / Act 双模式** | 工具栏一键切换。Plan 模式用白名单只暴露读/搜索/规划工具，系统提示词明确禁止写盘与执行命令；切回 Act 才恢复全部工具 |
| **内联审批卡片** | 替换整批 `QMessageBox`。卡片插在对话流里，展示工具名、风险等级、文件路径和 unified diff（新增行绿、删除行红），可逐项「批准 / 拒绝」或「全部批准 / 全部拒绝」 |
| **写入前 dry-run diff** | 审批阶段只在内存里计算 `create_file` / `edit_file` / `insert_edit` / `apply_patch` / `delete_file` 的新内容，不落盘 |
| **检查点与撤销** | 执行前快照受影响文件（`~/.spyder_copilot_chat/checkpoints/`），审批卡片出现「↩ 撤销本步更改」，工具栏也有全局 ↩ |
| **异步挂起** | 审批期间显式挂起 Agent 循环，不嵌套事件循环、不与 QThread 信号重入冲突；部分批准时只把批准的工具写入消息历史，保持 OpenAI tool_calls 协议一致 |
| **可配置** | 设置 → Agent：默认模式、审批策略（writes / all / none）、检查点开关；兼容旧 `auto_execute` |
| **修复** | 移除 0.4.1 开发分支里 `_stream_timer.timeout.connect(self._flush_stream)` 指向不存在方法的死代码（会导致面板实例化抛 AttributeError） |
