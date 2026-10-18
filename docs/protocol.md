# 线协议

两种链路共用同一套帧格式，relay 只搬运不解码：

1. **直连**：浏览器 → 本机 daemon（`ws://127.0.0.1:6769/ws`）。
2. **中继**：手机/网页 → relay → daemon 的上联。daemon 主动向外连 relay，NAT 后可用。

实现位置：Python 在 `src/supermonoagent/protocol.py`，页面在 `src/supermonoagent/web.py` 顶部的 `pack/unpack/packJson`。

## 帧格式

```
| uint32 LE length | uint8 op | payload[length-1] |
```

- `length` = 1（op 字节）+ payload 长度，自身不含在 length 里。
- `MAX_FRAME = 256 KiB`，`pack` 超限抛 `ProtocolError`；`unpack` 校验长度合法且与实际字节数精确相等，否则抛错。收到坏帧直接丢弃，不断连。
- 未知 op 静默忽略（daemon 侧返回空）。
- 文本类控制用 JSON：payload 为 UTF-8 JSON 且必须是对象（`unpack_json`）。

### 截断规则

- `fit_payload`：超限时保留**最新**的字节，并跳过开头的 UTF-8 续字节（`10xxxxxx`），避免半个字符。
- `pack_feed`：中继帧的 payload = `session_id + "\0" + 数据`，空间不够同样从头部截断。
- 直连帧不带会话号前缀；只有 `client.key` 以 `relay-` 开头的客户端走 `pack_feed`（见 `hub.py` 的 `_feed_frame`）。原因是会话号直接拼进字节流会印到 PowerShell/cmd 提示符上。

## Opcode

| Op | 名称 | 方向 | Payload | 说明 |
|----|------|------|---------|------|
| 1 | HELLO | 客户端 → daemon | JSON `{token, window?, channel?}` | 首帧。可顺带选窗口、声明订阅频道 |
| 2 | HELLO_OK | daemon → 客户端 | JSON 全量状态 | 见下方"状态结构"。`error` 字段附在首次回复里 |
| 3 | OUTPUT | daemon → 客户端 | 终端字节 | 增量输出；中继帧带会话号前缀 |
| 4 | INPUT | 客户端 → daemon | 原始按键字节 | 写入该客户端当前正在看的会话，原样转发 PTY |
| 5 | RESIZE | 客户端 → daemon | JSON `{cols, rows}` | 列钳制 20–300，行钳制 5–80 |
| 6 | OPEN | 客户端 → daemon | JSON `{window?, shell, group?, cwd?}` | 开窗内 CLI；`window` 不存在时新建窗口 |
| 7 | SESSIONS | daemon → 客户端 | JSON 目录 | 窗口/分组/预设变化时广播 |
| 8 | SNAPSHOT | daemon → 客户端 | 终端字节 | 全量快照：选中/聚焦/订阅时发，输出溢出时也发 |
| 9 | CLOSE | 客户端 → daemon | JSON `{window?}` 或 `{session?}` | 关窗口或关单条 CLI（会终止进程） |
| 10 | PING | 双向 | 任意 | 回显 |
| 11 | PONG | daemon → 客户端 | PING 的 payload | 原样返回 |
| 12 | WINDOW | 客户端 → daemon | JSON `{title?, group?}` | 新建窗口并选中 |
| 13 | SELECT | 客户端 → daemon | JSON `{window}` | 切换当前窗口 |
| 14 | FOCUS | 客户端 → daemon | JSON `{session}` | 看同一窗口里的另一条 CLI |
| 15 | GROUP | 客户端 → daemon | JSON `{action, ...}` | 见下表 |

### OP_GROUP 的 action

| action | 附加字段 | 效果 |
|--------|----------|------|
| `create` | `name`, `channel?` | 新建分组；频道缺省用组名，重名自动加 `-2`、`-3` |
| `rename` | `id`, `name?`, `channel?` | 改分组名/频道 |
| `delete` | `id` | 删分组，窗口归 `default` |
| `move` | `window`, `group` | 窗口移到分组 |
| `subscribe` | `channel` | 声明订阅频道，立即补发一条快照 |
| `presets` | `presets` | 整单替换命令预设 |
| `title` | `window`, `title` | 改窗口标题 |
| `autotitle` | `window` | 按软件名+最近命令自动命名 |
| `relays` | `relays` | 整单替换外联中继列表（见"安全现状"） |

### 状态结构（OP 2 / OP 7 的 JSON）

```json
{
  "windows": [{"id", "title", "group", "focused", "sessions": [{"id", "shell", "cwd"}]}],
  "groups":  [{"id", "name", "channel"}],
  "presets": [{"id", "label", "command", "shortcut"}],
  "selected": "窗口id",
  "session":  "会话id",
  "service":  {"ok", "accounts", "windows", "sessions", "relays": [{"name", "url", "up"}]}
}
```

## 中继链路消息

