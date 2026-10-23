# 线协议

## 后续新增：统一中继账号（TD-008）

这部分实现于本轮源码，公网部署尚未执行。完整配置步骤见 [统一账号部署](relay-account-setup.md)。原有长期 Token/known-room 路径保持独立。

| 首帧文本操作 | 请求 | 响应/行为 |
| --- | --- | --- |
| `relay_login` | `{email,password}` | `relay_auth {session,expires,account:{id,email}}`；session 默认有效 1 小时 |
| `relay_logout` | `{session}` | 撤销该登录，先清目录/phone 路由及队列，再关闭相关连接 |
| `watch` | `{session}` | `directory {revision,capabilities:{relay_accounts:true,...},hosts:[{endpoint,code,online,machine?,project?}]}`，仅当前账号 grants 内的房间 |
| `phone` | `{code,session,reliable}` | 检查登录与授权，向 host 发送 `join {id,account,expires}`，收到 `joined {id,ok:true}` 后才给客户端 `ready` |

`endpoint` 仅来自管理员配置的 `--public-url`，包含端口，不采用主机任意广告地址。owner secret、daemon 长期 Token 不在账号目录中返回。`watch {rooms:[{code,token}]}` 与 `phone {code,token}` 继续支持，不得把账号 session 当成直接 daemon 的 Token。

委托 phone 随后的二进制 HELLO 不携带 Token/session/password/account。daemon 使用当前上联代际、Client 实例、账号 ID、到期时间和显式 `(relay,room,account)→owner` 本地绑定验证访问，再按该 owner 处理既有会话；direct HELLO 不能伪造此授权。委托账号可以操作绑定所有者的终端和分组，不能修改主机全局 identity/presets/relays。新增能力 `relay_delegation` 不改变原二进制帧格式。

公网账号首帧要求 TLS，或真实来源为受信反代且 `X-Forwarded-Proto: https`；不信任任意客户端提供的该头。Host/Origin 仍精确检查，Windows 本机 UI 的 Origins 需在部署时明确允许。账号、grants 和哈希会话保存在 SQLite；CLI 变更、密码重置、注销和过期由运行中服务监控并撤权。旧终端 Token 轮换不误伤独立的账号委托授权。

本文描述当前仓库实现（2026-10-22），不是远端生产部署状态。终端帧实现见 [protocol.py](../src/supermonoagent/protocol.py)，访问边界见 [security.py](../src/supermonoagent/security.py)，系统关系见 [architecture.md](architecture.md)。

## 链路与认证

- **直连**：客户端 → daemon `/ws`。浏览器在连接后 2 秒内发送二进制 OP1 HELLO，Token 放在 JSON 负载中；原生客户端也可使用 `Authorization: Bearer …`，随后发送 HELLO 协商能力。
- **本机兼容适配**：浏览器 → loopback frontend → 已运行的 loopback daemon。frontend 等待并校验首个 HELLO 的结构，把其中的 Token 放进固定上游的 Authorization 头，再原样转发 HELLO 一次。它不启动或替换 PTY，也不能代理任意远端 URL。
- **中继**：客户端 → relay ← daemon 上联。先发 relay 文本角色握手；phone 收到 `ready` 后，再发送二进制 HELLO。relay 验证访问 Token，daemon 也验证对应客户端的 Token 和会话所有权。
- daemon、relay、frontend 的 WebSocket URL 不接受查询串认证。桌面启动使用 `#token=…`；页面消费 Token 后立即清理地址，并通过首帧认证。旧页面入口的查询 Token 只做一次性兼容消费，不能生成带 Token 的 WS URL。

来源：[daemon.py:handle_socket/allowed/relay_uplink](../src/supermonoagent/daemon.py)、[frontend.py:handler](../src/supermonoagent/frontend.py)、[relay.py:handler](../src/supermonoagent/relay.py)、[workspace.js](../src/supermonoagent/static/workspace.js)。

## 二进制帧

```text
| uint32 LE length | uint8 op | payload[length-1] |
```

`length` 包括 opcode，不包括四字节长度字段。`MAX_FRAME=256 KiB` 限制帧体，`unpack` 要求声明长度与实际长度精确匹配。JSON 负载必须是 UTF-8 对象。认证后的普通坏帧/坏 JSON 会被忽略；认证阶段的坏首帧会关闭连接。

