# 架构与代码导航

TD-008 更新：新增 `relay_identity.py`，以 SQLite 保存统一中继账号、房间 grants 和哈希短期 session。`relay.py` 负责认证目录及 phone 授权，`daemon.py::RelayBindings/client_authorized/delegated_client` 将受信上联账号绑定到明确的本机终端所有者，`hub.py` 在操作和排队输出前检查独立授权。Windows workspace 保存按中继 endpoint 绑定的会话，自动选择电脑时使用账号模式；原来的每主机长期 Token 模式独立保留。见 [账号配置与迁移](relay-account-setup.md)。这些源码能力不表示公网服务器已经升级。

当前实现由 daemon 持有 Windows PTY，桌面和 Android 聚合各台已授权电脑的目录，并把输入、输出和尺寸操作路由回所属 daemon。本文按 2026-10-22 仓库源码更新；不表示已部署到任何远端生产服务。二进制字段、认证首帧和目录 watch 的完整约定见 [protocol.md](protocol.md)。

## 进程与所有权

```text
daemon A ── ConPTY A1/A2 ── Hub A
       ╲                     ╱
        relay（已登记房间 A、B；host ↔ phone 路由；认证目录 watch）
       ╱                     ╲
daemon B ── ConPTY B1/B2 ── Hub B

桌面：每台电脑一个终端 WebSocket + 每个 relay 一个已知房间 watcher
Android：每台电脑一个目录/控制连接 + 当前终端一个 xterm 渲染器
本机 frontend：当前 UI → 固定 loopback daemon，保留已运行的 PTY
```

daemon 是 asyncio 主事件循环；每条 CLI 一个阻塞 PTY 读线程，输出通过 `OutputPump` 和 `call_soon_threadsafe` 回到事件循环。定向输入与 resize 通过 `asyncio.to_thread` 执行，并在会话 `io_lock` 内重新检查订阅/客户端状态，避免关闭或重连与阻塞 ConPTY 操作交叉。relay 是独立 asyncio 进程，不拥有 PTY。

来源：[daemon.py:_op/relay_uplink](../src/supermonoagent/daemon.py)、[hub.py:_read/write_session/resize_session](../src/supermonoagent/hub.py)、[pty.py](../src/supermonoagent/pty.py)、[pump.py](../src/supermonoagent/pump.py)。

## 模块边界

| 模块 | 责任 |
|---|---|
| [protocol.py](../src/supermonoagent/protocol.py) | 长度帧、19 个 opcode、能力标志、JSON、会话前缀与有界截断 |
| [accounts.py](../src/supermonoagent/accounts.py) | 每个 daemon 的本机账户、PBKDF2 密码 verifier 与访问 Token |
| [hub.py](../src/supermonoagent/hub.py) | 分组/窗口/CLI/客户端、按 Token 所有权过滤、显式订阅、尺寸租约、历史回放 |
| [pty.py](../src/supermonoagent/pty.py) | pywinpty/ConPTY、命令启动、输入过滤与原生终端 I/O |
| [pump.py](../src/supermonoagent/pump.py) | 有界环形历史、输出合并和输出位置 |
| [daemon.py](../src/supermonoagent/daemon.py) | 认证、opcode 分发、HTTP、上联、身份与 Token 发布 |
| [relay.py](../src/supermonoagent/relay.py) | owner enrollment、phone 验证、路由、撤销、已知房间推送目录 |
| [security.py](../src/supermonoagent/security.py) | 共享 Host/Origin 检查、TLS 配置及认证时限 |
| [frontend.py](../src/supermonoagent/frontend.py) | 固定 loopback 上游的新版 UI 适配；旧 daemon 配色/输入兼容 |
| [workspace.py](../src/supermonoagent/workspace.py)、[workspace.js](../src/supermonoagent/static/workspace.js) | 桌面多主机树、标签/分屏、每主机连接状态、owner 绑定操作 |
| [terminal-client.js](../src/supermonoagent/static/terminal-client.js) | 共用字节帧、meter、输入过滤、尺寸与 pane 资源回收 |
| [web.py](../src/supermonoagent/web.py) | 兼容页面和统一静态资源路由；桌面 workspace 是独立入口 |
| [Android mobile app.js](../android/assets/mobile/app.js) | 多主机目录、当前终端、按 host/session 隔离的草稿与交互状态 |
| [MainActivity.java](../android/src/dev/supermonoagent/mobile/MainActivity.java)、[LocalAssets.java](../android/src/dev/supermonoagent/mobile/LocalAssets.java) | Android 连接设置、WebView 生命周期、同 endpoint Origin 的本地资源装载 |

## 终端数据流

**输出**：`PTY → 读线程 → pump 记忆/合并 → Hub 按所有权与订阅筛选 → 每客户端队列 → WS → 所属 pane 的 xterm.write(bytes)`。