relay（`src/supermonoagent/relay.py`）在 WebSocket 之上跑一套自己的小协议，文本 JSON 是控制、二进制是数据。

**握手**（连接后 2 秒内必须发出第一帧，否则关闭，code 4000）：

| 方向 | 内容 | 说明 |
|------|------|------|
| daemon → relay | `{"op": "host", "code": "...", "tokens": [...]}` | 同一房间只能有一个 host，重复连关闭 4003 |
| 页面 → relay | `{"op": "phone", "code": "...", "token": "..."}` | 房间没有 host 时关闭 4004 |

**控制消息**（双方随时可发）：

| 方向 | 内容 | 说明 |
|------|------|------|
| relay → host | `{"op": "ready"}` | host 握手成功 |
| relay → phone | `{"op": "ready"}` | phone 握手成功，页面收到后再发 OP 1 HELLO |
| relay → host | `{"op": "join", "id": N}` / `{"op": "leave", "id": N}` | 手机进出 |
| host → relay | `{"op": "tokens", "tokens": [...]}` | 全量刷新可用 token 列表 |

**数据**：双向都是 `| uint32 LE phone_id | 帧 |`。phone 的每条消息前由 relay 补上自己的编号再转给 host；host 下发同样带编号，relay 按编号找队列。relay 不解析帧内容，不做任何编解码。

**扇出**：daemon 侧给每台中继、每个 phone 建一个 `key = relay-<slot>-<phone_id>` 的客户端。中继客户端不走"只看当前会话"的逻辑，而是收**频道内所有窗口**的输出（`hub._feed_targets`），靠帧头的会话号前缀区分；页面用 `^[0-9a-f]{8}\0` 匹配前缀，不是本 pane 的会话就丢掉。

## 本机账户接口

不连中继、token 又不通过时（当前 `Daemon.allowed()` 恒真，此路径实际不会触发），daemon 只对 `127.0.0.1`/`::1` 开放一段一次性的 JSON 握手：

- 客户端发 `{"op": "register"|"login"|"rotate", "name": "...", "password": "..."}`
- 成功回 `{"op": "auth", "token": "...", "name": "..."}`，失败回 `{"op": "auth", "error": "..."}`，随后关闭
- 成功后 daemon 把新 token 列表推给所有已连中继（`{"op": "tokens", ...}`）

## 背压与丢弃

协议无序号、无确认，设计上允许丢帧：

- 每条客户端/手机连接一个 32 帧的队列，满时**丢最旧**的一帧再塞新帧（`hub._send`、relay `_enqueue`）。终端画面靠后续帧刷新，不追求字节可靠。
- daemon 侧每条会话的输出先经 `pump.py` 合并：20ms 窗口内的字节合成一帧；待发缓冲超 128 KiB 时清空，改发全量快照（OP 8）让订阅端重建画面。
- 每会话保留 256 KiB 环形缓冲，选窗/聚焦时从里取快照。

## 限额一览

| 项 | 值 | 位置 |
|----|----|------|
| 单帧上限 | 256 KiB | `protocol.MAX_FRAME` |
| daemon WS 消息上限 | 256 KiB | `daemon.main` |
| relay WS 消息上限 | 1 MiB | `relay.main` |
| 队列深度 | 32 帧，丢最旧 | `QUEUE_FRAMES` |
| 环形缓冲 | 256 KiB | `pump.RING_BYTES` |
| 待发合并 | 20 ms / 128 KiB | `pump.COALESCE_S` / `PENDING_BYTES` |
| 握手首帧超时 | 2 s | `relay.FIRST_FRAME_S` |
| 会话/窗口 id | 4 / 3 字节随机 hex | `hub.open_cli` / `open_window` |
| token / relay code | `secrets.token_urlsafe(18)` / `token_hex(3)` | `accounts`、`daemon.load_code` |

## 安全现状（已知缺口）

测试阶段的实现，**不要对不可信网络开放**：

- `Daemon.allowed()` 恒返回 `True`，任何能连到 daemon 端口的人都有完整读写权限（`daemon.py`）。
- relay 存了 `room.tokens` 但**从不校验** phone 的 token，知道 6 位十六进制房间码即可连入（`relay.py` 的 `_phone`）。
- 两处 WS 握手都不校验 `Origin`，浏览器里任意网页可尝试连本机 daemon。
- `OP_GROUP {"action": "relays"}` 可让 daemon 向任意 URL 发起外联，握手时会把全部 token 发给对方。
- relay 的 `/health` 返回所有活跃房间码。
- 全程明文 ws://，无 TLS；token 走 URL 查询串，会留在浏览器历史和日志里。
- 恢复收紧时的落点：`allowed` 改回校验本机 token + 账户 token，relay 的 `_phone` 加 token 校验，`relays` action 限本机。

## 与 Orca 官方协议的关系

官方 Orca Mobile 的线协议绑着 E2EE 握手和整套桌面 RPC，本项目没有实现，官方 App 连不上本主机。学习笔记见 `study/NOTES.md`。
