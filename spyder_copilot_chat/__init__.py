"""
Spyder Copilot Chat — GitHub Copilot Chat 完整移植到 Spyder 6
基于 vscode-copilot-chat (Microsoft) v0.48.1 的设计理念移植

功能:
  - AI 聊天面板（多 Provider + 流式输出）
  - Agent 模式（工具调用 + MCP 集成）
  - 子 Agent 系统（searchSubagent / executionSubagent / Fleet）
  - Git 工具集（getChangedFiles / git_add / git_commit / 代码审查）
  - 补丁应用（applyPatch / insertEdit）
  - Notebook 操作（创建/编辑/删除单元格）
  - Skills 技能系统（14 个内置技能）
  - 自定义指令（.instructions.md）
  - 自定义 Agent（.agent.md）
  - MCP 服务器集成
  - Plan 规划模式
  - FIM 代码补全
  - 内联聊天
  - 斜杠命令（/fix, /explain, /tests, /plan, /review 等）
  - 聊天历史管理 + 记忆系统

v0.5.0 "Cline Edition" — 参照 VS Code Cline 重构交互：
  - Plan / Act 双模式开关（Plan 只暴露只读工具）
  - 写文件 / 执行命令前的内联审批卡片（逐项批准、unified diff 绿增红删）
  - 执行前检查点快照，可一键撤销本步更改
  - 非模态、异步挂起 Agent 循环，不再用批量 QMessageBox

v0.5.5 "Responsive Edition" — 落地专业代码 Agent 的 P0 工程底线：
  - 双层流式节流 + 增量文本插入，结束时只做一次全量 Markdown 渲染
  - 工具 / 检查点 / 验证 / 历史写盘全部移出 GUI 线程；MCP 读取有超时
  - 写操作后自动验证并把失败结果回灌给模型迭代
  - 运行中可随时输入新指令转向，任务快照可跨会话恢复
  - 上下文占用、token 与成本状态条

v0.5.6 "Governance Edition" — 落地专业代码 Agent 的 P1 工程能力：
  - 规则文件（AGENTS.md / CLAUDE.md / .clinerules / 条件规则）+ 渐进式 Skills
  - 结构化 JSONL 审计日志（工具轨迹 / 审批 / 验证 / 用量 / 拦截）
  - 风险分级权限 + 工作区沙箱（危险操作强制逐项确认）
  - 仓库结构索引（后台构建、按需检索）+ Plan/Act 分模型

v0.6.0 "Interface Edition" — 参照 2026 年主流代码 Agent 重做界面：
  - 语义化设计系统（widgets/theme.py）：亮/暗双主题、自动跟随 IDE、单一强调色
  - 26 个自绘矢量线性图标，替换彩色 emoji 与系统符号
  - 三段式布局：紧凑顶栏 + 低噪音对话流 + 底部统一 composer
  - 次要动作收进溢出菜单，模型/模式/权限/发送集中在输入区
  - 工具痕迹默认折叠、密度可调；消息气泡与审批卡扁平化
  - 结构化空状态欢迎卡；Markdown / diff / Pygments 高亮随主题重绘

v0.6.1 "Native Edition" — 配色与 IDE 同源，不再是"贴上去的一块深色"；
v0.6.2 "Fix Edition" — 修掉 0.6.1 实拍里暴露的一批硬伤：
  - 暗色 IDE 下整块面板变白板：Spyder 只挂 qdarkstyle **样式表**、不设调色板，
    按 palette 亮度判断永远得到"亮色"。改用 spyder.config.gui.is_dark_interface()
  - 顶栏出现一条黑条：未命名的 QLabel 被布局拉伸，又被宿主样式表刷成 #19232d
  - 气泡吃掉半屏：QFrame 的 Preferred 策略让外层布局把剩余高度分给了气泡
  - 代码块横向色带 / 溢出：Pygments 的外层 div 被 Qt 逐块上色，<pre> 不折行
  - 顶栏与输入区在窄侧栏里挤爆：最小宽度从 790px 降到 358px，次要控件按宽度收起

v0.7.0 "Native Render Edition" — 参照 DeepSeek Harness 聊天面板重写渲染管线：
  - 块级解析（fence / 标题 / 列表 / 表格 / 引用 / 段落）与行内解析彻底分离，
    代码块不再靠占位符绕开正则，嵌套列表、任务列表、表格对齐都能正确输出
  - GFM 能力补齐：有序列表、嵌套列表、- [x] 任务列表、~~删除线~~、裸链接自动成链
  - 代码块卡片化：表头「语言 · N 行」+ 右侧逐块「复制」，复制后原地变「已复制」
  - 流式增量渲染：回复边到边排版（不再是纯文本），已完成块走 HTML 缓存，
    长回复帧成本从全量重排降到 ~5ms；结束再做一次全量渲染保证与静态一致
  - 排版 token 对齐 harness：正文行高 1.62、标题与正文同级加粗、段间距 8px、
    表格 5/10 内边距 + 表头底色、引用做成左轴 callout、分隔线压到 1px

v0.7.1 "Cursor Context Edition" — 对标 Cline，让面板知道"你正指着哪段代码"：
  - 读取当前编辑器的 QTextCursor：空选区时上下各扩 3 行（同 Cline 的
    CONTEXT_LINES_TO_EXPAND），有选区就用选区
  - 输入框上方新增可点胶囊：跟随光标 / 仅选区 / 整个函数·类 / 整份文件 / 不附加，
    选择写进 config.json 的 editor_context_mode，重启保持
  - 发送时注入 <editor_context file=… lines=…>：只进模型消息，不进气泡
  - 边界与截断：项目外路径直接丢弃（同 Cline 的 path.relative 检查），
    单次上限 200 行 / 8KB（整文件 400 行），超长时保留光标附近并标注 truncated
  - 未保存的新文件按 <untitled> 附加，只给编辑器里的内容、不给磁盘路径
  - 顺带修掉胶囊圆角：Qt 在 border-radius > 控件高度一半时会直接画成直角，
    大字号下所有 chip（含"自动"/徽章）其实一直是方块；改为按字号推高度取半

v0.7.2 "Inline AI Edition" — 把 AI 搬进代码编辑区（不再只活在侧栏）：
  - Tab 灰字补全（对标 VS Code InlineCompletionItemProvider / Trae Tab）：
    打字停顿 320ms → 取光标前后窗口 → FIM 请求 → 灰字画在光标后。
    灰字**不进文档**，因此不进撤销栈、不影响语法高亮与 lint
  - 接受用 Tab（一个 beginEditBlock，Ctrl+Z 整段撤销），Esc 丢弃；
    与 Spyder 自带补全弹窗互斥（弹窗可见时不请求，同 VS Code 的 Yielding）
  - 候选清洗：裁掉与光标后文本重复的前缀、去首尾空行、限 12 行 / 1200 字符；
    迟到结果按 generation 丢弃，不会覆盖新输入
  - 多行候选只在下方是空行时逐行展开（Qt 无法为未插入的文本预留行高，
    硬画会盖住已有代码），文件末尾之外按行距继续画
  - Ctrl+K 就地改写（对标 Trae / Cursor 的 Inline Chat）：有选区改选区，
    光标在 def/class 行上改整块，否则改当前行；后台线程请求，先看 diff 再落盘
  - FIM 默认后端改为 auto：聊天用 Copilot 就走 Copilot FIM，否则复用同一份
    base_url / api_key / model 走 OpenAI 兼容接口，不用单独再配一遍
  - 顺手把 FIM 的默认值调成"低延迟"：max_tokens 160、温度 0、超时 6s

v0.7.3 "Readable Edition" — 修掉 composer 里"字看不全"的三处硬伤：
  - Agent 选择器弹出后七个条目全被中部省略（"Default Agent" → "Def…ent"、
    "Code Review Agent" → "Co…nt"）。根因是 QComboBox：popup 是独立窗口，
    宽度只按控件自身（composer 里 78px）算，行高也不认 QSS 的 ::item padding，
    13px 字号下行高只有 11px、行与行互相压字。改用「胶囊 + QMenu」——
    菜单尺寸由内容决定，七项全名一次排开、当前项带勾、向上弹
  - 胶囊上只放短名（Default / Explore / Research / Review / Task / Plan /
    Rubber Duck）+ ▾，全名与说明放进 tooltip；宽度上限按名字实际长度给，
    面板窄到 330px 以下才允许省略
  - 权限徽章（"自动"/"按风险"）原本直接叠在 Agent 控件上：旧实现的
    sizePolicy 是 Ignored，QHBoxLayout 会把它记成 0 宽。改用胶囊后布局
    正常占据宽度，另加 _align_composer_row() 把三件控件统一高度
    （原来 26 / 22 / 32 三种高度，看着像没对齐）

v0.7.4 "Markdown Edition" — 修掉"整条回复不渲染 Markdown"：
  - 工具卡片是插件以文本形式拼进气泡的（"🔧 **fn**" + ```json 参数），旧实现
    只在卡片**前面**补了空行，末尾没有。于是下一段流式正文直接粘在收尾的
    ``` 上，变成 "```合适。"——CommonMark 里这不是合法闭合，围栏一路吞到
    消息末尾：后面的标题、列表、表格、粗体全部成了代码块正文，用户看到的
    就是"Markdown 完全不显示"
  - 新增 MessageBubble.append_block()：工具卡片 / 状态行 / 检查点这类"整段"
    内容统一走它，前后各保证一个空行；流式 delta 仍走 append()，首尾相接
  - 围栏解析加容错 _fence_break()：行首有足够长的同一字符就闭合，粘在围栏
    后面的正文补回文档流继续解析；"```python" 这种语言标注不算闭合，
    代码块里的示例围栏不会被误伤
  - 代码块白底：_style_pre 注入的 background:transparent 被 Qt 当成白色刷在
    <pre> 上，整块盖掉了单元格底色（暗色主题下成了白底 + monokai 浅色字）。
    改为直接删掉 <pre> 上的 background / line-height，让单元格底色生效
  - 工具参数按 JSON 缩进展示并限长 700 字符，卡片头从 "Code · 1 行" 变成
    "JSON · 3 行"，长内容不再挤成一行又被截断

v0.7.5 "Chinese Edition" — Agent 选择器全中文化：
  - 七个条目原来是 "Default Agent / Explore Agent / Research Agent /
    Code Review Agent / Task Agent / Plan Agent / Rubber Duck Agent"，
    在中文面板里很突兀。现在统一为 默认助手 / 探索助手 / 调研助手 /
    代码审查 / 任务执行 / 方案规划 / 橡皮鸭
  - 每个 Agent 新增 short_name 字段（默认 / 探索 / 调研 / 审查 / 任务 /
    规划 / 橡皮鸭），胶囊上直接用它，不再依赖 " Agent" 后缀推断短名
  - 各 Agent 系统提示词里的自称同步中文化（"你是探索助手（Explore）"），
    英文名以括注保留，方便模型与文档对齐
  - 菜单 tooltip、"点击切换助手"等旁白一并中文；数据层的 name 仍是英文字段
    （explore / code_review / ...），配置与历史记录不受影响

v0.7.6 "Math Edition" — LaTeX 公式终于能看了：
  - Qt 的富文本没有 MathML、也没有能用的 CSS 数学排版，KaTeX 那套前端方案
    搬不过来。改为在后端用 matplotlib 的 mathtext 排版引擎把公式光栅化成
    PNG，再以 <img> 内联进消息：块级 $$...$$ 居中成行，行内 $...$ 跟随文字
  - **只走 MathTextParser("agg") 的位图结果**，不经过 Figure / transforms：
    实测这条路上 fig.savefig() 会在 transforms.get_affine 崩掉
    （Windows fatal exception 0xc06d007f），而单纯解析 mathtext 没问题
  - 2 倍采样生成、显示缩回 1/2，高分屏与放大字号下都不糊；颜色取主题正文色、
    背景透明，深浅两套主题共用一套排版
  - 按 (公式, 字号, 颜色) 缓存 PNG 到临时目录，跨会话复用；流式渲染每帧
    重绘尾块时全部命中缓存（同一公式 6.0ms → 0.0ms）
  - 边界：金额 "$100 和 $200" 不会被当公式；mathtext 不支持的语法（或没装
    matplotlib）降级成等宽源码；单条消息最多渲染 60 个公式，防止一条长回复
    把界面卡住；超宽公式等比缩到 460px 以内
  - 顺带把 mathtext 不认但模型爱写的命令做了预处理：dfrac / tfrac 归一到 frac，
    去掉 displaystyle / tag / label / big 这类排版命令，operatorname{x} 换成
    mathrm{x}

v0.7.7 "Diff Edition" — 补丁按行标红标绿：
  - 之前审批卡里的差异是「整段套一个 <span 背景>」，Qt 只给文字上色、
    铺不满整行，看着像荧光笔划过。现在每行是一个 <td>，整行底色由单元格
    画，行号 / 标记 / 正文各占一列，才是 Cline、Roo Code 那种 diff 视图
  - 新增 widgets/diff_view.py：补丁文本 → 行模型（type + 旧行号 + 新行号）
    → 表格。三种来源归一到同一套渲染：unified diff（@@ 块头）、git 的
    --- / +++ 文件头、Codex 的 V4A（*** Update File: 后跟 @@ 与 +/-）
  - 双行号用两个计数器：'-' 走旧号、'+' 走新号、上下文两边都走。V4A 没有
    绝对行号时不显示行号（只给 *** Add File 从 1 起编），不伪造数字
  - 相邻的 -/+ 块做词级 diff（difflib.SequenceMatcher），只把真正变了的
    字符加深一档 —— 改一个字符不会整行重色
  - 代码块识别 ```diff / ```patch；没标语言的补丁靠 @@ / *** Begin Patch
    / --- 与 +++ 成对出现来认，普通代码里的 - 号不会被误判
  - 显示层细节：文件头行、@@ 块头、超长折叠（400 行）、CRLF 与
    "\\ No newline at end of file" 噪声清洗、按"实际画出的行"报行数
  - 顺带修了 recolor_html 的接力问题：深色背景 #19232d →
    浅色 #fafafa 之后，浅色正文恰好也是 #19232d，旧实现会把刚换好的背景
    再改回去（改成两趟替换）

v0.7.8 "Clarity Edition" — 字号跟上设置，图标换成矢量：
  - 任务面板整块重写。旧版把 #1a1a2e / 12px / 📋✅🔄 全写死在内联 QSS 里，
    既不认主题也不认字号：界面字号调到 18px，任务行依旧是 12px 的彩色
    emoji，在一堆 17~19px 的文字里像别人的界面。现在标题取 ui_font、条目取
    ui_font-1、行高与图标尺寸随之缩放，配色 / 圆角 / 进度条全部来自语义 token
  - 状态图标改成矢量：完成 = 实心圆 + 背景色镂空钩（make_icon 新增 hole
    参数，双画笔绘制），进行中 = 细环 + 内点，待开始 = 空心圆，取消 = 叉
  - 面板变成"透明容器 + 内嵌卡片"，左右与对话流同边距；收起时高度归零。
    踩坑记录：容器上写 setStyleSheet("background: transparent") 会把这条
    声明一并套到子控件，里面的卡片也变透明、只剩 1px 边框——改用具名规则
    QWidget#todoDock
  - composer 的 Plan/Act 与 Agent 选择器换成 PillButton：图标 + 文案 +
    矢量箭头三段拼装。旧的 "Act ▾" 里的 ▾ 是字符，Windows 字体里偏上、
    CJK 字体里偏下，字号一大就贴边。现在箭头是图标，"Act" 用闪电、
    "Plan" 用眼睛，胶囊高度由字号推（ui_font + 12），和发送按钮严格等高
  - 发送按钮改胶囊 + 向上箭头图标；状态行 / 工具结果里的 ✅❌⚠️🔧💾🧪
    换成单色记号（✓ ✕ !）或直接去掉，正文与代码里的 emoji 一律不动
  - 顺手修模型胶囊在 900px 宽面板里也会把 "deepseek-flash" 截成
    "deepseek-flas" 的问题（上限从 300px 提到按剩余空间算）

v0.7.9 "Inline Diff Edition" — 修改直接标进代码编辑区（Cursor 式复核）：
  - Agent 写盘之后，改动不再只活在聊天面板的审批卡里：文件在编辑器里
    就地高亮——新增行整行绿底、被删的原文以"幽灵行"插回原位（红底 +
    删除线），相邻的删/增行做词级加深，左侧一条红绿竖纹
  - 决定权在编辑器里：视口顶部常驻「接受修改 / 全部接受 / 撤销」，悬停
    到某一处修改行尾浮出 ✓ / ✗（接受 / 撤销这一处）；快捷键对齐 Cursor：
    Ctrl+Enter 全部接受、Ctrl+Backspace 全部撤销
  - 渲染走 Spyder 原生 TextDecoration 管理器（editor.decorations，与
    当前行 / 单元格高亮同一套），不会被 IDE 自己的 extraSelections 刷新
    冲掉；非 Spyder 环境自动降级 ExtraSelection
  - 落块工程细节（Qt 侧没有 zone widget，全靠文档手术）：
    · staging = 先在内存拼出「上下文 + 幽灵行 + 新增行」的最终行序列，
      setPlainText 一次性写入再按行号打 userData 标记——边插入边标记会
      因块身份漂移挂错行
    · 删块必须一次倒序删并按字符 position 快照重定位 + userData 验证
      （QTextBlock 按 index 引用，块合并会让对象失效或指向别的行）
    · 块合并会让 userData 归属漂移，处理完的 hunk 残留标记一律重扫清除
    · 复核期间拦截 Ctrl+S / Ctrl+Z（否则幽灵行会被存进文件、撤销会打散
      标记）；编辑器有未保存内容时直接放弃 staging，不碰用户 buffer
  - 接受 = 清幽灵行（文件已写盘，无需再动）；撤销 / 逐处撤销会把旧内容
    恢复回磁盘，落盘走 Spyder 的 EditorStack.save（编码 / 行尾 / 保存
    偏好全部沿用 IDE 设置），并给模型回灌一条"用户已撤销"的事实消息
  - 多文件一起复核：「全部接受」跨文件广播；聊天流里同步一条说明气泡
  - PyQt 所有权四个坑（这版最花时间的部分，全是 C 层崩溃、Python 侧
    连 traceback 都没有）：
    · QTextBlockUserData 是 QObject，setUserData 传进去的临时对象在
      表达式结束就没了 Python 引用 → PyQt 当场析构 C++ 对象 → 块里
      留下悬空指针 → 文档销毁时 double free（退出码 127）。必须把每个
      mark 的 Python 引用攥在手里，等块真的从文档消失再放手
    · event filter 装在编辑器上，而编辑器析构时 Qt 会回调名单里每个
      filter 的 removeEventFilter —— 若 filter（review）先被回收，那
      次回调就是访问野指针。review 析构时必须自己先摘 filter
    · deleteLater() 与"紧接着释放 Python 引用"不能同时做：前者把销毁
      排进事件队列，后者让 PyQt 当场析构，队列里那一笔稍后再执行就
      是野指针。清理 widget 只 hide，析构交给 GC
    · 任何改文档的动作之前先摘装饰：setPlainText 会重建所有
      QTextBlock，旧装饰里的 QTextCursor 立刻悬空

v0.8.0 "Autonomy Edition" — 把「跑到 25 轮就踢回给用户」改成预算式自主循环：
  - 旧行为：每批工具调用算一轮，累计 25 轮就 _agent_active=False +
    _finalize()，弹出「请回复『继续』」。读 5 个文件、搜 3 次、改 2 个
    文件就到底，长任务必然被切成好几段，这和主流代码 Agent 的体验相反
  - 现在分三层：
    · 软阈值（默认 40 轮）：只往上下文里塞一条「尽快收束」的系统注记，
      气泡里显示一次 🟡，不中断任务
    · 硬预算（默认 200 轮，0 = 不限制）：真正兜底，设置里可调
    · 自动续跑（默认 5 次）：用满一段预算就自动续期并把轮次归零，注入
      「预算已续期、任务状态未丢失」的注记后接着跑，用户不用再敲「继续」
  - 只有真正的死循环才硬停：重复的写 / 执行类调用（同一工具 + 同样参数
    连续 3 次）会停止；只读类重复只警告一次并让模型自己换招——旧版不
    区分，读文件重试 3 次就把整个任务掐断
  - 内置 Agent（探索 / 调研 / 审查 / 任务 / 规划 / 橡皮鸭）的
    max_tool_rounds 一律改成 0 = 跟随全局预算：角色节制本来就该由
    allowed_tools 表达，而不是用 8 / 10 / 25 这种小轮数硬切
  - 系统注记一律排在 tool 结果之后再入消息列表，避免破坏
    assistant(tool_calls) → tool 的紧邻约束
  - 消息裁剪保留轮次 20 → 40，长任务不会过早丢掉中间的读取结果

v0.8.1 "Chat Diff Edition" — 删行标红 / 增行标绿，做成任何时候都看得见：
  - 写盘前的那份旧文本快照不再只服务编辑器内联：它同时喂给对话里的
    红绿卡片。Agent 一旦落盘，被删掉的那几行就没有第二处可查了
  - 对话内新增一条出口：每次写文件后，改动按 hunk 画成「旧行号 / 新行号
    / 标记 / 正文」四列卡片，删除行红底、新增行绿底、词级变化再加深一档，
    走的就是审批卡那套 diff_view 管线（无外部依赖，difflib + 手写行模型）
  - 两种形态可切换：智能（编辑器已高亮就不重复）/ 总是（两处都画）/
    关闭。默认智能，所以「编辑器里没看到」时对话里一定还有一份
  - 卡片有规模上限：单文件最多 90 行、一次最多 6 个文件，超出的部分
    折叠成一行提示，避免一条消息把滚动条拉爆
  - 修掉「标了红却看不见」的根色问题：深色主题下 diff 底色原本是拿
    success / danger 按 84% 混背景混出来的 #243b3c / #3a3037——一个是
    脏青、一个是脏紫，在深蓝灰编辑器底上根本分不出谁红谁绿。现在红绿
    各定一组色相明确的底 + 词级加深档（深色 #1d3428 / #3d2325，浅色
    #dff3e5 / #fce3e2），编辑器内联也改用这两档，不再靠低 alpha 叠加

v0.8.2 "Diagnostics Edition" — 「什么都没发生」的失败全部改成能看见：
  - 起因：真实 Spyder 里看不到红绿行，而离屏预览一切正常。区别在于
    离屏走的是 ExtraSelection 降级路径，真实环境走 editor.decorations。
    把所有会 return False 的静默分支逐个加上原因，让下次失败能自己说话
  - stage() 现在记录 fail_reason：编辑器有未保存改动 / 读不到新内容 /
    新旧一致无差异 / 写文档抛异常，四种情况各有文案
  - ReviewHub 维护 last_failures；plugin.review_files 返回值从 int
    改成 {"staged", "failures"}，聊天流据此区分「成功」与「没生效 + 原因」
  - 部分失败时只给失败的文件补对话卡片，不再整批重画
  - 工具栏 / 左侧竖纹原本只等 viewport 的 Resize 事件才 show；文件早就
    打开、视口尺寸没变时 Qt 不会再发 Resize，于是红绿行画出来了、按钮条
    永远不出现。改成 staging 结束后主动排一拍
  - staging 成功后自动滚到第一处修改：文档被整体重写，视图却停在原来
    的滚动位置，改动在文件另一头时用户看到的还是一屏普通代码
  - 装饰挂上后主动催一次 manager._update()，不等 80ms 定时器
  - EditorStack.load() 的返回值直接用（Spyder 6 本来就返回 editor），
    只有在没有返回值时才回退去重扫 data

v0.8.3 "Visible Range Edition" — 红绿行终于真的画出来了：
  - 根因（这才是「没效果」的真凶）：Spyder 的
    TextDecorationsManager._update() 只把 editor.get_buffer_block_numbers()
    （可见行 ± 半屏）以内的装饰推给 setExtraSelections，范围外直接丢弃；
    而 _update 没有任何外部触发者 —— CodeEditor.update_decorations()
    在 6.1 里无人调用，只有 add_key 那一刻算一次
  - 旧顺序恰好踩中：setPlainText 让视图回到第 1 行 → 挂装饰（可见范围
    = 文件开头）→ 才滚到改动处。改动在 200 行开外时，所有装饰在过滤
    阶段就被丢光，滚过去也是一片空白。幽灵行在、按钮条在，就是没颜色
  - 改成先滚动居中到第一处修改，再挂装饰；并通过新增的
    _refresh_decorations() 在滚动 / 光标移动 / 切 tab（Show、Resize）
    时重算可见范围，深在文件任何位置的 hunk 都会在滚到眼前时上色
  - plugin.review_files 结束后把复核文件切到前台：没打开过的文件是
    load(set_current=False) 进的**后台 tab**，红绿行和按钮条全画在那个
    看不见的标签页里，用户这边就是「什么都没发生」
  - centerCursor 代替 ensureCursorVisible：贴边露一行时容易被下一次
    可见范围过滤挤出视野

v0.8.4 "Fold Edition" — 过程收进折叠面板，只留结论：
  - 之前一轮 Agent 的所有中间步骤（工具卡片、结果行、叙述段落）都拼进
    同一个 Markdown 串，等于把日志和答案写在同一张纸上：任务一长，用户
    要先滚过几十行参数 JSON 才看得到结论，而且没法收起来
  - 新增 ProcessPane：运行中展开、结束自动收成一行「已完成 5m1s · 6 步」，
    点一下再展开。判定沿用 Claude Code / Cursor 的那条边界——过程可折，
    结论和异常不折
  - 关键在于「归属」不是「渲染」：MessageBubble 里正文分成两段，
    _say 是尚未定性的尾段叙述。工具调用前它被归档进过程面板（后面跟着
    工具，说明它只是过渡），流结束时它被 commit 成正文（这一轮到此为止）。
    只靠 CSS 折叠做不到这件事
  - userOverrode：用户手动点过折叠箭头之后，自动策略不再改动它。没有
    这个字段，下一帧到达就会把用户刚展开的面板收回去
  - 失败不折：过程里出现 ✕ / 失败，这一轮结束时保持展开，避免报错被
    折进"已完成"里让用户以为成功
  - 工具行压成一步一行「▸ read_file `editor_diff.py`」，不再把 700 字符
    的 JSON 摆进折叠区——那样展开后照样是一屏看不懂的日志
  - 过程区用 surface_alt 底色 + 圆角与正文分层，尺寸策略设成 Maximum，
    否则外层布局会把剩余空间全分给它，灰块被拉高、正文挤在顶部

v0.8.7 "Blind Write Edition" — 用 run_command 写盘时也必须出现红绿：
  - 反馈是「改了文件，编辑器里一点颜色都没有，连一句失败提示都没有」。
    审计日志给了答案：弱模型（deepseek-flash 一类）几乎不用 edit_file，
    而是 python -c "open(r'D:\\...\\a.py','w').write(...)" / 重定向 / echo
  - 这类命令的参数里没有 filepath，affected_files 解析为空 ⇒ 写盘前
    快照为空 ⇒ _stage_editor_reviews 直接 return。整条预览链路静默哑火，
    而且因为「没有 items」，连失败文案都不会出现——最难查的那种失败
  - affected_files 新增 paths_from_command：从命令文本反推源文件。
    双引号 / 单引号必须**各扫各的**——python -c "…open(r'D:\\a b.py')…"
    这种嵌套写法用 alternation 写成一个正则，双引号会把里面的单引号
    整段吃掉，结果只抓到 "b.py"
  - 目录级兜底快照：一批里只要有 run_command，就在执行前扫一遍项目源
    文件的 (mtime_ns, size)，事后比对出真正被改的文件。不依赖模型怎么
    写字，上限 4000 文件 / 12 MB / 单文件 512 KB
  - _changed_by_scan 的 scan 必须由调用方传入：_stage_editor_reviews
    开头就把 self._review_scan 取走清空了，从 self 上再读只会拿到 None，
    于是每次都判定「什么都没改」
  - stage() 的未保存门禁改成「buffer 是否还停在改动前原文」：正常复核
    时磁盘已是新内容、buffer 还是旧内容，用「buffer ≠ 磁盘」当判据
    必然误判，isModified 一置位就永远复核不了
  - 一次最多铺开 8 个文件（py 优先）：一次开二十张 tab 比看不到更糟
  - 新增 /diffdemo 自检：对当前文件做一份**副本**造三处假改动并预览，
    不动原文件。一条命令分清「渲染没生效」还是「触发链路没走到」
  - 新增 docs/images/inline-editor-{dark,light}.png：装饰走真实的
    TextDecorationsManager 出图，过滤规则与现场一致

v0.8.8 (readable)：字号太小、还调不动 —— 三个 bug 叠在一起
  - markdown 的段落 / 引用 / 列表没写 font-size，全回落到
    QTextDocument 默认字体（中文 Windows 是宋体 9pt ≈ 12px）。
    于是「正文字体大小」调到 32 也只有标题和代码块会变
  - get_provider_config() 漏了 ui_theme / ui_accent / ui_density /
    tool_density 四项，外观页选了浅色、紧凑也毫无反应
  - 入口太深：顶栏新增 Aa 菜单（档位 12~36、界面字号、字体族），
    并支持 Ctrl+= / Ctrl+- / Ctrl+0 与对话流里 Ctrl+滚轮
  - 消息视图兜底设置文档默认字体，<br> 空行、流式尾块也跟着正文走

v0.8.9 (wired)：内联复核其实一次都没跑起来 —— 段名被实例字段遮住了
  - 报错是「编辑器内联未生效：文件 — staging 异常：'NoneType' object
    is not callable」。路径为空说明异常发生在 review_files 的循环之前，
    跟装饰 / 可见范围 / diff 计算全无关系
  - 真因：on_initialize 里写了 self._review_hub = None，而插件类里又有
    def _review_hub(self)。实例字典优先于类，于是 self._review_hub()
    实际在调用 None —— v0.7.9 起这条链路第一步就断了，每次都被外层
    except 兜成一句「staging 异常」，看起来像装饰没画出来
  - 方法改名 _ensure_review_hub()；ReviewHub 只在本地变量里建好再赋给
    字段，避免中途又被自己遮住
  - 单文件失败不再吞掉整批：循环体单独 try，失败项带上文件名与异常类型
  - 失败文案在路径为空时不再硬凑一个「文件」前缀
  - 新增静态守卫：全包扫描「实例字段与类方法同名」，这类遮蔽以后会被
    测试直接拦下（只靠单测 ReviewHub 是发现不了的——它绕过了插件）

v0.8.10 (visible)：顶部工具条报「修了几个文件」，并给出真正的总开关
  - 标签从「+2 处修改」变成「2/3 文件 · +16 −6 · 2 处修改」：切 tab 时
    终于知道一共改了几个文件、现在看的是第几个
  - 按钮换成「接受全部修改」「不接受本次修改」，两个都常驻。旧版的
    「全部接受」只在多文件时露面、「撤销」只作用于当前文件——用户要的
    「全部拒绝」根本不存在
  - 快捷键与按钮同义：Ctrl+Enter = 接受全部修改，Ctrl+Backspace =
    不接受本次修改（此前是文件级，容易和按钮语义打架）
  - 走宽度感知版式：标签按可用宽度三级降级（全量 → 去掉 ± 行数 →
    只留「2/3 文件」），按钮永远先占位，515px 的窄编辑区也不会把
    「不接受本次修改」挤出边界
  - 判可见性改用 isHidden() 而非 isVisible()：工具条是编辑器视口的
    子控件，首次 refresh 发生在视口 show 之前，那时所有子控件的
    isVisible() 都是 False，按它算宽度会把整条撑到 717px 盖住编辑器

v0.8.11 (stable)：内联 diff 把 IDE 打崩 —— 块标记继承错了基类
  - 崩溃栈落在 Spyder 自己的 linting 上：
    lsp_mixin._process_code_analysis() 调 data._selection()，而我们挂在
    block.userData() 上的 DiffBlockMark 没有这个方法 → AttributeError →
    manage_lsp_handle_errors 包成 LSPHandleError，编辑器直接瘫
  - 真因：v0.8.9 只「照抄了 Spyder BlockUserData 的字段」，类的基类
    仍是裸 QTextBlockUserData。字段能读、方法不在 MRO 里
  - 基类改成模块级算出来的 _MarkBase（Spyder 在就继承它），在 class
    语句里写死——运行时补不了
  - editor 不再传 None：mark 现在指向真编辑器，_selection() 内部要用
    editor.document() / textCursor()，给 None 就是下一颗雷
  - __getattr__ 收窄：dunder 照常抛，未知的其它下划线属性给「可调用
    兜底」而不是抛异常；也不能给 None（None 会被当方法调用）
  - 新增 test_v091.py 32 项 + probe_v091 真机探针：在真 CodeEditor 上
    塞 diagnostics 跑 finish_code_analysis()，复刻用户那次调用栈

v0.8.12 (vision)：输入框能贴图了 —— 截图直接变成缩略图发给模型
  - Ctrl+V 粘贴截图 / 拖图片文件进来 / 图钉按钮选文件，三种入口
  - 附件落盘到 ~/.spyder_copilot_chat/attachments（sha1 命名，天然去重），
    消息里只存 spyder-file:// 引用，base64 在发请求前才算 —— 会话历史
    不会被几十 MB base64 撑爆
  - ChatWorker._prepare_messages() 是三家提供方唯一公共咽喉：OpenAI
    兼容 / Copilot（顺带触发 Copilot-Vision-Request 头）/ Anthropic
    （转成 image block）
  - 用户气泡里显示缩略图，点开看原图；重试、历史回放都带图
  - 模型明确不看图时（deepseek-chat 等）给一句提示，不拦着发

v0.8.13 (honest)：API 出错时说人话 —— 402 不再让人去升级 Copilot 订阅
  - 新增 api_errors.py：按 状态码 + 错误体 + 提供方 分类成 余额不足 /
    Key 失效 / 限流 / 模型名错 / 超长 / 内容策略 / 服务端 / 网络 等，
    分类函数永不抛异常（错误处理自己不许成为新的崩溃点）
  - 402 的措辞跟着提供方走：DeepSeek 说「余额不足，去 platform.deepseek.com
    充值」，Copilot 才说「订阅额度用完」。旧实现所有 402 一律报成
    「Copilot 额度已用尽，请升级订阅」——方向错，用户照着修当然修不好
  - ChatWorker 抛结构化 ApiError（带状态码与错误体），不再拼
    "API 402: {json}" 字符串；错误气泡变成卡片：结论 + 服务端原文引用 +
    2~3 步真能照做的处置 + 页脚，充值 / Key 页链接可直接点开
  - 失败日志从 logger.exception 降为一行结论（traceback 降到 debug）：
    内部控制台里不再是红色回溯，不会再把「账户没钱」看成「IDE 崩了」
  - 已生成的半截回答不再被错误文案覆盖，卡片接在它后面
  - 错误处理全程 try/except 兜底，_on_error 永不把异常抛回 GUI 线程
  - 402/401/4xx 明确不自动重试（重试一百次也还是不够钱）
  - FIM 补全的 402 提示也按提供方改写，冷却 5 分钟不变

v0.8.14 (hy4)：腾讯混元 Hy4 / TokenHub 成为一等 Provider
  - 设置页新增「腾讯混元 Hy4（TokenHub）」：默认广州地址、hy4 /
    hy4-preview 两个模型、Key 控制台与产品文档入口、地域备选地址
  - 请求链路按官方契约发：Bearer 鉴权 + /v1/chat/completions、
    thinking: {"type": "enabled" | "disabled"}、合法 reasoning_effort、
    stream_options.include_usage、max_tokens 截到 64K 输出上限
  - 思考强度白名单按 Provider 走：Hy4 只有 high / no_think，DeepSeek 仍是
    low / high / max；从别家切过来残留的 low 会回落 high，不会发成 400
  - 带 tools 的 Agent 对话保留 Preserved Thinking：把上一轮的
    reasoning_content 原样回填进 assistant 消息，避免缓存命中率掉下去
  - 流式响应里的 {"error": ...} 帧不再被当成空 chunk 吞掉，六位业务码
    前三位映射回 HTTP 状态，重新走结构化 ApiError 与错误卡片
  - 错误文案单独处理 TokenHub：402 说清「余额不足 / 模型服务未开通」，
    401 说清广州 / 新加坡 / 硅谷 Key 不能跨地域用，MODEL / NETWORK
    提示的地址与模型名也跟着 Provider 走
  - FIM 复用 Hy4 时自动关思考、截 64K；不影响 OpenAI / DeepSeek 原有行为
  - 设置页顺手修了三处实拍发现的 UI 硬伤：通用 QComboBox 暗色配色、
    可编辑模型框文字被切、非 Copilot 时残留的空「认证:」行
  - 新增 test_v094.py 124 项（本地 mock TokenHub，覆盖注册表 / 设置页 /
    请求体 / SSE / 错误卡片 / FIM / 多模态 / 旧配置兼容）

v0.8.15 (quiet)：API 出错不再让 Spyder 弹「内部错误」窗口 —— 那不是崩溃
  - 现场：选 Hy4 后服务端回「模型不可用」，IDE 弹出「Spyder遇到了内部
    错误！」问题报告框，正文是 `Chat request failed: 模型「hy4」...`
  - 真因：Spyder 6 把 sys.stderr 重定向到内部控制台，对 error 流的每一行
    都 emit sig_exception_occurred → SpyderErrorDialog；logging 的 root /
    lastResort handler 正好写 stderr，于是一句 logger.error 等价于「向 IDE
    报一个未处理异常」。v0.8.13 只降了 traceback，error 级别本身仍在弹
  - 新增 safe_logging.py：包级 logger 挂专用 handler + propagate=False，
    日志不再进 stderr；只写 stdout 且统一显示成 WARNING（双保险），
    traceback 强制单行剥离、存进内存诊断缓冲供 diagnostics_text() 导出
  - 包内所有业务 logger.error / logger.exception 降为 warning；AST 静态
    守卫拦住以后新增的 error 级调用；debug 模式的 exc_info 也不再能弹窗
  - ChatWorker._emit_error 整段兜底：分类 / 渲染 / emit 任一环失败都退化
    成通用卡片，绝不把异常抛回 Qt 事件循环
  - 顺手修 Hy4 误诊：TokenHub 402「模型服务未开通」的错误体常写成
    "model does not exist"，被判成「模型名写错」；现在强制归 BALANCE，
    MODEL 卡片也给开通 / 地域 / GET /v1/models 三步指引
  - ApiErrorInfo.links 此前是死字段，现在渲染成卡片里可点的控制台链接

v0.8.16 (verified)：拿真实 TokenHub Key 做线上验证后修掉两个 Hy4 配置坑
  - 实测结论：短别名 https://tokenhub.tencentmaas.com/v1 上 GET /v1/models
    返回 200（hy4 online、hy4-preview pre-offline），hy4 的非流式 / 流式 /
    no_think 请求全 200，中文回答无乱码，usage 带 reasoning_tokens；
    插件 ChatWorker 的中文问答 / 多轮 / usage 信号也全部跑通
  - 默认地址从长域名 tokenhub.tencentcloudmaas.com 改成短别名：同一把
    Key 在长域名返回 401002「API Key 不存在」，两套广州入口各自签发 Key
  - 用户配置里 /plan/v3 这类套餐页地址会让 chat 走错路径（实测 401002、
    /models 404）。load_config() 现在把官方域名下非 /v1 的路径自动规范成
    scheme://host/v1；自定义代理 host 不受影响，纠正会打一条 warning
  - 修误诊：401002 原文是 "The API Key does not exist ..."，旧的裸
    "does not exist" marker 把它判成「模型名写错」，于是报「模型 hy4
    不可用」，用户改名字永远修不好。现在 blob 纳入完整错误体（让
    authentication_error 参与判断）、401/403 语义优先于措辞、MODEL
    markers 去掉裸短语靠正则精确匹配；真正的 400004 仍归 MODEL
  - AUTH 卡片补「地址必须以 /v1 结尾」「两套广州入口 Key 不通用」、
    「/plan/... 不是接口地址」；设置页 tooltip / 偏好页 / README 同步
  - FIM auto 后端默认地址同步；新增 test_v096.py 74 项（带 Key 时 83 项，
    含真实线上 /v1/models + chat + ChatWorker 端到端），全库 20 脚本全绿

v0.8.17 (indexed)：对话流右侧多一条消息导航条，历史发过的话一点就回
  - 对照参考截图补的缺口：长会话想找回自己问过的某句，只能一直往上滚
  - MessageRail：一条刻度 = 一条用户消息，自绘 paintEvent，等距摆放
    （按真实比例会让长回答里的几条消息挤成一坨）；当前视口那条用
    accent 加粗放大，悬停给预览 tooltip，点刻度直接滚回去并闪 1.2s
  - MessageFlyout：鼠标移到右缘展开完整清单，一行一条，点行跳转；
    最多 24 行 + 「还有 N 条」提示；条数没变时只挪高亮不重建列表，
    免得长清单的滚动位置被弹回顶部
  - 浮层做成面板内子控件而不是 Qt.Popup——Popup 一出现就抓走鼠标，
    视口收不到 MouseMove，「移开自动收起」会全部失效
  - 导航条锚到 viewport 右缘而不是 scroll 右缘，避开 9px 竖滚动条；
    面板缩放 / 滚动区几何变化 / 主题切换都会重新定位
  - 设置页新增「消息导航条」开关（message_nav_enabled，默认开），
    关掉立即收起；清空会话同步清索引，deleteLater 的半死气泡会被探活摘掉
  - 修一个会让进程直接消失的坑：_refresh_rail 跑在 QTimer 槽里，首版
    遗留一个未定义的 total，Qt 槽内未捕获异常会被 PyQt 直接 abort
    （exit 127、无 traceback）。现在整段 try/except + 降级清空刻度
  - 修浮层当前项不高亮：setSelected / setCurrentItem 必须先 addItem
  - 深浅两套主题实拍 4 张；新增 test_v097.py 87 项，全库 21 脚本全绿

v0.8.18 (rendered)：半截代码围栏不再把整轮过程吞成一张 Code 卡片
  - 现场：一张 Code · 33 行卡片里 # / ** / 反引号全是原样字符，卡片
    上方两行工具轨迹却是正常粗体；复制出来的纯文本没有围栏，因为消息
    气泡的「复制」优先提取代码块正文，围栏被剥掉了
  - 真因：read_file 结果是「文件头 + 围栏 + 内容 + 围栏」，过程卡片只取
    前 3 个非空行做预览，开启围栏留下、闭合围栏还在文件末尾被截掉；
    ProcessPane.append 用空行把每条过程拼成一份 markdown，这个没闭合的
    围栏一路吞到消息末尾，后面的标题 / 列表 / 检查点全成了代码块正文
  - 源头修：预览只是摘要，_strip_fence_lines 剥掉围栏行；只补闭合围栏
    的话 README 标题仍会被单独关进一张 1 行小 Code 卡片
  - 兜底修：整段插入前用渲染器自己的 parse_blocks 判末块是否未闭合，
    是就按原 marker 长度补上（ProcessPane.append 与 append_block 都做）。
    复用渲染器口径很关键：宽松闭合「围栏后紧跟正文」已判 closed，凭正则
    数奇偶会误补出第二个围栏，反而把后面的正文再吞一次
  - parse_blocks 的 code 块带出 marker；完整 diff 卡片、流式 delta、
    四反引号嵌套围栏都不受影响
  - 新增 test_v098.py 73 项（含真实会话数据复现），全库 22 脚本全绿

"""

__version__ = "0.8.18"
__edition__ = "rendered"

# Spyder 把 stderr 的每一行都当成未处理异常弹「内部错误」窗口。包一加载
# 就接管自己的日志出口，保证任何子模块 import 之前这条隔离已经生效。
try:
    from .safe_logging import install as _install_safe_logging
    _install_safe_logging()
except Exception:  # pragma: no cover - 日志隔离失败不该拖垮插件加载
    pass

