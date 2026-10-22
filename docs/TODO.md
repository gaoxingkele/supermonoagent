# 修改 TODO

## 2026-10-22：TD-001～TD-006 执行状态（当前）

用户已确认本次包含新增 TD-006；以下为当前状态，后面的 2026-10-21 检查、登记和旧包验收作为历史记录保留，不再代表“仍未实施”。详细代码落点和证据见 [本轮验证记录](todo-completion.md)。

| 编号 | 当前结果 | 验证/剩余步骤 |
| --- | --- | --- |
| TD-001 | [x] 已实现收发速率与连接质量格 | 字节/时间采样、空闲零速率、重连/验证失败、按电脑独立统计及多宽度回归通过 |
| TD-002 | [x] 换行、确认网格与原生缩放通过 | 真实 CLI 中英文草稿、IME、窄分屏；WebView2 普通/最大化/窄窗口 × 100%/125%/150% 共 9 组通过，跟底与主动历史浏览均验证 |
| TD-003 | [x] 已移除新版经典入口 | 源码及隔离打包 EXE 页面验证；经典路由继续兼容 |
| TD-004 | [x] 已完成并扩大粘贴验证 | Windows 流式边界/UTF-8/CRLF、真实隔离 Kimi 与 PowerShell/Codex 草稿证据 |
| TD-005 | [x] 已完成受控全量重绘 | 现代 ACK、旧协议 PONG 屏障、丢确认/重连/IME/租约竞争回归 |
| TD-006A/B/C/H | [x] 桌面多主机与隔离回归 | 同 relay、相同 session ID、含冒号 host ID、并发输出/定向输入、断线/错 Token/延迟回调 |
| TD-006D | [ ] Android 源码和 APK 完成，真机安装待完成 | 双主机/生命周期与改地址清旧草稿回归通过；之前覆盖安装停在荣耀本人指纹/系统密码验证，最后确认已装版本为 0.2.4；当前 adb 无设备，新包 0.2.14(16) 未覆盖安装 |
| TD-006E/F | [x] 已知房间鉴权发现与推送 | 无公开房间 roster，错误凭证/未知/无法验证离线一致；不轮询 health |
| TD-006G | [x] 统一身份/分发设计完成 | 见 [安全与身份设计](global-workspace-security.md)，每主机独立凭证，不引入全局明文 Token；统一账户未上线 |
| TD-006I | [x] 资源评估完成，实测边界已登记 | 1/4/16 主机消息/内存、未订阅输出为零、Android 后台关闭；未声称测得长时间电池功耗 |
| TD-006J | [x] 协议与架构说明已更新 | 新鉴权、TLS/Host/Origin、目录推送、所有权登记及客户端连接模型 |
| TD-007 | [x] Windows 左栏窗口状态灯与分组缩进 | 红色断开、黄色无近期输出、闪烁绿色实时输出；黑底可见、窗口行增加缩进；相关 Python 回归 16 项、Node 32 项通过，新包 `dist/windows-sidebar-status/`；详见 [状态灯说明](sidebar-activity.md) |
| TD-008 | [ ] 默认中继、统一账号与自动选择电脑，部署待完成 | 默认地址可选/手填、统一账号目录与自动填充已实现；TLS模板、真实Caddy及最终 Windows 包的账号/发现/隔离 CMD 验证通过；部署包 `dist/relay-hot-update-deployment.zip` 已生成，公网部署仍需要服务器管理入口；见 [配置说明](relay-account-setup.md) |
| TD-009 | [ ] 桌面/手机签名界面热更新，部署待完成 | 自动提醒、顶部手动检查、双平台打包发布、完整备份和回退已实现；真实 WebView2 挂死恢复通过，Windows 外壳和 Android 0.2.14(16) 已构建，签名更新包已生成；公网发布仍需 SSH/TLS 配置，手机未连接、未覆盖安装；见 [热更新说明](ui-hot-updates.md) |
| TD-010 | [x] Windows 同手机图标已打包 | 手机原图复用为七尺寸 ICO；当前正式编号统一为手机端 0.2.14，目录 `dist/windows/0.2.14/`，以后 Windows 同步使用手机版本号，不再按功能另起名字；EXE/窗口/任务栏图标与快捷方式已验证 |

安全审查发现的直连旧 Token 撤销与 Android 修改目标后旧草稿残留已修复并增加回归。原生缩放验收与 Windows 重建包验证已通过；最终测试数字见 [本轮验证记录](todo-completion.md)。尚未关闭的是手机系统允许后的覆盖安装及真机验证，不把 APK 构建通过等同于手机已升级。

