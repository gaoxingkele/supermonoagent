---
type: Review
title: WebView 布局与 Windows 原生终端方案评估
status: active
updated: 2026-10-21
owner: llm
tags: [windows, terminal, webview, layout]
source_paths:
  - src/supermonoagent/web.py
  - src/supermonoagent/desktop.py
  - src/supermonoagent/pty.py
  - src/supermonoagent/pump.py
  - src/supermonoagent/hub.py
  - study/orca/src/main/daemon/pty-subprocess/native-pty-spawn.ts
source_urls:
  - https://github.com/microsoft/intelligent-terminal
  - https://github.com/sweetcornna/cmux-for-windows
  - https://github.com/0x5bfa/WindowsTerminalControl
confidence: medium
---

# WebView 布局与 Windows 原生终端方案评估

## 结论与证据边界

建议先改善现有布局和终端数据正确性，同时用一个独立原型验证原生终端控件。保留 Python daemon、Hub、WebSocket 中继及手机端。只有原型证明中文输入、全屏 TUI、性能和同会话远程接入均满足要求，再扩大原生桌面端范围。

这是技术评估与待实施方案，不是已完成的性能优化。检查基于当前工作区，包含原有未提交的 desktop.py、web.py、WebGL 等改动；未修改业务代码、安装候选客户端或启动用户的交互窗口。现有 docs/截图20261014.png 仅作为历史参考，当前布局判断以源码为准。未进行实时 WebView 截图、端到端时延或候选软件兼容性实测。

Evidence 表示源码或维护者文档直接支持；Inference 表示工程判断；Unknown 表示尚需验证。外部信息核查日期为 2026-10-21。已读全局库 D:/aicoding/mylib/README.md、WIKI.md 与 LLM_Wiki/README.md，未从其中引用本次终端选型事实。

## 现有实现回顾

