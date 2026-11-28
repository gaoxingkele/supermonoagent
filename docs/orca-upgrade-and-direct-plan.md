# SuperMonoAgent Orca 借鉴升级与可选直连开发计划

- 日期：2026-11-28
- 状态：实施计划，尚未开始编码
- 基线：SuperMonoAgent UI 0.2.84；原生安装包 0.2.83 / Android code 85
- 参考副本：`study/orca`，已更新到 `b5a2e2229d686b1fb52a27cdf264a6c7f14c9ff5`
- 建议版本：0.2.85（事件与状态）、0.2.86（Bot + Worker）、0.2.87（可选直连）
- 预计总工期：17–24 个开发日，不含外部 STUN/WebRTC 环境等待时间

本文把事件驱动、长期记忆与实时状态拆分、本地 Bot + Worker、手动 WebRTC 打洞直连整合为一套实施顺序。版本号和工期是计划值，不是已经完成的发布承诺。文中描述的目标行为在对应版本验收前，不代表现有产品能力。

## 1. 背景与问题边界

当前 SuperMonoAgent 以 daemon 持有 PTY，桌面和 Android 通过本机 WebSocket 或中继连接主机。Relay 是默认且最通用的连接路径，必须在全部改造中保持可用。

计划解决四类问题：

1. 托管页签每 3 秒执行一次 `managed_status`，空闲时也产生请求、SQLite 读取和共享 WebSocket 流量。
2. 长期记录、运行时状态、输出位置和浏览器草稿虽然已有不同实现，但缺少统一的恢复和排序契约。
3. 当前没有本地 Bot、WorkItem、独立 Worker 和 worktree 所有权模型。
4. Relay 路径可能受跨国网络、共享主机上行和队头阻塞影响，需要保留一种手动、可选的直连旁路。

### 1.1 Ewan 延迟结论不能被混用

2026-11-28 的只读诊断中，Ewan 曾出现 20 秒以上无 HELLO 响应、10.46 秒和 6.19 秒 HELLO；随后 PONG 又可恢复到约 0.6–0.9 秒。Ewan 的 15 秒 daemon 心跳连续，没有中继重连或错误记录；中继同步采样观察到瞬时 Send-Q。

Ewan 没有声明托管会话或对话记录能力，因此：

- 事件驱动改造不能宣称修复 Ewan 的 12 秒问题。
- Bot/Worker 与 Ewan 当前卡顿没有直接因果关系。
- 直连成功时可能绕开共享中继上行，但如果瓶颈在 daemon 本身，直连仍会慢。
- 共享上行的慢请求、发送排队和跨国 TCP 仍需独立计时诊断。

诊断证据位于主工作树：

- `.omx/artifacts/ewan-latency-20261128/probe-short.json`
- `.omx/artifacts/ewan-latency-20261128/server-monitor.json`

## 2. Orca 参考范围

本地 Orca 副本用于阅读，不直接并入 SuperMonoAgent 构建。

重点参考：

- `cloud/apps/relay/src/splice-forwarder.ts`：高低水位、字节预算、暂停/恢复读取和堵塞检测。
- `cloud/packages/relay-contract/src/splice-state-machine.ts`：转发处理器安装完成后才确认连接。
- `cloud/packages/relay-contract/src/control-messages.ts`：主机控制连接、独立数据连接和能力协商。
- `src/main/bots/`：Bot 身份、工作项、Worker、长期记忆、触发器和人工接管。
- `src/main/runtime/rpc/relay-transport.ts`：每查看连接的数据通道及传输边界。
- Orca Bots 与安全文档：任务来源、Markdown 记忆、权限和不可信外部内容边界。

不照搬：

- Orca Cloud、director、区域迁移和 PostgreSQL 运维体系。
- Slack、Linear、GitHub 托管 Connector 服务端。
- Orca 私有 RPC/E2EE 全量协议。
- 自动记忆整理、自动 PR 合并和无人值守外部写入。

WebRTC NAT 打洞是 SuperMonoAgent 的可选传输设计，不声称是 Orca 现有实现；Orca 主要提供控制/数据分离、路径切换和版本兼容方面的参考。

## 3. 总体原则