交付目标：`dist/windows-global-workspace/`、`dist/android-global-workspace/SuperMonoAgent.apk`。现有 daemon/Kimi/Codex 与远程生产中继均未重启；生产 TLS 和房间登记需按文档迁移，不等同于本轮已远程部署。

## 2026-10-21：连接状态、长输入换行与顶部入口

状态：**仅检查和登记，等待实施。** 本轮不修改功能代码、不重打包、不重启客户端或 daemon。以下代码位置是实施落点，不代表已修改或已修复。

### 二次核实结果（2026-10-21）

三项均仍待实施，当前源码与原清单的主要描述一致。尺寸接管与快照回放已有修复，不应重复登记成“完全没有自适应”；本次 TD-002 专门追踪用户仍报告的长输入换行与边框越界，尚不能标记为完成。

| 补充核实项 | 证据 | 归入 |
| --- | --- | --- |
| 换电脑／重连仍保留上一连接的 RTT | `workspace.js` 的 `metrics.lastPongMs` 初始化后，仅在 OP 11 响应中更新；`connect()` 和 `switchHost()` 未清空它 | TD-001 |
| 验证失败却显示“重连中” | `ws.onclose` 先把 `#connection-label` 设为“离线 · 重连中”，遇到 4001 随后直接返回，没有安排重连 | TD-001 |
| 非房间码的远端直连也可能标成本机 | `sync()` 仅按 `config.code` 是否存在选择“中继已连接／本机已连接”，未区分直连目标是否在本机 | TD-001 |
| 窄分屏存在前后台最小行列不一致的边界 | `fitPane()` 使用 FitAddon 的本地行列，后台 `resize_session()` 会约束到至少 20 列、5 行；隔离 FakePty 检查实际确认：请求 8×3，后台接受并返回 20×5 | TD-002 |
| 已提交的 resize 尚未确认就被缓存 | `fitPane()` 在 `send(5, ...)` 成功后立即更新 `pane.lastSize`；发送成功本身不等于后台接受该尺寸或取得租约 | TD-002 |
| 删除按钮还需同步错误文案与打包界面 | “右上角经典版”的提示仍在 `sync()`；`workspace.py` 的页面嵌入打包代码，单独改 JS 不能完成顶部入口变更 | TD-003／共同交付门槛 |

证据边界：本次增加的运行核实只使用进程内隔离 FakePty，没有启动真实 shell，也没有向用户现有终端发送内容。8×3 → 20×5 证明尺寸约束差异可发生，但不能单凭这一点认定它就是用户当前 Codex 长输入问题的唯一根因。

| 编号 | 优先级 | 待办 | 状态 |
| --- | --- | --- | --- |
| TD-001 | P1 | 顶部显示每秒通信字节数和连接质量格子 | [ ] 待实施 |
| TD-002 | P0 | 长输入在可用终端宽度内换行，输入框边界不越出屏幕 | [ ] 待复现、定位及修复 |
| TD-003 | P1 | 移除右上角“经典版”入口及失效提示 | [ ] 待实施 |

### TD-001：顶部通信速率与连接质量

检查事实：

- [`workspace.py`](../src/supermonoagent/workspace.py) 的 `.titlebar`、`#connect-button`、`#connection-dot`、`#connection-label` 当前只显示连接文字和圆点。
- [`static/workspace.js`](../src/supermonoagent/static/workspace.js) 的 `metrics` 已有累计 `transmittedBytes`、`receivedBytes`、`lastPongMs` 和 `reconnects`，但没有每秒速率采样和顶部速率显示。
- `send()` 和 `input()` 累加发送的二进制帧；`handshake()`、中继的 JSON 入房消息走直接 `ws.send()`，未进入该发送计数。接收统计目前只累计二进制帧，未累计文本消息。
- `sync()` 每 15 秒发送一次应用层 ping，`ws.onmessage` 的 OP 11 更新最近 RTT；`updateFooter()` 目前只把 RTT 放到页脚。

登记的修改：