multiplex 客户端的 OP3/OP8 无论直连或中继，都包含 `raw_session_id + NUL`。仅被 OP16 订阅的会话发送输出；未打开的目录项不持续复制输出。订阅快照保留输出位置，用于去除 pending batch 与快照的重复区段。旧非 multiplex 直连保留当前单视图的无前缀输出，旧 relay 频道模式保留兼容路径。

**输入**：桌面 pane 持有所属 `HostConnection` 与 generation，构造 OP17 的原始会话 ID 与按键字节；Hub 再检查会话存在、仍在该连接订阅集、Token 所有权与频道权限，最后写 PTY。OP4 只用于旧单视图客户端。relay 的 host 链路另加四字节 phone ID，客户端本地 host 元组键不会进入协议。

**背压**：队列深度 32、环形历史 256 KiB、待发上限 128 KiB；默认合并 20ms，multiplex 订阅时降至不高于 4ms。`reliable:true` 在溢出时明确关闭慢连接并重新请求有界历史，避免静默丢失 ANSI 片段；它不是永久日志或端到端序号确认系统。旧非 reliable 路径仍保留丢最旧帧/快照兼容策略。

来源：[hub.py:_feed_targets/_feed_frame/_session_snapshot/_flushed/_send](../src/supermonoagent/hub.py)、[daemon.py:_op](../src/supermonoagent/daemon.py)、[relay.py:_phone/_enqueue](../src/supermonoagent/relay.py)。

## 客户端多主机模型

桌面 `HostConnection` 独立保存 socket、generation、目录/分组、身份、controls/能力、RTT/速率 meter、心跳、重试、错误和订阅签名。本机及保存的电脑同时连接，一台 Token 错误或断线只暂停该电脑。当前选中电脑决定工具栏显示与新建目标；标签与 pane 自己保存真实 owner。

pane、窗口和分组在客户端用 `JSON.stringify([hostId,rawId])` 形成不透明键，不通过冒号拼接/拆分。故 host ID 自带冒号或两台电脑拥有相同八位会话 ID 都不会碰撞。socket 回调、resize/重绘、pane 输入、配色、重命名/结束等操作核对所属连接 generation；打开的对话框不能在切换电脑或重连后把操作发给其他 authority。

侧栏按项目 → 电脑 → 分组 → 会话显示，电脑各自显示连接、RTT、速率和实际协议能力；标签包含所属项目/电脑。选中的初始电脑打开全部既有终端标签；其他电脑先保持目录连接，在选中后打开其标签。打开的隐藏标签保留解析器和订阅，关闭标签回收 xterm/observer/GPU 并取消订阅。**关闭视图或移除电脑不结束后台进程；明确“结束”才发 OP9。** 桌面最多 32 台电脑（含本机）。

每个 pane 采用服务器确认的 grid；观察者缩放显示，不自动夺取其他客户端尺寸租约。现代 daemon 的 resize ACK 按请求编号确认，旧服务用 PING 顺序屏障。有限尺寸往返重绘受输入、IME 和频率限制，避免改写用户草稿。

Android 同样维护各主机连接/目录与不透明 host/session 键，但只渲染当前会话；切换终端取消旧输出订阅并保留草稿/模式。页面/Activity 后台时释放连接与定时器，回前台恢复目录。WebView 通过 endpoint Origin 装载本地资源，不依赖服务器全局允许 `Origin:null`。

来源：[workspace.js](../src/supermonoagent/static/workspace.js)、[terminal-client.js](../src/supermonoagent/static/terminal-client.js)、[Android app.js](../android/assets/mobile/app.js)、[MainActivity.java](../android/src/dev/supermonoagent/mobile/MainActivity.java)、[LocalAssets.java](../android/src/dev/supermonoagent/mobile/LocalAssets.java)。

## 认证、目录与传输边界

`Daemon.allowed()` 实际校验 daemon 主 Token 或本机账户 Token；目录和窗口操作还受 Hub 的 Token 所有权限制。仅主 Token 可修改预设、上联和项目身份。relay phone 也必须提供在线 host 发布的有效访问 Token。

relay 的主机注册具有独立权限：先通过 `--enroll ROOM` 把 owner secret 的 verifier 持久化，再把原 secret 配给对应 daemon。终端访问 Token 不能认领房间。重复 host、未登记或错误 owner secret 不得覆盖在线房间。只有显式 `--development` 的 loopback 测试服务允许本机 fixture 首次声明。

每个 relay 的目录 watcher 提交已知 `(code,token)` 集合，最多 64 项，接收带 revision 的在线身份推送。未知、错误凭据、离线不可验证项只返回 `online:false`，不返回机器/项目，也不创建房间。当前实现不保留离线身份。Token 更新立即重新计算目录并替换旧队列；被撤销 phone 的待发帧和 sender 被移除，再关闭连接。目录 socket 不承载终端输出；旧 relay 不支持 watch 时各终端连接仍可工作。