**multiplex 客户端的直连与中继采用相同会话前缀**：OP3/OP8 payload 为 `raw_session_id + NUL + terminal_bytes`。未协商 multiplex 的旧直连视图仍接收原始终端字节；旧 relay 视图也带会话前缀。前缀不是需要写进 xterm 的内容。`pack_feed` 和 `fit_payload` 保留最新的有界历史，并跳过截断位置的 UTF-8 续字节。

来源：[protocol.py](../src/supermonoagent/protocol.py)、[hub.py:_feed_frame/_session_snapshot](../src/supermonoagent/hub.py)。

## Opcode

| Op | 名称 | 方向 | Payload / 语义 |
|---|---|---|---|
| 1 | HELLO | 客户端 → daemon | `{token,multiplex?,reliable?,window?,channel?}`；认证、能力与初始选择 |
| 2 | HELLO_OK | daemon → 客户端 | 目录、身份、能力、当前选择、controls，可能带 error/resize_ack |
| 3 | OUTPUT | daemon → 客户端 | 增量终端字节；multiplex 带 raw session 前缀 |
| 4 | INPUT | 客户端 → daemon | 旧单视图按键；multiplex 客户端不使用此操作 |
| 5 | RESIZE | 客户端 → daemon | `{session?,cols,rows,request?,take_control?}`；列 2–500、行 1–200 |
| 6 | OPEN | 客户端 → daemon | `{window?,shell,group?,cwd?}`；在授权窗口开 CLI，或新建窗口 |
| 7 | SESSIONS | daemon → 客户端 | 目录/尺寸租约等更新；multiplex 包含 controls |
| 8 | SNAPSHOT | daemon → 客户端 | 有界历史回放；multiplex 带 raw session 前缀 |
| 9 | CLOSE | 客户端 → daemon | `{window}` 或 `{session}`；明确结束后台进程 |
| 10 | PING | 客户端 → daemon | 任意 payload，用于 RTT 或旧版 resize 顺序屏障 |
| 11 | PONG | daemon → 客户端 | 原样回显 PING payload |
| 12 | WINDOW | 客户端 → daemon | `{title?,group?}`；新建并选中窗口 |
| 13 | SELECT | 客户端 → daemon | `{window}`；旧视图窗口选择 |
| 14 | FOCUS | 客户端 → daemon | `{session}`；旧视图 CLI 选择 |
| 15 | GROUP | 客户端 → daemon | `{action,...}`，见下表 |
| 16 | SUBSCRIBE | 客户端 → daemon | `{sessions:[raw_id,…]}`；替换本连接的输出订阅集，最多 256 项 |
| 17 | INPUT_SESSION | 客户端 → daemon | `8 字节 raw session ID + NUL + 原始输入字节` |
| 18 | TASK_SUBSCRIBE | 客户端 → daemon | `{after?,replay?}`；订阅任务完成事件，`after` 为最后收到的 event_id，`replay:false` 只建立游标 |
| 19 | TASK_EVENT | daemon → 客户端 | 完成事件 JSON，含 `event_id,session,window,title,machine,project,status,summary,agent,created_at`；基线确认时为 `{type:"cursor",cursor}` |

OP16/17/5 都受 Token 所有权与订阅约束。OP18 的回放按 Token 所有权过滤，relay 客户端还按频道过滤；OP19 只发给已订阅且仍有权限的连接。客户端内部的 host/session 元组键不能放进线协议；不同 daemon 的相同 raw ID 由其所属连接隔离。关闭标签页只从 OP16 订阅集移除该 ID，不发送 OP9。

### 任务完成事件

后台从实时 PTY 字节中识别 OSC 7501；只有 `working/running/busy → idle` 才算一次完成，启动即 idle、历史回放、快照、重连和 `clear` 都不产生事件。事件按所有者哈希（relay 客户端另加频道）隔离，本机最多保存 256 条，单次回放最多 24 条。OP18 的 `after` 找不到对应 event_id 时按最近 24 条回放；首次启用通常发送 `{"replay":false}`，服务端以 OP19 的 cursor 建立基线，避免把旧任务当成新通知。事件只表示 agent 回到输入，不表示命令或测试成功，也不会向终端写入任何内容。

来源：[daemon.py:_op](../src/supermonoagent/daemon.py)、[hub.py:set_subscriptions/write_session/resize_session/subscribe_task_events](../src/supermonoagent/hub.py)、[task_events.py](../src/supermonoagent/task_events.py)、[notification_bridge.py](../src/supermonoagent/notification_bridge.py)。