- [ ] 在连接文字旁增加至少 `↑ 发送速率`，按 `B/s`、`KiB/s`、`MiB/s` 显示；建议同时增加 `↓ 接收速率`。
- [ ] 采用约 1 秒的字节增量采样，除以实际经过的时间；明确统计的是当前连接的应用消息吞吐量，不是物理链路带宽上限或运营商网速。
- [ ] 统一二进制、文本、握手、心跳等消息的计数口径，避免遗漏或重复统计；只记字节数，不记录消息正文或 Token。
- [ ] 增加类似手机信号的 0–4 格连接质量图标。依据连接状态、RTT、心跳新鲜度和重连情况分级，阈值在实施时统一定义；不得把传输量大小当成连接质量，也不声称能读取真实无线信号强度。
- [ ] 空闲时正常显示 `0 B/s`；连接中、离线、重连中和质量数据未知时分别显示正确状态。
- [ ] 工作区切换、重连时重设采样基线，防止上一台电脑的流量混入；计时器随连接生命周期和页面退出清理，避免重复定时器。
- [ ] 同时清空或作废上一连接的 `lastPongMs`，记录响应时间戳；新连接尚未测得 RTT 时显示“未知”，不沿用旧电脑的质量格数。区分全应用累计诊断计数与当前连接的显示采样。
- [ ] 修正 4001 验证失败的顶部状态：既然不自动重试，就不能继续显示“重连中”；4004 电脑未上线且仍在重试时应采用不同状态。增加相应状态转换回归。
- [ ] 根据当前电脑／连接目标区分本机、远端直连和中继连接，避免仅按有无房间码把远端直连显示成本机。
- [ ] 顶部右侧保留连接设置的点击行为。桌面和窄屏下状态文字、速率及格子不互相覆盖，不挤出页面。

代码落点：

| 文件 | 待改位置／职责 |
| --- | --- |
| [`workspace.py`](../src/supermonoagent/workspace.py) | 顶部状态区 DOM、速率与质量图标的可访问标签 |
| [`static/workspace.js`](../src/supermonoagent/static/workspace.js) | `metrics`、`send()`、`handshake()`、`input()`、`connect()`、`ws.onmessage/onclose`、`sync()`、`switchHost()`、退出清理；新增统一采样及显示更新 |
| [`static/workspace.css`](../src/supermonoagent/static/workspace.css) | `.titlebar`、`#connect-button`、连接文字及新状态组件的布局与窄屏规则 |
| [`tests/workspace-client.test.cjs`](../tests/workspace-client.test.cjs) | 速率换算、空闲、时间间隔、质量分级和采样重置的单元回归 |
| [`tests/test_workspace_hosts.py`](../tests/test_workspace_hosts.py) | 切换电脑后统计不串台；连接设置仍可打开 |

验收：给定已知字节数和采样时间可准确计算 B/s；握手与中继消息统计口径一致；空闲不显示为断线；切换、断线和重连无负速率或历史尖峰；不同窗口宽度下顶部不溢出。

### TD-002：长输入换行与输入框边界

用户现象：输入较长文字后，文本及其边框向屏幕右侧延伸，没有在屏幕边缘自然换行。

检查事实与证据边界：

- 新版实际输入由 xterm 经 `term.onData → terminalInput() → input()` 发送到后台，进入真实 PTY，并不是普通网页文本框。
- [`static/workspace.js`](../src/supermonoagent/static/workspace.js) 的 `fitPane()`、`ResizeObserver` 和尺寸接管逻辑负责终端行列；`drainPane()` 负责按后台尺寸回放。当前后端尺寸会限制在 20–500 列、5–200 行，见 [`hub.py`](../src/supermonoagent/hub.py) 的 `resize_session()`；[`pty.py`](../src/supermonoagent/pty.py) 的 `WinPty.resize()` 再调用 `setwinsize(rows, cols)`。
- [`static/workspace.css`](../src/supermonoagent/static/workspace.css) 的 `#stage`、`.split-child`、`.pane`、`.terminal-screen` 使用受限尺寸及溢出裁切；标签等界面文本另有 `white-space:nowrap` 规则。这些规则存在，不能据此直接判定终端长输入的根因。
- 现有 [`test_responsive_layout.py`](../tests/test_responsive_layout.py) 和 [`test_frontend.py`](../tests/test_frontend.py) 主要验证页面几何尺寸、PTY 尺寸同步及接管，不能替代长输入换行的验收。
- **Inference：** 需要区分网页容器真的越界、PTY/xterm 列数不一致，以及 Codex/PowerShell 自身的输入编辑／终端换行模式。此次未向用户现有终端输入测试文本，具体根因尚未复现确认。

登记的修改：