1. **Relay 优先且始终可回退。** 冷启动、手机或电脑重新进入时，先通过 Relay 恢复可用画面。
2. **逻辑会话与物理连接分离。** Transport 可以替换，daemon 运行时、CLI 进程和 WorkItem 不能因换路而被重建。
3. **新增能力协商，不改旧帧语义。** 旧端不声明能力，就不接收新消息。
4. **事件只是优化，权威状态可重建。** 断线、序号缺口或目标变化时，以权威快照校准。
5. **命令必须幂等。** 换路、丢 ACK 和重连不能重复提交 Prompt、创建 Worker 或执行权限操作。
6. **事件帧小而有界。** 不在事件中携带终端正文、完整 Journal、完整 diff 或大段权限参数。
7. **不重写稳定 Relay 路径。** DirectTransport 与 RelayTransport 并存，未手动启用直连时不进入新代码路径。
8. **功能分版本发布。** 事件、Bot/Worker、直连不叠加为一个无法定位回归的大版本。
9. **可回滚。** 首版直连不做数据库迁移；WebRTC 组件失败不得影响 daemon 或 Relay 启动。
10. **不保存秘密到日志和记忆。** Token、STUN/TURN 凭据、终端正文和未确认正文不得进入公共事件或 Markdown 记忆。

## 4. 目标架构与状态归属

### 4.1 逻辑结构

```text
Bot / WorkItem / Worker 控制层
              │
ManagedConversationService / RuntimeRegistry
              │                    │
JournalStore 长期记录          OutputPump 输出与游标
              │                    │
          LogicalSession（跨 Transport 稳定）
              │
      ┌───────┴────────┐
      │                │
RelayTransport    DirectTransport（可选）
      │                │
      └──── 同一查看端会话恢复、去重与命令归属 ────┘
```

主机 Relay 上行始终保留。直连只替换当前查看端的数据路径，不迁移主机上行，也不影响其他查看者。

### 4.2 状态唯一所有者

| 状态 | 唯一所有者 | 说明 |
|---|---|---|
| 创建记录、提交 receipt、Provider 绑定、最终回复、审计事件 | `JournalStore` | 长期、可恢复、可审查 |
| driver、锁、活动轮次、取消版本、权限计时器、运行状态 | `ManagedConversationService` / RuntimeRegistry | daemon 运行时 |
| `history_epoch`、`stream_id`、输出位置、环形历史 | `OutputPump` | 不复制进 Bot 记忆 |
| 每客户端 `feed_offsets`、`history_after`、待回放状态 | Hub Client | 随订阅和连接恢复 |
| WorkItem、Worker、attempt、controller_owner、幂等 intent | RuntimeStore | 操作记录，不是长期记忆 |
| 未确认浏览器草稿 | 浏览器 Controller | 不能证明后台已经受理 |
| Relay/Direct socket、RTT、ICE、重连状态 | Transport | 不代表 PTY 或 Worker 是否存活 |
| Bot 身份与长期事实 | `bot.json`、`SOUL.md`、`MEMORY.md` | 不保存连接、队列和秘密 |

### 4.3 代次和恢复键

统一区分：

- `runtime_epoch`：daemon 每次启动生成；socket 重连或 Relay/Direct 切换不能改变。
- `process_generation`：CLI 进程代次；Transport 切换不能改变。
- `connection_generation`：每条物理连接递增，只用于废弃迟到 socket 回调。
- `revision`：每个托管运行时状态递增。
- `history_epoch`、`stream_id`、`output_position`：终端输出恢复和去重。

Runtime Event 唯一键：

```text
host_installation_id / journal_id / process_generation / runtime_epoch / revision
```

## 5. 协议与兼容性预留

建议统一登记：

```text
OP26 = OP_RUNTIME_EVENT
OP27 = OP_DIRECT_SIGNAL
```

Bot/Worker 控制第一版复用现有控制通道；确需新增 opcode 时再统一登记，避免分支重复占用编号。

新增能力：

```text
managed_events
bots
workers
direct_webrtc
```

兼容矩阵：

| 组合 | 目标行为 |
|---|---|
| 旧 UI + 旧后台 | 与当前版本一致 |
| 新 UI + 旧后台 | 保留现有轮询，不显示不支持的功能 |
| 旧 UI + 新后台 | 不接收 OP26/OP27，不出现未协商消息 |
| 新 UI + 新后台 | 仍 Relay 优先，按能力启用事件、Bot 和手动直连 |
| 账号授权/ delegated 连接 | 继续执行既有所有者、房间和目标权限检查 |