### 尺寸租约与 ACK

`controls[raw_id]` 为 `{cols,rows,owner,busy}`。`owner` 表示本连接拥有尺寸控制；`busy` 表示其他连接占有。空闲租约可被领取，显式 `take_control:true` 可接管。取消订阅或连接分离释放对应租约。

当前 `resize_ack` 能力返回 OP2 状态及 `resize_ack:{session,request,accepted}`，即使请求被拒绝也返回确认后的实际 grid。客户端按 request ID 串行确认，采用服务器 grid 缩放显示；没有 ACK 能力的旧 daemon 使用 `resize:<request>:<raw_id>` PING/PONG 屏障。有限重绘通过确认后的尺寸往返完成，避开输入/IME 组合期间，不能把重绘当作恢复完整历史的保证。

来源：[hub.py:controls/resize_session](../src/supermonoagent/hub.py)、[daemon.py:_op](../src/supermonoagent/daemon.py)、[workspace.js:requestResize/acceptResize/maybeRedrawPane](../src/supermonoagent/static/workspace.js)。

### GROUP actions

| action | 附加字段 | 权限与效果 |
|---|---|---|
| create / rename / delete | `name,channel?` / `id,name?,channel?` / `id` | 创建自己的分组；只修改自己的分组 |
| move | `window,group` | 窗口与目标分组均须属于此 Token |
| subscribe | `channel` | 选择有权访问的频道 |
| title / autotitle / tab-theme | `window,title` / `window` / `window,theme` | 只修改自己的窗口；theme 为预设配色 |
| presets | `presets` | 仅 daemon 主 Token 可替换全局预设 |
| relays | `relays` | 仅 daemon 主 Token 可修改上联配置 |
| identity | `project` | 仅 daemon 主 Token；机器名来自本机，项目名保存并推送到 relay |

来源：[daemon.py:_op](../src/supermonoagent/daemon.py)、[hub.py:owns/_channel_ok](../src/supermonoagent/hub.py)。

### 状态结构

OP2/OP7 的共同字段如下；OP2 另有当前选择和 service，resize 的 OP2 可另带 `resize_ack`。字段示意中的省略号不是实际 JSON。

```json
{
  "capabilities": {"protocol_version":2,"multiplex":true,"targeted_input":true,"resize_lease":true,"reliable":true,"resize_ack":true,"window_themes":true,"conpty_paste":true,"relay_delegation":true,"task_events":true,"snapshot_sgr":true},
  "machine":"Office-PC",
  "project":"Project A",
  "windows":[{"id":"a1b2c3","title":"Codex","group":"default","focused":"1234abcd","sessions":[{"id":"1234abcd","shell":"cmd","cwd":"D:/work"}]}],
  "groups":[{"id":"default","name":"默认","channel":"default"}],
  "presets":[],
  "controls":{"1234abcd":{"cols":100,"rows":28,"owner":false,"busy":true}}
}
```

能力缺失的旧服务不能被推断为 v2；客户端展示旧版/未知并按实际能力兼容。目录和操作均按当前 Token 过滤。[protocol.py:CAPABILITIES](../src/supermonoagent/protocol.py)、[hub.py:summary](../src/supermonoagent/hub.py)、[daemon.py:_state](../src/supermonoagent/daemon.py)。

## relay 角色与路由

首帧必须在 2 秒内到达，是 JSON 对象，最大 WS 消息 1 MiB。

| 角色 | 首帧示例 | 边界 |
|---|---|---|
| host | `{"op":"host","code":"room-a","owner_secret":"…","tokens":["…"],"machine":"PC","project":"A"}` | 必须具有已登记的独立房间 owner secret；同房间只能一条 host 上联 |
| phone | `{"op":"phone","code":"room-a","token":"…","reliable":true}` | Token 必须存在于在线 host 发布的集合中；ready 后再做 daemon HELLO |
| watch | `{"op":"watch","rooms":[{"code":"room-a","token":"…"}]}` | 只查询提供的已知房间，不返回任意房间花名册 |

普通错误首帧/不支持角色关闭 4000，凭据无效 4001，重复 host 4003；离线账户请求等路径可使用 4004。无效 Token 与未知房间的 phone 查询均返回 4001，不公开身份。注册/登录/轮换另使用 `register/login/rotate` 文本角色，由在线 host 处理账户密码；它们不是发现接口。