- [ ] 先在隔离的测试会话复现长中文、连续无空格英文／数字、多行粘贴和中文 IME 输入，分别检查 Codex、PowerShell 的输入区。
- [ ] 同步记录容器可用宽度、xterm `cols/rows`、后台确认的 `controls`、光标位置和换行状态；区分应用关闭自动换行的模式与错误尺寸。
- [ ] 覆盖小于 20 列／5 行的窄分屏，确认前台拟合尺寸与后台约束后的有效尺寸一致；结合容器布局确定最小可用区域或其他明确的适配办法，不能让两端长期使用不同网格。
- [ ] 区分“请求尺寸”与“服务端确认尺寸”，核对 `pane.lastSize` 的缓存时机；处理后台限幅、未取得租约或其他客户端接管时的确认结果。保持共享观看与用户主动接管的边界，避免多个客户端反复抢尺寸。
- [ ] 若是布局越界，修正具体容器的收缩约束及允许换行的界面文字；不依靠 `overflow:hidden` 隐藏问题。
- [ ] 若是终端／应用绘制问题，在 `fitPane()`、回放时序、尺寸确认或 PTY 适配边界修复；必要时记录并验证 Codex/PSReadLine 对终端宽度的处理。
- [ ] 保持固定单元格终端模型，不直接对 xterm 字符层施加 `word-break` 来代替终端换行，也不盲目强制改变应用的 VT 模式。
- [ ] 视觉换行不得向输入自动插入 `\r`、`\n` 或 Enter，避免改变命令内容或执行用户尚未提交的输入。

代码落点：

| 文件 | 待检查／可能修改位置 |
| --- | --- |
| [`static/workspace.css`](../src/supermonoagent/static/workspace.css) | `main`、`#stage`、`.split`、`.split-child`、`.pane`、`.terminal-screen`；输入相关 DOM 的宽度约束 |
| [`static/workspace.js`](../src/supermonoagent/static/workspace.js) | `createPane()`、`term.onData`、`drainPane()`、`fitPane()`、`takeControl()`、`openView()`、窗口 resize 与尺寸确认 |
| [`static/terminal-client.js`](../src/supermonoagent/static/terminal-client.js) | `terminalInput()` 与快照处理边界；核对软换行不改变发送字节 |
| [`hub.py`](../src/supermonoagent/hub.py)、[`pty.py`](../src/supermonoagent/pty.py) | 尺寸租约、有效行列限制、`WinPty.resize()`；仅在前后端证据指向此处时修改 |
| [`tests/test_responsive_layout.py`](../tests/test_responsive_layout.py)、[`tests/test_frontend.py`](../tests/test_frontend.py)、[`tests/workspace-client.test.cjs`](../tests/workspace-client.test.cjs) | 新增长输入的实际换行、光标、边界及原始输入字节回归 |

验收：在 390、850、1440、1920 px 宽度及分屏中，输入超过一屏宽的中文／连续英文数字，输入区边界不越界，文字按当前有效宽度换行；补测 19／20／21 列、4／5／6 行的约束边界及浏览器缩放／Windows DPI 场景；缩放后光标、边框与 Codex 底栏正确；退格、方向键、中文组合输入和粘贴正常；仅输入而未按 Enter 时不执行命令。需分别验证桌面 WebView2 和浏览器。

### TD-003：移除右上角“经典版”

检查事实：

- [`workspace.py`](../src/supermonoagent/workspace.py) 顶部包含 `<a href="/legacy" class="legacy">经典版 ↗</a>`。
- [`static/workspace.css`](../src/supermonoagent/static/workspace.css) 有 `.legacy` 的样式和媒体查询。
- [`static/workspace.js`](../src/supermonoagent/static/workspace.js) 的 `sync()` 在协议不兼容时仍提示“使用右上角经典版”，删除按钮后该提示会失效。

登记的修改：

- [ ] 删除新版顶部的“经典版”链接，右上区域只展示当前连接状态、通信速率及连接质量。
- [ ] 清理专属于 `.legacy` 的样式；保留与其并列选择器下其他仍需使用的窄屏样式。
- [ ] 更新协议不兼容提示，不再指引用户点击已移除的按钮；保留清楚的后台升级／切换工作区提示。
- [ ] 此项范围是移除新版入口，`/legacy` 路由和既有经典客户端服务保持可用，避免影响现有链接和会话。
- [ ] 实施时同步修订 [`docs/workspace-hosts.md`](workspace-hosts.md) 等“右上角进入经典版”的说明。

代码落点：`workspace.py`、`static/workspace.css`、`static/workspace.js::sync()`；回归落点为 [`tests/test_workspace.py`](../tests/test_workspace.py) 及 [`tests/test_client_routes.py`](../tests/test_client_routes.py)。