Relay 终端帧保持不透明转发。直连信令优先复用 daemon 到查看端的现有路由，不要求重写 `relay.py`。

## 6. 分阶段实施计划

## P0：基线、公共边界与回滚准备（1 天）

### 工作内容

- 固化当前 stable 的安装包、UI 快照、哈希和回滚位置。
- 在 `workspace.js` metrics 增加或规划：
  - `managedStatusRequests`
  - `managedStatusBytes`
  - `managedEventsReceived`
  - `managedEventGaps`
  - `managedReconciles`
  - `managedLastEventLatency`
  - `transportSwitches`
  - `directAttempts`
  - `directFailures`
- 记录 10 个托管页签、10 分钟的请求数、流量、PONG 和主线程阻塞基线。
- 登记 OP26/OP27 和能力名称。
- 定义 LogicalSession、Transport、connection_generation 的接口边界。
- 为 Ewan 单独保留中继与 daemon 分段计时，不把它混入事件功能验收。

### 完成条件

- 当前 Relay 全量回归通过。
- 新旧版本测试矩阵可执行。
- 可恢复上一版 UI 和原生安装包。
- 未启用新功能时，线上协议和配置不变。

## P1：事件驱动替代固定轮询（3–4 天，0.2.85）

### 后台实现

新增 `managed_events.py`，并小范围修改：

- `protocol.py`
- `hub.py`
- `daemon.py`
- `managed_conversation.py`

要求：

1. 仅向握手中声明 `managed_events` 的客户端发送 OP26。
2. 事件订阅跟随现有 OP16 会话订阅，不另建一套无界订阅。
3. 在 ready、提交受理、turn started、权限出现/撤销/过期、中断、完成、失败和关闭的提交完成后发布。
4. `_refresh()` 更新一次缓存快照，多个客户端共享，不能每客户端重复读取 SQLite。
5. 公共状态指纹不变时不发事件；25ms 内连续变化合并到最新 revision。
6. 大权限详情通过 OP24 按需读取，事件只带描述符和 ID。
7. 每会话保留最近 128 个事件 revision，用于短期补发；队列必须有界。

### 离线与断线

- 重连后客户端提交最后事件键。
- 可连续回放则补发；revision 缺口、runtime_epoch 改变、process_generation 改变或目标不匹配时，执行一次完整 `managed_status`。
- 状态事件不必永久落盘；权威状态可以重建。
- 未确认提交继续使用 nonce 和持久化 receipt 核对，不因离线自动重发正文。

### 前端实现

在 `terminal-client.js`：

- 把 `applyStatus()` 统一为权威快照入口。
- 新增 `applyRuntimeEvent()`，拒绝旧 epoch、旧 revision 和不匹配目标。
- 事件与命令 ACK 任意先后到达时，同一 nonce 的 `onAccepted` 只能执行一次。
- 上下文切换后，迟到事件不得修改新页签。

将 `workspace.js` 当前 3 秒固定逻辑改为：

- 创建 Controller、重连、重新订阅：立即校准一次。
- 事件缺口：立即校准。
- 未确认提交：3、10、30 秒退避核对。
- 活动状态且事件不可用：最多每 30 秒兜底一次。
- ready/idle 且事件正常：不周期查询。
- 旧后台：保持原 3 秒兼容行为。

对话记录阅读器的健康检查随后复用同一事件机制，不与第一阶段同时做大规模重写。

### 验收

- 新版组合中，10 个空闲托管页签运行 10 分钟，除初始校准外 `managed_status` 为 0。
- 权限、完成和失败事件在国内正常链路下展示 p95 小于 300ms。
- 丢 ACK、断线、乱序和重复事件不造成重复提交。
- 新 UI/旧后台、旧 UI/新后台和 delegated 连接全部通过。

## P2：长期记忆与实时状态拆分（2–3 天，0.2.85）

### 工作内容

新增 `runtime_state.py`，定义：

- `ManagedRuntimeSnapshot`
- `WorkItemRecord`
- `WorkerRecord`
- `RuntimeEvent`

不一次性搬动所有锁和 driver。先让现有 `_Conversation` 实现统一投影，再逐步替换分散字段。