已登记 host 收到 `ready`；phone 加入/离开由 relay 给 host 发 `join:{id,token,reliable}` / `leave:{id}`。host 后续可发 `tokens` 替换访问 Token 集、`identity` 更新机器/项目名、`disconnect` 关闭指定 phone。终端数据在 host–relay 链路外包四字节 LE `phone_id`；relay–phone 链路只传原终端帧。relay 不解释终端 payload，但它是可见凭据与转发内容的信任边界，当前没有端到端加密层。

来源：[relay.py:handler/_host/_phone/_account](../src/supermonoagent/relay.py)、[daemon.py:relay_uplink](../src/supermonoagent/daemon.py)。

### 已知房间推送目录

```json
{"op":"directory","revision":7,"capabilities":{"known_rooms":true,"directory_push":true,"host_enrollment":true,"protocol_version":2},"hosts":[{"code":"room-a","online":true,"machine":"Office-PC","project":"A"},{"code":"room-b","online":false}]}
```

每个请求项只对应其给定 code：在线且 Token 有效才返回机器/项目。未知房间、错误 Token、离线不可验证凭据统一返回 `{code,online:false}`，没有身份字段；查询不会创建房间。当前实现离线时也清除已认证 watcher 的身份，不依赖离线身份缓存。

同一 watch socket 可以再次发送完整 `watch` 替换已知集合。host 上线/离线、身份变化、Token 集替换都会触发新 revision 的推送。每个 watcher 队列深度上限 32，发布前替换待发旧快照，因此实际通常只保留最新项；Token 撤销不会排队继续推送旧授权身份。每个连接最多 64 个房间、每秒最多 10 次 watch 更新，房间码 1–128 字符，Token 字段最多 4096 字符，机器/项目名最多 40 字符。

桌面每个 relay 一个 watcher，终端连接保持独立；旧 relay 拒绝 watch 时仅标记目录不可用。客户端不轮询 `/health` 发现电脑。[relay.py:_watch_rooms/_watch/_snapshot/_publish](../src/supermonoagent/relay.py)、[workspace.js:refreshDirectories](../src/supermonoagent/static/workspace.js)。

### 同 Token 的在线电脑发现

客户端可以在已加密的 watch 连接发送 `{"op":"watch","rooms":[],"tokens":["…"]}`，无需预先知道同 Token 电脑的房间码。`tokens` 最多包含 32 个不重复的非空字符串，每个最多 4096 字符；可以与已有 `rooms` 合用，不能与中继账号 `session` 混用。公网必须使用 TLS；可信反向代理模式仅接受配置的代理地址及其 `X-Forwarded-Proto:https`。loopback 隔离测试可以使用 WS。

支持此扩展的目录返回 `capabilities.token_discovery:true`。新增的在线电脑项为 `{code,online:true,machine,project,token_index}`，其中 `token_index` 是请求 `tokens` 数组中首个匹配凭据的下标，响应不包含凭据。只返回在线、接受该 Token 且声明为终端服务的房间，最多 64 项；`host` 首帧可声明 `service:"notifications"`，排除通知专用房间，未声明时为 `terminal`。原有 `rooms` 查询语义保持不变；同一在线房间已由 `rooms` 返回时，只补充 `token_index`，不新增重复项。

上线、离线、身份更新和 Token 撤销复用原有 revision 推送及最新快照队列。无匹配凭据返回空列表，不公开其他电脑身份。此扩展只复用已经由主机接受的访问 Token，不授予房间所有权，也不创建跨主机的新授权。来源：[relay.py:_watch_tokens/_secure_token_discovery/_snapshot](../src/supermonoagent/relay.py)、[test_relay_token_discovery.py](../tests/test_relay_token_discovery.py)。

### owner enrollment 与撤销

生产 relay 的 `--enroll ROOM --enrollment-file FILE` 创建并保存 owner secret 的 SHA-256 verifier，原 secret 只交给对应 daemon（`--relay-owner-secret` 或受保护的 `relays.json`）。终端访问 Token 不能作为 owner secret；未登记、错误 secret 或重复 host 都不能替换已有身份与 Token 集。显式 `--development` 仅允许 loopback 监听及 loopback fixture host 首次声明，不是公网自动认领策略。