验收：桌面与窄屏右上角均无“经典版”入口，连接设置仍能使用；错误提示无失效入口；顶部状态与速率不越界；原经典路由测试继续通过。

### 后续实施顺序

先为 TD-002 建立长输入复现与回归，再处理终端换行；TD-001 与 TD-003 共用顶部 DOM，可在同一轮顶部调整中完成。用户要求当前仅登记，三个事项均保持未勾选，不启动实施或发布。

### 共同验收与发布门槛（依附上述三项）

- [ ] 在隔离会话完成回归，保留用户现有 daemon、Kimi、Codex 进程；不把给真实用户会话自动发送长文本当成无副作用测试。
- [ ] 修改后检查源码与最终交付的 HTML／JS／CSS 一致。`workspace.py` 页面嵌入打包代码，需要重新打包后验收实际 EXE；只覆盖静态 JS 不能代替完整发布验证。
- [ ] 用 [`frontend.py`](../src/supermonoagent/frontend.py) 的界面 revision 机制确认客户端连接到对应版本的 frontend，避免旧 HTML 搭配新 JS。核实 [`desktop.py`](../src/supermonoagent/desktop.py)、[`scripts/build-windows.ps1`](../scripts/build-windows.ps1) 的桌面／浏览器入口使用一致的新版工作区。
- [ ] 实测最终包的顶部状态、通信速率、长输入、页签配色、房间码工作区切换及关闭视图保留后台会话；同步使用说明。文档不把已有几何尺寸测试通过等同于长输入问题已修复。

## 2026-10-21：Kimi 黏贴与 Codex 输入栏重绘

状态：**已完成代码、自动化回归和独立 Windows 包验证。** 未重启 daemon，也未向现有 Kimi/Codex 会话注入测试输入；当前运行实例仍使用旧包，需用户自行选择何时切换到 `dist/windows-paste-fix`。

### 已确认事实

- 本机版本：Kimi CLI 2.3.0、Codex CLI 0.163.0。
- 前端 `terminalInput()` 不会剥离 bracketed paste；`tests/workspace-client.test.cjs` 已确认 `ESC[200~...ESC[201~` 会原样进入后台。
- Kimi 支持 bracketed paste，但 Windows ConPTY 会剥离标记；Codex 对无标记的快速黏贴流兼容更好，因此表现为“Codex 可以、Kimi 不行”。
- Codex 输入栏使用 ratatui/crossterm 的 diff 渲染和 inline viewport。重连或重开视图后只重放有限原始输出，无法可靠恢复光标、滚动区域和输入栏状态；之后只发变化单元格，旧画面可能持续存在。
- 尺寸变化会迫使 Codex 全量重绘；当前 workspace 在打开、点击、窗口尺寸变化时可能自动接管尺寸，需避免多视图反复抢租约。

### TD-004：Kimi bracketed paste 兼容

| 编号 | 优先级 | 待办 | 状态 |
| --- | --- | --- | --- |
| TD-004A | P0 | 在隔离会话记录单行、中文、多行黏贴到达 daemon 的实际字节 | [x] 已由单元回归和真实 CLI 探针覆盖 |
| TD-004B | P0 | 在 [`pty.py`](../src/supermonoagent/pty.py) 的 `WinPty.write()` 前为 ConPTY 解包 bracketed paste | [x] 已完成 |
| TD-004C | P0 | 增加 Kimi 形态输入回归：短 ASCII、中文、多行、跨包标记和换行不提前提交 | [x] 已完成 |

要求：只在 Windows/ConPTY 路径处理；不盲目过滤其他 CSI；黏贴只写正文，不向输入自动追加 Enter；保留 Codex 当前可用行为。

### TD-005：Codex 输入栏全量重绘与尺寸竞争

| 编号 | 优先级 | 待办 | 状态 |
| --- | --- | --- | --- |
| TD-005A | P0 | 在 [`static/workspace.js`](../src/supermonoagent/static/workspace.js) 的接管、重连或快照回放完成后触发一次受控全量重绘 | [x] 已完成；另有“刷新”按钮 |
| TD-005B | P1 | 收敛 `openView()`、`activate()` 和浏览器 resize 中的自动接管，避免相同尺寸反复请求 | [x] 已完成 |
| TD-005C | P1 | 统一 [`hub.py`](../src/supermonoagent/hub.py) `resize_session()` 与 `resize()` 的行列上限 | [x] 已由共享 `_terminal_size()` 完成 |
| TD-005D | P0 | 增加 Codex 形态 diff 输出、截断快照、重新订阅和尺寸竞争的回归 | [x] 已覆盖重开视图、尺寸弹跳和最终尺寸恢复 |