`/health` 不再提供公开房间、机器、项目或默认 Token，只用于存活检测；frontend 健康响应还有自身 backend/revision。浏览器 Token 走首个 HELLO，不放 WS 查询串；桌面启动 fragment 在读取后移除。桌面 Token 默认保存在 sessionStorage，持久化需明确勾选，且绑定规范化 endpoint/room；编辑目标会清除旧凭据。每台 daemon 的账户和 Token 仍是独立权限源，这不是统一登录或跨主机共享主密钥。

共享 Host/Origin 检查拒绝恶意 authority、错误 Origin scheme/host/port 和 `Origin:null`；公网域名/Origin 需要精确配置。无 Origin 原生客户端仍要认证。服务端支持证书/私钥与最低 TLS1.2；daemon/relay 公网监听要求 TLS 或显式可信 TLS 代理设置及 HTTPS Origin。上联远端 relay 必须用 `wss://` 并正常验证证书。relay 仍是看到访问凭据和转发内容的信任方，当前不提供 E2EE。

来源：[security.py](../src/supermonoagent/security.py)、[daemon.py:allowed/_op/serve_http/relay_uplink](../src/supermonoagent/daemon.py)、[relay.py:enroll/handler/_watch/_replace_tokens/serve_page](../src/supermonoagent/relay.py)、[frontend.py:handler/serve_http](../src/supermonoagent/frontend.py)。远端部署需另行完成证书、Origin、owner enrollment 与凭据迁移；本次源码更新不会重启已运行的生产服务。

## 持久化与配置

daemon 状态默认位于 `~/.supermonoagent/`，隔离实例可用 `--state-dir` / `SUPERMONO_STATE_DIR`。目录中的凭据是运行时秘密，不应提交仓库。

| 文件 | 内容 / 写入方 |
|---|---|
| token | daemon 主 Token；首次启动生成 |
| relay-code | 本机默认房间码 |
| accounts.json | 本机账户密码 verifier 与访问 Token |
| desktop.json | 分组/频道/所有权与命令预设；不等价于可恢复的活 PTY |
| machine.json | 项目身份；机器名重新读取本机 hostname |
| relays.json | 上联 name/url/code 和可选 owner_secret |
| relay-owners.json | relay 登记 verifier；可用 `--enrollment-file` 指定其他路径 |
| frontend-themes-*.json | 旧 daemon 的本机配色适配记录 |

daemon 默认监听 loopback:6769；relay 默认端口 6770，但公开监听须满足上述传输门禁；frontend 固定监听 loopback，自动复用 6771–6788 内匹配 backend/revision 的服务。TLS/Origin 参数为 `--tls-cert`、`--tls-key`、可重复 `--allowed-origin` 和 `--reverse-proxy-tls`。daemon 上联可用 `--relay`、`--code`、`--relay-owner-secret`；隔离测试用 `--no-relay` 避免读取保存的外联配置。

来源：[daemon.py:main/load_relays/load_identity](../src/supermonoagent/daemon.py)、[hub.py:load_desktop](../src/supermonoagent/hub.py)、[accounts.py](../src/supermonoagent/accounts.py)、[relay.py:main](../src/supermonoagent/relay.py)、[frontend.py:local_page_url](../src/supermonoagent/frontend.py)。

## 验证入口与边界

- [test_client_integration.py](../tests/test_client_integration.py)：隔离 daemon/relay、字节流与真实 shell 路径。
- [test_desktop_multihost.py](../tests/test_desktop_multihost.py)：同 relay 两台 daemon、相同 raw ID、并行 I/O、独立 native grid、错误 Token/断线/重连、过期对话框与凭据绑定。
- [test_security_directory.py](../tests/test_security_directory.py)：认证目录、owner enrollment、撤销、Host/Origin/TLS 与资源限制。
- [test_frontend.py](../tests/test_frontend.py)：本机兼容适配保留既有会话、初始全部标签、尺寸与配色。
- [test_workspace_runtime.py](../tests/test_workspace_runtime.py)、[test_resize_recovery.py](../tests/test_resize_recovery.py)：现代/旧版尺寸确认、观察者租约、重绘及草稿/IME 恢复。
- [test_android_multihost.py](../tests/test_android_multihost.py)：Android 文档多主机与生命周期自动化；实体设备触摸/输入法证据需单独采集。

构建成功、隔离浏览器与可信测试证书通过，不能替代远端实际部署验收或实体 Android 电池/输入法测量。实现边界与可重现测试应同时更新，避免把测试环境结论写成生产现状。