提供两种只读投影：

- 目录快照：状态、Provider、Worker 数等轻量字段。
- 控制快照：活动提交、权限、取消版本等控制信息。

统一 `_snapshot()`、`_status_result()` 和目录装饰的输出语义，避免相互矛盾。

### 恢复语义

- 后台重启不丢长期记录和 receipt。
- 无法确认的进程状态标为 `unverifiable`，不能仅凭断线写成 exited。
- 输出游标可恢复则恢复，否则做一次有界快照。
- Worker 的 worktree 和分支保留，不自动删除或重建。
- Transport 断开只改变连接状态，不改变 WorkItem 的最终结果。

### 验收

- 重启后台不会清空长期记录或误报完成。
- 同一状态只计算一次并广播给多个客户端。
- 旧快照不能覆盖更新的 revision。
- 终端恢复位置不进入 Bot 记忆或对话正文。

## P3：直连隔离技术验证（1–2 天，独立 QA）

此阶段只回答“依赖和网络是否可行”，不接入 stable workspace。

### 工作内容

- 在独立分支、独立状态目录和测试房间验证 WebRTC DataChannel。
- 优先评估可选 sidecar，避免 WebRTC 依赖成为核心 daemon 的硬依赖。
- 对比 `aiortc` 与原生 sidecar 的 Windows 打包、杀毒软件、依赖体积和 Android WebView 互操作性。
- 只实现合成 echo、DTLS、STUN 和 ICE，不接真实 PTY。
- 验证 Windows 与实体 Android 构建。
- 测试同 LAN、家庭 NAT、蜂窝 CGNAT、对称 NAT、STUN 不可达和 IPv6 场景。
- 不使用生产 stable manifest，不访问真实终端正文。

### 退出条件

- 明确选择可维护的 WebRTC 组件，或记录直连暂不具备交付条件。
- WebRTC 组件加载失败不会阻止 daemon 启动。
- 获得至少一组真实打洞成功和一组明确失败但 Relay 不受影响的证据。

## P4：本地 Bot + Worker（4–6 天，0.2.86）

### 首版范围

包含：本地 Bot、手动任务、用户确认启动 Worker、独立 worktree、Worker 状态、用户接管和幂等恢复。

不包含：Slack/Linear/GitHub Cloud Connector、自动 PR 合并、自动推送、自动清理 worktree、自动记忆整理和无人值守外部写入。

### 新模块

- `bot_service.py`：Bot 创建、对话和任务调度。
- `worker_service.py`：Worker 启动、状态和接管。
- `runtime_store.py`：WorkItem、Worker、attempt 和幂等 intent。
- 前端 `bots.js`：Bot、WorkItem 和 Worker UI，避免继续膨胀 `workspace.js`。

### Bot 文件

```text
bots/<bot_id>/bot.json
bots/<bot_id>/SOUL.md
bots/<bot_id>/MEMORY.md
```

`bot.json` 保存稳定 ID、所属电脑、项目根目录、Provider 和权限策略。Markdown 只保存用户可审查的长期身份与事实，不保存 Token、会话秘密、连接状态和输出位置。

### WorkItem 与 Worker

WorkItem 状态：

```text
proposed → awaiting_approval → queued → running
         → waiting_user / completed / failed / cancelled
```

Worker 至少记录：

```text
worker_id、bot_id、work_item_id、attempt_id
repo、base_commit、branch、worktree_path
session_id、process_generation
controller_owner、status、created_at
```

Worker 在 Hub 目录中显示为可打开会话，并携带 Bot/WorkItem 元数据。

### Worktree 规则

- 使用参数数组调用 Git，不拼接 shell 命令。
- Git 操作放入工作线程，不阻塞 daemon 事件循环。
- 默认从明确 HEAD commit 创建唯一分支和 worktree。
- worktree 位于用户状态目录，不污染当前检出目录。
- 不自动携带未提交修改；dirty repo 明确提示。
- 不自动提交、推送、合并或删除。
- 默认最多 3 个并发 Worker，可配置。

### 用户接管

首版接管指接管 Worker 的对话与审批权，不承诺切换到未受控原始 PTY。

