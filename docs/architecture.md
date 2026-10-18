# 代码说明

SuperMonoAgent：Windows 上的单进程主机，一个进程接住多窗口、多条 CLI，网页和手机经直连或 WebSocket 中继进入每个窗口。

## 进程与线程模型

```
daemon（asyncio 单事件循环，主线程）
├── Hub：窗口/分组/会话/客户端登记处
├── 每条 CLI 会话一个读线程（hub._read，阻塞在 pty.read）
├── 每个外联中继一个 asyncio task（relay_uplink）
└── websockets server：/ws 数据口 + / 与 /static 的 HTTP 口

relay（独立进程，asyncio 单事件循环）
└── 按房间转发 host ↔ phone，不解码终端内容
```

- 读线程把 PTY 字节交给 `OutputPump` 合并，`loop.call_soon_threadsafe` 跳回事件循环发送。除 PTY 读写外没有共享可变状态；Hub 内部用一把 `threading.Lock` 保护 clients 字典。
- relay 是纯 asyncio，无线程。

## 模块

| 模块 | 职责 |
|------|------|
| `protocol.py` | 长度前缀帧的打包/解包、15 个 opcode、JSON 负载、UTF-8 安全截断 |
| `accounts.py` | 本机账户。PBKDF2(100k, sha256) 存密码，随机 token；注册/登录/换 token。数据在 `~/.supermonoagent/accounts.json` |
| `hub.py` | 核心领域。窗口、分组、会话、客户端；开/关 CLI、选窗/聚焦、输出扇出、目录广播、桌面状态持久化 |
| `pty.py` | ConPTY 封装（依赖 pywinpty）。powershell/pwsh 启动时注入 PSReadLine 配色；`shell_command` 把预设命令行解析成 argv |
| `pump.py` | 输出合并器。20ms 窗口合并、128KiB 待发上限、256KiB 环形快照缓冲，溢出改发全量 |
| `daemon.py` | 主机进程。WS 服务、opcode 分发、外联中继管理、token/房间码生成、账户握手端点 |
| `relay.py` | 中继进程。房间（host + phones）、编号转发、控制消息、/health |
| `web.py` | 单文件页面：HTML/JS 内嵌（`PAGE`）+ xterm.js 静态资源（`static/`）。既是 daemon 的页面，也是 relay 的页面 |

## 数据流

**输出**（daemon）：

```
PTY → 读线程 → pump.remember（环形缓冲）/ pump.stage（20ms 合并）
    → hub._flushed → _feed_targets 找订阅者
        ├── 直连客户端：只收自己正在看的那条会话，原始字节
        └── 中继客户端：收频道内全部会话，帧头加 session_id\0
    → 客户端 asyncio 队列（32 帧，满丢最旧）→ websockets 发出
```

**输入**（daemon）：`OP_INPUT` 原始字节 → `hub.write` → 该客户端当前视图的 PTY。键盘只进正在看的那条 CLI，是硬约束（`_view_locked`）。

**中继**：phone 的消息前补 `<u32 phone_id>` 转给 host；host 下行同样带编号，relay 按编号投递到对应手机的队列（32 帧，满丢最旧）。relay 不理解帧内容。

**页面**：两条连接。

- **control**（每页面一条）：收 OP 2/7 同步目录、发开窗口/关窗口/分组管理等控制帧。中继模式下收到 `ready` 后发 HELLO。
- **每 pane 一条**：选定某条 CLI 后建立，收 OP 3/8 画终端、发 OP 4 按键和 OP 5 尺寸。直连时首帧 HELLO 带 `window`；中继时每 pane 都收频道内所有会话，靠帧头会话号过滤。

## 状态文件（`~/.supermonoagent/`）

| 文件 | 内容 | 写入方 |
|------|------|--------|
| `token` | 本机 token，首次启动随机生成 | daemon |
| `relay-code` | 6 位十六进制房间码 | daemon |
| `accounts.json` | 账户（盐、密码哈希、token） | accounts |
| `desktop.json` | 分组（名称+频道）和命令预设 | hub |
| `relays.json` | 外联中继列表（name/url/code） | daemon.set_relays |

## 关键不变量

- 终端字节流只在 daemon 内部解码标题（`short_title`），relay 全程透传。
- 直连帧不带会话号；会话号只加在中继帧头上。
- 中继客户端必须声明频道（OP 1 的 `channel`），只收 `分组.channel == channel` 的窗口输出（`hub._channel_ok`）。
- 关窗口/关会话会真杀进程（`pty.terminate(force=True)`）。
- 坏帧、坏 JSON、未知 op 一律忽略，不影响连接。
- 页面重连：pane 断开 1.5s 后重建；control 断开 2s 后重建。

## 配置入口

- `daemon.main`：`--host`（默认 127.0.0.1）、`--port`（6769）、`--relay`（追加一台中继）、`--code`（覆盖房间码文件）。
- `relay.main`：`--host`（默认 0.0.0.0）、`--port`（6770）。
- 命令预设和 Alt 快捷键在页面"设定"里改，存 `desktop.json`；默认 powershell/pwsh/cmd/claude/codex 五组（`hub.DEFAULT_PRESETS`）。

## 测试

`tests/` 下按模块对应：`test_protocol.py`（帧编解码）、`test_accounts.py`、`test_pump.py`、`test_relay.py`、`test_windows.py`、`test_pty.py`。用 `.venv` 里的 pytest 跑。

## 依赖与坑

- `websockets` 17：HTTP 响应要用 `connection.respond(status, text)` 再改 `Content-Type` 头（`web.text_response`）。
- Windows 依赖 `pywinpty` 提供 ConPTY。
- 详细线协议见 `docs/protocol.md`；安全现状（当前不验 token）见该文档末节，恢复收紧的落点也在那里。