要求：不能把“扩大历史缓冲”当成完整 VT 状态恢复；若采用尺寸弹跳强制重绘，必须限频、只在接管或用户明确刷新时触发，并避免影响正在输入的内容。

### 本轮共同验收

- [x] 运行 Python 与 JavaScript 全量测试：96 项 Python、6 项子测试、12 项 JavaScript 通过。
- [x] 在隔离环境验证 Kimi/Codex 真实 CLI 黏贴探针和 Codex 尺寸弹跳重绘；未向用户当前会话发送测试内容。
- [x] 已重建 `dist/windows-paste-fix`；26 个源码哈希全部匹配，EXE 校验值匹配且 CLI 可启动。
- [x] 已更新本文件；现有 daemon、Kimi、Codex 进程均未被擅自终止。

## 2026-10-21：全局桌面工作区互联

状态：**仅完成分析并登记，尚未实施。** 详细分析见 [`global-workspace-analysis.md`](global-workspace-analysis.md)。目标是把当前“桌面/手机保存多台电脑但逐台切换”升级为“同时连接多台电脑，并统一显示和操作各电脑的终端”。

### 分析结论

- 底层 daemon、relay、房间码、机器/项目 identity、多会话订阅和 per-host Token 已具备。
- 当前缺口主要在客户端：桌面 workspace 与 Android 都是单 WebSocket 切换模型；同时连接时需要按 `hostId:sessionId` 区分会话，避免不同电脑撞号。
- relay 已有 `/health` 项目/主机目录，但目录当前无鉴权地暴露房间码、机器名和项目名；接入发现前必须收紧。
- 第一阶段可以不改 daemon、relay 转发协议或终端帧格式，只做桌面与 Android 的并行聚合。

### TD-006：全局桌面工作区互联

| 编号 | 优先级 | 待办 | 状态 |
| --- | --- | --- | --- |
| TD-006A | P0 | 将 `workspace.js` 的单连接状态抽成 per-host `HostConnection`，每台电脑独立保存 ws、catalog、controls、重连、速率和错误状态 | [ ] 待实施 |
| TD-006B | P0 | 将会话内部键改为 `hostId:sessionId`，输出、输入、订阅、尺寸租约、刷新和关闭视图均按 host 分流 | [ ] 待实施 |
| TD-006C | P0 | 侧栏与页签按“项目 → 电脑 → 分组 → 会话”聚合显示，每台电脑显示独立在线状态、RTT、速率和协议版本 | [ ] 待实施 |
| TD-006D | P0 | Android 页面改为多 host 目录并行感知；仍只渲染一个活动终端，未打开会话不订阅大输出 | [ ] 待实施 |
| TD-006E | P1 | 为 relay 主机目录增加鉴权或“已知房间在线状态”接口，再把发现/选择电脑接入 workspace 增加工作区对话框 | [ ] 待设计 |
| TD-006F | P1 | 增加主机上下线推送或低成本目录订阅，避免客户端轮询 `/health` | [ ] 待设计 |
| TD-006G | P1 | 设计跨主机统一身份/Token 分发；不得以共享明文 Token 换取便利 | [ ] 待设计 |
| TD-006H | P0 | 双隔离 daemon + 共享 relay 回归：相同 session id、并行输出、定向输入、单 host 断连/验证失败不影响另一台 | [ ] 待实施 |
| TD-006I | P1 | 评估多 host 心跳、输出订阅、手机耗电和内存；默认只给打开的终端订阅输出 | [ ] 待实施 |
| TD-006J | P1 | 更新 `protocol.md`、`architecture.md` 的安全现状和连接模型，使其与当前 token 校验及聚合设计一致 | [ ] 待实施 |

### 实施顺序与验收

1. 先做 TD-006A/B/C/H，形成桌面端双主机并行聚合 MVP；不改 relay。
2. 再做 TD-006D 的 Android 聚合目录与活动终端切换。
3. 发现、目录推送和统一身份必须先完成安全设计，再进入实施。
4. 验收至少覆盖：两台电脑同 session id 不串画面；双向输入路由正确；一台离线或 Token 错误不影响另一台；每台电脑状态独立；关闭视图不结束后台；手机和桌面看到的电脑/项目目录一致。
5. 生产启用前必须完成 TLS、Origin 校验、Token 移出 URL 查询串及 relay 目录鉴权。