- 接管后 `controller_owner` 变为用户。
- Bot 的发送 fence 立即失效，迟到回调不能继续提交。
- 已物理发送的 Provider 请求如实标记，不能假装已撤销。
- 用户可发送下一条指令、处理权限、中断或交还 Bot。
- Worker 的代码、diff 和测试结果可单独打开检查。

### 离线队列与幂等启动

- 创建和启动操作都有 nonce。
- 数据库先记录 intent，再执行 Git/PTY 操作。
- ACK 丢失后，同一 nonce 返回原 Worker，不能再建 worktree。
- 重启后 `starting` 先核对；无法确认时标为 `unverifiable`，不自动重复启动。
- Worker 完成通过内部事件通知 Bot，不让 Bot 轮询整份会话。

### 验收

- 重复点击、丢 ACK 和重连只产生一个 Worker。
- 用户接管后，迟到的 Bot 回调无法发送。
- daemon 重启不丢 WorkItem、分支和 worktree。
- 无法确认的进程不被误报为 exited 或 completed。
- Windows 与实体 Android 可完成创建、启动、接管和恢复流程。

## P5：DirectTransport 与手动打洞集成（4–6 天，0.2.87 QA）

### 默认行为

- 冷启动始终 Relay 优先。
- 未点击“尝试直连”时，不创建 WebRTC 对象、不访问 STUN、不发送 OP27。
- 不因为上次直连成功而改变下次启动顺序。
- DirectTransport 与现有 RelayTransport 并存，不重写稳定 Relay 路径。

### 手动协商

1. Relay 已连接并恢复目录、终端和历史。
2. 用户点击“尝试直连”。
3. 通过现有授权路径交换 offer、answer、ICE candidates、随机会话 ID 和一次性票据。
4. 信令设置短有效期，并绑定房间、主机代次和随机会话。
5. 打洞期间 Relay 继续显示输出和处理操作，不暂停 PTY。
6. 打洞失败或 10 秒超时只清理 Direct，Relay 不受影响。

### 测速和切换

- 直连先完成应用认证、HELLO、历史恢复和 3–5 秒稳定性测量。
- 仅当直连稳定且 RTT 至少改善约 20% 时建议切换；用户仍可保留 Relay。
- 切换瞬间短暂冻结当前查看端输入。
- 核对 runtime_epoch、process_generation、输出游标和未确认 nonce。
- 新连接收到正确目录和快照后原子切换，再关闭旧查看连接。
- 不关闭主机 Relay 上行，不影响其他查看者。
- Bot/Worker 命令只查询原 intent，不因换路重新执行。

### 回退

- Direct 中断自动重接 Relay，并提示“直连已断开，已回退中继”。
- 不自动反复打洞；用户需要时再次手动点击。
- 下次冷启动仍 Relay 优先。
- Relay 与 Direct 的迟到事件按统一事件键和输出位置去重。

### 界面状态

```text
中继 · 85ms        [尝试直连]
打洞协商中…         [取消]     中继仍可使用
直连可用 · 42ms    [切换直连] [保留中继]
直连 · 42ms        [切回中继]
直连失败：NAT 不可穿透            中继未受影响
```

错误只显示通用分类，不暴露 IP、Token 或详细网络配置。

## P6：直连稳定化与候选发布（2 天，0.2.87）

- 完成 Relay 全量回归和混合版本矩阵。
- 验证切换中输入、权限、提交和 Worker 启动不重复。
- 验证手机休眠、电脑重启、打洞超时、STUN 不可达、对称 NAT、Direct 中途断开。
- 验证多查看者、主机 Relay 上行和其他房间不受影响。
- Direct 关闭时不得访问 STUN 或改变现有网络行为。
- 通过后才进入 stable；发布时仍默认关闭、手动启用。

## 7. 冲突分析与统一处理

### 7.1 事件恢复与 Direct 恢复交叉

两套功能必须使用同一组 runtime/event/history/process 恢复键。Direct 切换不能改变 runtime_epoch 或 process_generation，旧路径的迟到事件按 connection_generation 废弃，按 event revision 去重。

### 7.2 状态拆分与 Direct 互补

Transport 只表示路径状态。Direct 或 Relay 断开不能直接把 Worker、CLI 或提交标为终止。状态拆分必须先于 Direct 正式集成。

### 7.3 Bot/Worker 与换路的重复命令风险