host 发布新 Token 集时，relay 清空被撤销 phone 的待发帧、移除发送路径并取消其 sender，再关闭连接（4001）；watcher 的旧快照也被替换为重新授权后的结果。host 断开时清空访问 Token。登记 verifier 跨 relay 进程重启保存，在线访问 Token 集则来自重新认证的 host。来源：[relay.py:enroll/_owner_allowed/_replace_tokens](../src/supermonoagent/relay.py)、[daemon.py:publish_tokens](../src/supermonoagent/daemon.py)。

## HTTP、TLS 与 Origin/Host

`daemon /health` 和 `relay /health` 仅返回 `ok/role` 存活信息，不返回 Token、房间、机器或项目。frontend 的健康响应另有自身 backend/revision 元数据，也不返回 Token/终端目录。发现必须走认证 watch。

daemon、relay、frontend 对 HTTP 与 WS 升级检查 Host 和 Origin：默认只接受实际监听端口的 loopback authority；公网 authority 必须匹配显式 `--allowed-origin` 的 netloc。浏览器 Origin 必须精确匹配 HTTP(S) scheme、host、port 或配置值；`Origin:null` 被拒绝。原生无 Origin 仍需满足 Host 检查并进行凭据认证。

服务端支持成对的 `--tls-cert/--tls-key`，最低 TLS1.2。daemon/relay 公网监听必须配置 TLS，或显式 `--reverse-proxy-tls` 加 HTTPS `--allowed-origin`；该标记表示操作者已配置可信 TLS 反向代理，不会自动生成或验证代理部署。daemon 对非 loopback relay 上联只接受 `wss://`，不禁用证书验证。本仓库的这些配置不会自动升级已运行的远端服务。

来源：[security.py](../src/supermonoagent/security.py)、[daemon.py:serve_http/main/relay_uplink](../src/supermonoagent/daemon.py)、[relay.py:serve_page/main](../src/supermonoagent/relay.py)、[frontend.py:serve_http](../src/supermonoagent/frontend.py)。

## 背压与有界历史

`reliable:true` 表示溢出时明确断开并要求重新订阅有界历史，**不代表带序号的永久无损日志**。daemon 客户端队列、relay phone 队列均为 32 帧；reliable 连接不能静默丢帧后继续 ANSI 流，慢连接以 1013 等关闭。旧非 reliable 路径仍可丢最旧帧。PTY 待发合并超限时，reliable 订阅端失败重连，旧路径可收到快照。

每会话环形历史 256 KiB、待发上限 128 KiB，默认合并窗口 20ms；multiplex 订阅时会把该会话合并窗口降到不高于 4ms。订阅快照记录输出位置，防止紧随其后的 pending batch 重复回放。客户端把 bytes 直接写入 xterm，跨帧保存 UTF-8/ANSI 解析状态。来源：[pump.py](../src/supermonoagent/pump.py)、[hub.py:_flushed/_send/fail_client/set_subscriptions](../src/supermonoagent/hub.py)、[relay.py:_enqueue](../src/supermonoagent/relay.py)。

## 验证入口

终端能力 `snapshot_sgr:true` 表示后台在有界历史裁剪及快照二次装帧裁剪时补回裁剪点的 ANSI SGR 样式，并对齐首个控制 token/UTF-8 字节。客户端收到此标记时直接重放快照，不再运行旧版首边界猜测裁剪。实时输出和 feed 的原始位置不变；只还原颜色、强弱、下划线等 SGR 状态，不宣称重建完整屏幕、光标、滚动区或 OSC8。来源：[ansi_state.py](../src/supermonoagent/ansi_state.py)、[pump.py](../src/supermonoagent/pump.py)、[protocol.py](../src/supermonoagent/protocol.py)。

[tests/test_protocol.py](../tests/test_protocol.py)、[test_security_directory.py](../tests/test_security_directory.py)、[test_client_integration.py](../tests/test_client_integration.py)、[test_desktop_multihost.py](../tests/test_desktop_multihost.py)、[test_workspace_runtime.py](../tests/test_workspace_runtime.py)、[test_resize_recovery.py](../tests/test_resize_recovery.py)。测试证明本地隔离实现行为；远端生产 TLS/owner enrollment 需要单独配置、迁移和验证。

官方 Orca Mobile 的 E2EE/RPC 协议未在此实现；官方 App 不能直接当作本项目客户端。学习资料见 [study/NOTES.md](../study/NOTES.md)。