| 已核实的事实 | 证据 |
| --- | --- |
| 桌面入口通过 pywebview 打开本机页面，1440×900，最小 960×640；Windows 使用 WebView2 的设计已记入项目状态 | [desktop.py](../src/supermonoagent/desktop.py#L1)、[创建窗口](../src/supermonoagent/desktop.py#L52)、[状态](../memory/project_status.md) |
| 当前界面是固定宽侧栏、可换行工具条、绝对定位叠放卡片及最小化 dock；已有最大化与多种平铺功能 | [布局 CSS](../src/supermonoagent/web.py#L10)、[排列入口](../src/supermonoagent/web.py#L1112) |
| 每个会话 pane 创建 xterm 和 WebSocket；桌面查询参数才启用 Unicode11/WebGL | [渲染器](../src/supermonoagent/web.py#L459)、[pane](../src/supermonoagent/web.py#L856)、[连接](../src/supermonoagent/web.py#L888) |
| 后端使用 pywinpty 启动 shell，PowerShell 注入 PSReadLine 颜色；本机虚拟环境 pywinpty 为 3.0.5 | [pty.py](../src/supermonoagent/pty.py#L17)；版本来自本机包元数据 |
| 输出按 20ms 合并，保留 256KiB 原始尾部；待发超 128KiB 改发该尾部 | [pump.py](../src/supermonoagent/pump.py#L7) |
| 同一会话可以有多个客户端；每个客户端都能改会话行列，限制为 20–300 列和 5–80 行 | [Hub 输入与 resize](../src/supermonoagent/hub.py#L429) |
| 中继 pane 客户端收频道内全部会话；页面再按 session id 过滤 | [订阅扇出](../src/supermonoagent/hub.py#L564)、[页面过滤](../src/supermonoagent/web.py#L905) |

重要区别：ConPTY 是伪终端与进程连接层，xterm/DirectWrite 是终端解析和显示层，WebView2/WinUI 是应用界面宿主。换成 WinUI 外壳、仍用 xterm 显示终端，并不等于替换了 WebView 终端。

## 布局优化方案

Inference：现有功能已覆盖窗口管理的基础，主要需要减少层级重复、遮挡和操作入口分散。无需先换前端框架。

建议默认布局：

```text
┌ 项目 / 主机 / 连接状态 ────────────── 新建会话 ▾  布局  ⋯ ┐
│ 项目与会话侧栏 │ PowerShell  × │ CMD  × │ Codex  ×       │
│ 项目 A        ├──────────────────────┬─────────────────┤
│   办公室主机  │ 当前终端             │ 可选第二分屏     │
│     会话 1    │                      │                 │
│     会话 2    │                      │                 │
├───────────────┴──────────────────────┴─────────────────┤
│ 当前路径 · shell · 120×32              本地/中继 · 状态  │
└───────────────────────────────────────────────────────┘
```

| 优先级 | 建议 | 解决的问题与实施落点 |
| --- | --- | --- |
| P0 | 工具条收为单行：新建、布局、更多；标题就地改名，中继/Token/预设放入对应设置 | 当前顶栏混合日常操作和低频配置且允许换行，见 web.py:11、127；减少窗口缩窄引发的终端高度变化 |
| P0 | 默认标签＋左右/上下分屏；保留自由叠放为可选模式 | 当前 .card 绝对定位及拖动逻辑见 web.py:80、641；已有平铺可作为过渡，后续采用比例分割树保存布局 |
| P0 | 一个窗口下的多条 CLI 用标签表达，同一个会话切换时保留已有终端实例 | 当前 ensurePane 按 session 建卡片，见 web.py:856；避免窗口、CLI、卡片三个概念对用户形成重复层级 |
| P1 | 侧栏可拖宽、可搜索，默认稳定排序；活动状态用徽标表达 | 当前固定 260px，活动排序逻辑见 web.py:19、1078；“按最近活动排序”保留为显式选项，不能声称当前每次输出都会重排 |
| P1 | 状态信息放到底栏：连接、当前 shell、工作目录、行列；吞吐/调试放详细面板 | 当前顶栏状态与每卡片速率分散，见 web.py:298、725；正常工作时让终端正文占据主要面积 |
| P1 | 明确区分“关闭视图”和“结束会话”，后者才杀进程 | 当前关闭按钮发 OP_CLOSE，见 web.py:873；终止路径见 hub.py:445。迁移时不能让一个关闭图标悄然改变含义 |
| P1 | 手机保留一个主终端，抽屉切会话，增加 Esc/Tab/Ctrl/方向键辅助条 | 当前窄屏已单窗与浮层侧栏，见 web.py:55、2007；辅助条应随软键盘安全区布局，不覆盖正文 |

界面采用系统 UI 字体，等宽字体只用于终端与代码；统一间距和边框，活动终端使用单一强调色。尺寸 40px 顶栏、24px 状态栏、220–280px 侧栏仅作为原型起点，最终以可用终端行数及中文可读性验证。

## 比换壳更优先验证的终端问题

1. **输出保真（高置信度代码事实，中等置信度影响判断）。** web.py:554 的 colorizeEcho 每次调用重置颜色状态，web.py:618 对每块 PTY 输出再着色。分块可能跨 ANSI 控制序列，额外 SGR 也会改变程序本来的颜色。建议真实终端默认原样交给解析器，保留 PSReadLine 原生配色；内容高亮只用于明确的独立日志视图。
2. **UTF-8 分块（高置信度事实）。** web.py:919 每帧新建 TextDecoder，pump.py:32 从任意字节位置截断。建议先按二进制协议剥离 session id，再采用每会话增量解码或 xterm 的字节输入；快照边界与 ANSI 状态另行处理，不能只修字符截断。
3. **丢帧与恢复（高置信度事实）。** hub.py:591、relay.py:206 队列满时丢最旧帧；pump 的所谓快照是原始字节尾部，web.py:617 收到快照会 reset 后重放。ANSI 是有状态流，尾部未必带齐光标、颜色、备用屏和滚动区状态。建议给流加序号、缺口检测和可验证的重同步，采用有界背压或明确的慢订阅者恢复策略；严格恢复需维护可序列化的屏幕状态。不能把任意丢帧视为可靠交互。
4. **小包回显（高置信度参数，Unknown 实际时延）。** pump.py:9 的 20ms 会给输出批次增加等待，实际端到端耗时还含 shell、输入、网络与渲染。先比较 20ms 基线、小输出即时发送、连续大输出短批处理三个策略；不要直接把定时器清零当成完成优化。
5. **输入/resize 阻塞与尺寸所有权。** daemon.py:216、225 直接调用同步 Hub→PTY 操作；阻塞是否显著需采样。应评估每会话有序写入队列，避免长粘贴阻塞 asyncio 主循环；不要为每个按键无序启动线程。hub.py:435 没有尺寸主控规则，建议交互拥有者决定尺寸，观察者适配显示，手机接管时显式转移，避免双端轮流 resize。
6. **多 pane 扇出。** 每 pane 一连接且中继连接收频道内全部输出，见 web.py:888、hub.py:564。Inference：同频道有 N 个 pane 时，数据接收量可能随会话和连接的组合放大。建议过渡到一个数据连接按 session id 多路复用，且明确订阅范围；不能直接删掉现有“所有窗口持续推送”的产品行为。
7. **WebGL、尺寸及后台渲染。** WebGL 只在 ?shell=desktop 启用，丢失 context 后当前只 dispose（web.py:459）；尺寸读取依赖 xterm 私有 _core，存在兼容负担（web.py:418）。建议按能力检测使用 GPU，记录回退原因，恢复时重新验证；隐藏 pane 保留会话与有界状态、降低绘制频率，恢复时重绘。不要把“暂停后台绘制”实现成丢掉其终端字节。

## Orca 的实际参考价值

Evidence：本地 study/orca 提交 25f87544 使用 Electron、@xterm/xterm、WebGL 和 node-pty（package.json:196、266、267、274）。Windows spawn 显式 useConptyDll: true（src/main/daemon/pty-subprocess/native-pty-spawn.ts:55），即采用其随包 ConPTY 实现路径。本项目 pywinpty spawn 没有显式指定 backend；尚未验证实际加载的 ConPTY 模块版本，不能假定双方完全相同。

Orca 有前后台输出调度及队列预算（src/renderer/src/lib/pane-manager/pane-terminal-output-queue-registry.ts:90）、WebGL context 恢复策略（pane-webgl-renderer.ts:28）、Windows PTY 兼容选项（windows-pty-compatibility.ts:36）及 headless 解析/序列化（src/main/daemon/headless-emulator.ts:1）。这些证明它在同类渲染技术上做了额外工程处理，不能据此宣称已经测出性能优势。

Inference：值得吸收的是输入、输出、尺寸、后台任务和状态恢复的分工，而不是完整复制其 Electron/React 架构。单纯把 pywebview 改成 Electron 或 Tauri，不会自动解决当前协议和输出保真问题。

## Windows 客户端与控件核查

以下“支持”除本项目代码外，均指维护者文档声明，未在本机安装验证。

| 候选 | 核实结果 | 与本项目的关系 |
| --- | --- | --- |
| [Microsoft Intelligent Terminal](https://github.com/microsoft/intelligent-terminal) | 微软实验性 Windows Terminal 分支，保留 shells/profiles/tabs，增加 ACP agent 面板、会话管理与 hooks；[发布页](https://github.com/microsoft/intelligent-terminal/releases) 当前列 0.2.2572.0 | 最值得优先试用的现成“Windows 终端＋agent 管理”客户端；不能据此认定能接入 SuperMonoAgent 的 WebSocket 会话或手机中继 |
| [sweetcornna/cmux-for-windows](https://github.com/sweetcornna/cmux-for-windows) | 独立社区移植；WinUI 3＋ConPTY＋Rust/Ghostty 解析＋DirectWrite/Direct2D。原生终端，WebView2 用于浏览器 pane。工作区/标签/分屏、中文 UI、agent 状态；[发布页](https://github.com/sweetcornna/cmux-for-windows/releases) 当前列 0.3.8 | 与“原生终端＋Orca 式工作区”最接近的研究样本。README 明示不含远程 daemon/SSH/relay；重启恢复布局和 provider 会话，不恢复旧进程、输出和 scrollback |
| [Windows Terminal](https://learn.microsoft.com/en-us/windows/terminal/) | PowerShell/CMD 等配置、多标签与 GPU 文字渲染 | 兼容性与体验基线；wt.exe 启动新 shell 不会自动接管 Python 已有 PTY |
| [WezTerm](https://wezterm.org/features.html) | Windows 客户端、pane/tab/window、可搜索历史、Lua 配置；[multiplexing](https://wezterm.org/multiplexing.html) 有自己的 domain 协议 | 多终端管理参考和对照基线；非本项目现成前端，Windows 本地脱离 GUI 的持久化语义需单独验证 |
| [Rio](https://rioterm.com/) | 提供 Windows 10+ MSI 与便携版；GPU 终端方向 | 可做原生渲染对照，尚不足以据现有材料认定覆盖本项目的项目/手机/频道管理 |
| [hiesem/cmux-win](https://github.com/hiesem/cmux-win) | README 明确 Windows 实现在 cmux-win/，Electron＋TypeScript＋node-pty/ConPTY＋xterm | 可参考管理交互，但不能作为“去 WebView 终端”方案 |
| [WindowsTerminalControl](https://github.com/0x5bfa/WindowsTerminalControl) | 第三方 WinForms/WPF/WinUI 3 宿主，示例用 ConPTY 接 PowerShell；源码构建要求 .NET 10 等工具 | 真正替换终端显示层的原型候选。须验证自定义字节连接接口、部署依赖、生命周期和 IME，不能把 NuGet 包名当成微软官方稳定嵌入 SDK |
| [Microsoft.Terminal.WinUI3](https://github.com/NikulovE/Microsoft.Terminal.WinUI3) | 第三方 Windows Terminal 控件封装；文档提示 HWND airspace 导致普通 WPF 内容不能覆盖终端区域 | 备选控件；这也意味着当前自由叠放/浮层设计不能原样迁入 |

[Ghostty 官方说明](https://ghostty.org/docs/about) 仍将其原生 GUI 描述为 macOS/Linux；社区 Windows 移植和可复用解析核心不等于官方 Windows 桌面客户端。名称中有 Windows 的仓库也可能只是保留上游 README 的 fork，需要核实真正的 Windows 源码和发布资产。

## 推荐实施路径（未执行）

### A. 先形成可测量的现有客户端基线

目标：保留已有能力，以最小差异改善布局和交互正确性。涉及 web.py、pump.py、hub.py、daemon.py、relay.py、tests/；入口见上面的逐项证据。

- 先补 UTF-8/ANSI 跨帧、队列溢出、双客户端尺寸、焦点与 IME 的行为测试；已有 tests/test_pump.py 和 test_windows.py 仅保护部分后端路径。
- 先做输出保真、可靠恢复和时延测量，再对工具条、标签/分屏、稳定侧栏逐项改动；两类改动分开交付。
- 后续可按职责将 web.py 内的样式、终端适配、连接与布局模块拆出；只在行为保护后进行，不引入框架或新依赖作为前置条件。

### B. 原生终端单 pane 原型

目标：WPF 或 WinUI 3＋候选 Windows Terminal 控件，显示同一个 Python daemon 拥有的会话。首选哪一框架取决于控件接口与分发验证，当前证据不足以锁定。

```text
PowerShell/CMD ↔ Python 会话与 PTY
                       ├─ 本地连接 ↔ 原生终端控件
                       └─ 现有中继 ↔ 网页/手机 xterm
```

原生控件只负责 VT 解析、绘制和输入，必须能将输入/resize 发给 daemon、消费 daemon 输出。若示例默认自建 ConPTY，必须改用可替换的连接适配器；不能开一个新的 shell 冒充同一会话。不要同时让 Python 与 .NET 各自拥有一套相互隔离的真实终端。

先只证明一个 pane 能接现有 session id；再考虑标签、分屏、配置和原生桌面生命周期。第一轮原型无需把整个界面翻写，也不需要替换 relay。只在原型达标后考虑原生桌面成为默认客户端，网页/手机保持独立显示端。

### C. 选择条件

Inference：短期投入产出更倾向 A；若核心要求是彻底去掉终端 WebView，则 B 是目标路线，A 中的数据保真与流控修正仍应复用。直接 fork 完整 Orca 或另一个客户端，会承担其状态、更新、会话和协议迁移成本，目前没有证据显示比保留 daemon 更省事。

## 验收与风险控制

下面是建议验收目标，不是现有实测结果：

| 验证 | 通过条件 |
| --- | --- |
| 桌面布局 | 960×640、1440×900、1920×1080，100%/150% 缩放；常用顶栏不换行，聚焦 pane 不被其他 pane 遮挡 |
| 移动布局 | 390×844、软键盘展开/收起、横竖屏；当前光标可见，辅助键不覆盖最后一行 |
| 输入 | PowerShell 5.1、pwsh 7、CMD；Tab、历史、Ctrl+C、选区复制、粘贴、中文 IME 连续输入各 100 次无串窗或重复输入 |
| 字节保真 | 多字节 UTF-8、SGR/OSC/CSI 在每个可能位置拆包，结果与完整输入一致；原始输出没有客户端额外插入的颜色序列 |
| TUI/resize | 编辑器、分页器、可用的 Claude/Codex CLI；备用屏进退与 100 次分屏/最大化/恢复无残影、错行或持续 resize 振荡 |
| 流控与恢复 | 慢客户端、32 帧队列溢出、超过 256KiB 输出与重连；无静默缺口，恢复后屏幕与权威状态一致，其他客户端输入不中断 |
| 交互时延 | 同机同字体同 shell 同负载，记录按键→PTY、PTY→解析、解析→可见画面；本地 p95 按键到显示初始目标 ≤50ms，同时报告相对基线变化 |
| 多会话 | 1/4/16 个会话，各做 60 秒持续输出；记录前台输入 p95、CPU、RSS、GPU/绘制帧间隔和收发量；隐藏 pane 不持续重绘、不丢其状态 |
| 同会话远程 | 原生桌面和手机看到同一 PID/session；明确尺寸控制者；只关闭桌面视图后手机仍可用，明确结束会话才终止进程 |

风险：原生控件可能与当前后端连接模型不兼容，第三方打包和 ABI 也可能增加维护负担。缓解：将字节连接适配、IME、DPI 与打包作为原型第一轮门槛，未通过则保留已优化的 WebView 客户端。

风险：原始尾部恢复无法保证完整终端状态，增加协议可靠性会超出单纯视觉调整。缓解：独立协议变更与布局变更，并加入序号和恢复测试，不用“看起来正常”代替字节与屏幕校验。

风险：新软件文档描述不等于本机实际体验。缓解：Intelligent Terminal、原生 cmux fork 与现有客户端使用相同测试矩阵；不凭“原生”标签宣称更快。保留手机与 relay 是选型硬约束。

完成条件：评估已给出布局、候选、架构与验收；实施尚未开始。后续实施先跑项目适用测试与静态检查，再用真实 PowerShell/CMD 与界面验证，不能以当前评估替代性能证据。