所有 Bot/Worker 控制命令都有 nonce 和 intent。换路只查询，不自动重放。controller_owner 保存在 WorkerRecord，不保存在 socket 上。

### 7.4 协议号和前端文件冲突

OP26/OP27 在 P0 统一登记。前端拆为 `runtime-events.js`、`direct-transport.js`、`bots.js`，`workspace.js` 只负责组合，避免四个功能同时实现各自的恢复逻辑。

### 7.5 共享 Relay 队头阻塞

Runtime Event 和 Direct Signal 必须小而有界，不能传输完整 Journal、diff、终端正文或大权限详情。事件减少固定轮询，但不等于修复共享主机上行；Ewan 的分段计时仍需独立推进。

### 7.6 性能结论归属

每次 Direct 验收同时记录 Relay RTT、Direct RTT、主机处理时间、历史恢复时间和切换等待，不能只看界面单个值，也不能把 Relay 改善误记为 daemon 修复。

## 8. 安全与隐私要求

### Bot/Worker

- 首版外部输入和 Worker 启动默认需要用户确认。
- Bot 不能自行提高权限或创建另一个 Bot。
- 外部邮件、Issue、Webhook 等未来内容一律视为不可信数据，不是指令。
- 长期记忆不得保存 Token、密钥、Cookie 和未确认提交正文。
- Git 操作必须有资源上限、超时和明确路径边界。

### Direct/WebRTC

- 使用 DTLS，并在应用层再次验证主机身份和一次性票据。
- 手动点击是暴露网络地址给对端和 STUN 服务的明确动作。
- STUN 地址可配置，正式环境优先自有 STUN。
- 不默认开启 UPnP/NAT-PMP，不自动修改防火墙或路由器映射。
- TURN 属于中继，不能显示为打洞直连。
- 信令短期有效、防重放，并绑定授权房间和主机代次。
- 日志只记录匿名网络类别、耗时和错误码，不记录候选地址、会话正文或凭据。

## 9. 测试计划

### 单元与集成

新增或扩展：

- `test_managed_events.py`：revision、去重、断线、epoch、权限隔离。
- `test_runtime_store.py`：恢复、幂等和离线 intent。
- `test_bot_service.py`
- `test_worker_worktrees.py`
- `managed-client.test.cjs`
- `workspace-managed.test.cjs`
- `android-managed.test.cjs`
- DirectTransport 与信令测试：能力、超时、迟到回调、防重放和 Relay 回退。

重点故障：丢 ACK、重复点击、乱序事件、跨用户访问、旧版本组合、重启、接管后迟到 Bot 回调、切换中命令、STUN 不可达和 Direct 中断。

### 标准检查

```bash
python -m pytest
node --test tests/managed-client.test.cjs tests/workspace-managed.test.cjs tests/android-managed.test.cjs
```

随后执行项目现有 Windows 打包、浏览器、Android 和发布工具检查。

### 真实端到端

- 隔离 Windows daemon + Relay + 实体 Android。
- 至少一个真实 Claude 或 Codex 托管会话。
- 至少一个临时 Git 仓库中的 Worker，验证分支、worktree 和接管。
- Direct 至少一组真实 NAT 成功和一组不可穿透失败。
- 手机锁屏、休眠、切换网络、进程被系统回收后恢复。
- 不将模拟器、合成 echo 或导入测试当作实体功能验收。

### 性能与稳定性

- 10 个空闲托管页签 10 分钟无固定 `managed_status`。
- 30 分钟多页签 soak，无事件风暴、无界面持续阻塞。
- Direct 切换无重复输入、重复 Worker 和输出重复。
- Relay 回归的 RTT、PONG、重连和队列指标不劣于基线。
- Ewan 单独记录分段指标，不能用其他主机的成功替代。

## 10. 发布与回滚

### 0.2.85：事件与状态

1. 先发布向后兼容后台。
2. 验证旧 UI + 新后台、新 UI + 旧后台。
3. 再发布 UI 热更新并保留上一版签名快照。
4. 不包含 WebRTC 硬依赖。

### 0.2.86：Bot + Worker

1. 发布 Windows/Android 原生包。
2. 先开放手动、单 Bot、单 Worker。
3. 再开放默认最多 3 个并发 Worker。
4. 仍完全使用 Relay，不引入 DirectTransport。
5. 回滚只停止新任务，不关闭现有 PTY、不删除 worktree。

### 0.2.87：可选直连

1. 必须发布原生包，不能只靠 UI 热更新。
2. 独立 QA applicationId 和状态目录先行。
3. 稳定版默认关闭，冷启动 Relay 优先。
4. WebRTC 组件缺失或失败不影响 daemon 和 Relay。
5. 首版不做数据库迁移，恢复旧包不需要数据降级。

## 11. 排期与依赖

| 优先级 | 阶段 | 版本建议 | 依赖 | 工期 |
|---|---|---|---|---|
| P0 | 基线、公共边界和回滚 | 0.2.85 准备 | 当前 stable 回归 | 1 天 |
| P1 | Runtime Event 与取消固定轮询 | 0.2.85 | P0 | 3–4 天 |
| P2 | 状态归属与统一恢复键 | 0.2.85 | P1 | 2–3 天 |
| P3 | WebRTC 隔离技术验证 | 独立 QA | P2 的边界定义 | 1–2 天 |
| P4 | 本地 Bot + Worker | 0.2.86 | P1、P2 | 4–6 天 |
| P5 | DirectTransport 集成 | 0.2.87 QA | P2、P3、P4 | 4–6 天 |
| P6 | Direct 稳定化 | 0.2.87 候选 | P5 | 2 天 |

P3 可以提前进行技术验证，但不得提前进入 stable。Direct 正式集成放在 Bot/Worker 幂等控制完成之后。

## 12. 全局发布门槛

任何阶段只有同时满足以下条件才可进入下一阶段：

- 对应单元、集成、混合版本和真实设备测试通过。
- Relay 默认路径没有功能或性能回归。
- 未启用的新功能不发送新帧、不读取新秘密、不改变持久化格式。
- 丢 ACK、断线和换路不会重复执行命令。
- 新旧 UI/后台组合行为明确。
- 回滚不需要删除 PTY、worktree 或长期记录。
- 文档中的目标行为已经由对应版本证据支持。
- 未验证项明确保留，不把“构建成功”写成“实体验收完成”。

## 13. 主要风险与应对

| 风险 | 应对 |
|---|---|
| 状态事件乱序或丢失 | epoch、revision、有界回放和权威快照 |
| Relay/Direct 切换重复输入 | 切换冻结、nonce、intent 和连接代次 |
| Worker 重复创建 | 持久化 intent 和确定性 Worker/attempt 归属 |
| WebRTC 依赖破坏 daemon 启动 | 可选 sidecar、延迟加载、缺失时只隐藏功能 |
| 原生打包或杀毒软件问题 | 独立 QA 包和 Windows 实机验证 |
| NAT 打洞成功率低 | 保持 Relay、明确失败、不自动端口映射 |
| 新事件加重共享上行 | 小帧、合并、去重、按需读取大内容 |
| 多功能同时回归难以定位 | 分版本、分模块、独立发布和回滚 |
| 把 Direct 当作 Ewan 修复 | 分段测量，保留独立队头阻塞诊断 |

## 14. 最终执行顺序

```text
现有稳定版回归与回滚快照
        ↓
Runtime Event 与统一恢复键
        ↓
长期记录 / 运行时 / 输出位置 / Transport 状态拆分
        ↓
隔离环境验证 WebRTC，仅验证可行性
        ↓
本地 Bot + WorkItem + Worker + 幂等控制
        ↓
DirectTransport 手动打洞与原子切换
        ↓
Relay / Direct 混合版本和真实网络回归
        ↓
默认关闭的 0.2.87 stable 发布
```

首批实施从 P0/P1 开始。Direct 在 0.2.85 和 0.2.86 中只保留边界，不进入稳定运行路径。

## 15. 相关文档与代码

- [架构与代码导航](architecture.md)
- [Hub 现场验收](hub-field-validation.md)
- `src/supermonoagent/managed_conversation.py`
- `src/supermonoagent/conversation_store.py`
- `src/supermonoagent/hub.py`
- `src/supermonoagent/daemon.py`
- `src/supermonoagent/static/terminal-client.js`
- `src/supermonoagent/static/workspace.js`
- Orca Bots 文档：https://orca.dev/docs/bots
- Orca Bot 安全文档：https://orca.dev/docs/bots/security
- Orca 仓库：https://github.com/stablyai/orca
