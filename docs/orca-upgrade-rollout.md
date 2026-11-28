# Orca 升级实施与发布记录

实施依据为 [P0–P6 开发计划](orca-upgrade-and-direct-plan.md)。每个阶段保存源码版本、候选产物、兼容性、实机或远端回读以及回滚证据。阶段产物按验证范围发布。

| 阶段 | 发布目标 | 状态 |
| --- | --- | --- |
| P0 基线和回滚 | 0.2.84 界面、0.2.83 原生包的回归与恢复记录 | 通过 |
| P1 运行时事件 | 0.2.85 后台候选，随后双端界面候选 | 试点验证通过，公开发布并入 P2 |
| P2 统一状态 | 0.2.85 完整候选，验证后逐端升级 | stable 已发布，现场核对通过 |
| P3 WebRTC 验证 | 独立 QA 包、状态目录和 Relay 测试房间 | 同 LAN、5G、IPv6 实测通过，真实对称 NAT 待验收 |
| P4 本地 Bot 与 Worker | 0.2.86 原生候选，完成接管和重启恢复后升级 | 待 P3 对称 NAT 验收口径 |
| P5 直连集成 | 0.2.87 QA，有成功切换和 Relay 回退两类证据 | 待 P4 |
| P6 直连稳定化 | 0.2.87 原生发布，直连默认关闭 | 待 P5 |

## P0 连接与会话边界

- `LogicalSession` 以主机安装标识、会话和进程代次绑定授权目标，持有当前物理路径、输出恢复游标及未确认命令。换路时在同一逻辑会话核对游标与回执后提交一次路径切换。
- `Transport` 管理连接、鉴权后的帧收发、RTT 和关闭。连接关闭通知逻辑会话重新选路与恢复。会话与命令的最终状态由 daemon 判定。
- `connection_generation` 沿用桌面 `HostConnection.generation` 的隔离语义。物理连接替换后，收帧、关闭和异步响应都需核对连接对象与代次。新旧路径并存时各有独立代次，只有已提交的路径可接受用户输入。
- `runtime_epoch` 随 daemon 启动更新。`process_generation` 随 CLI 实例更新。`history_epoch`、`stream_id`、`output_position` 由终端输出层维护。换路依照这些值决定续接或重取快照。

实现定位：[桌面 workspace](../src/supermonoagent/static/workspace.js)、[Android app.js](../android/assets/mobile/app.js)、[daemon](../src/supermonoagent/daemon.py)、[Hub](../src/supermonoagent/hub.py)、[线协议](protocol.md)。

## 性能指标定义

| 指标 | 计数口径 |
| --- | --- |
| `managedStatusRequests` | `managed_status` 请求成功交给连接发送的次数 |
| `managedStatusBytes` | 对应请求和响应的应用帧字节和，含五字节帧头，两种方向分别留样 |
| `managedEventsReceived` | 通过协商、连接和目标校验的 OP26 接收次数 |
| `managedEventGaps` | 需要回放或快照校准的修订序列缺口次数 |
| `managedReconciles` | 由初始连接、恢复或兜底触发的权威状态校准次数 |
| `managedLastEventLatency` | 客户端接收后至状态生效的耗时。跨机端到端 p95 单独用可校准的测试时钟测量 |
| `transportSwitches` | 成功提交的路径切换次数，按方向分别采样 |
| `directAttempts` | 用户显式发起且进入协商的直连次数 |
| `directFailures` | 直连协商因超时或失败退出的次数，用户取消单列 |

只保存计数、时间和错误分类。桌面 0.2.84 每 3 秒核对已打开的受管页签，Android 活动控制器每 2 秒安排下一次核对。混合版本验收分别保留各端的旧后台兼容频率。

## 兼容与发布检查

冻结工作树起点为 `327a9ba68558973d861601f8fc322a5888ff2d46`，发布基线见 [0.2.84 界面](release-0.2.84.md) 和 [0.2.83 原生包](release-0.2.83.md)。在独立端口与状态目录分别运行旧、新后台，加载对应版本的完整资源包。

| 组合 | 核对项 |
| --- | --- |
| 旧客户端 + 旧后台 | 建连、目录、OP24 查询、权限处理和断线恢复，留存频率、流量、PONG 和主线程基线 |
| 旧客户端 + 新后台 | 建连通过；保留原 OP24 轮询；捕获确认没有 OP26 / OP27 |
| 新客户端 + 旧后台 | 建连和旧功能通过；按缺失能力启用原频率的轮询；捕获确认没有新帧 |
| 新客户端 + 新后台 | 按能力验证各端订阅、恢复、晚到帧和重复回执，抽查十个桌面空闲页签 |
| delegated + 中继 | 授权目标可用，撤权断开，同房间其他所有者的事件和会话不可见 |

```powershell
$relay = @(rg --files tests -g 'test*relay*.py')
python -m pytest -q @relay tests/test_protocol.py
$managed = @(rg --files tests -g 'test_managed*.py' -g 'test_workspace_managed.py' -g 'test_android_managed.py')
python -m pytest -q @managed
node --test tests/managed-client.test.cjs tests/workspace-managed.test.cjs tests/android-managed.test.cjs
```

P0 在项目虚拟环境的独立状态目录执行。中继与协议 127 项、23 个子测试通过。托管后端和桌面、Android 浏览器回归 158 项通过。JavaScript 49 项通过。

真实 Edge、隔离 Relay、daemon 和 SQLite 上的 10 页签运行 600.005 秒。测试 Provider 通过既有夹具模拟。共 2,000 次 `managed_status` 请求，1,447 次返回 `busy`。应用帧请求 587,175 字节、回复 383,472 字节。40 次 PONG 无丢失，p95 为 3.548 ms。期间重连和 50 ms 以上主线程长任务均为 0。daemon 侧十个会话均 ready，桌面仍有五个显示未知。P1 的批量校准需避免旧版同步轮询造成的队列竞争。原始记录在主工作树 `.omx/artifacts/orca-p0-baseline/baseline.json`。

## P0 发布服务器与回滚材料

- 默认 HTTPS 通道公开的签名界面为 0.2.84。Windows 原生更新为 0.2.83。Android 已归档原签名 APK 为 0.2.83（code 85）。清单签名、双端资源字节、Windows 安装目录与 Android APK 签名均已复验。
- 通过现有发布密钥和固定主机密钥只读核查 Linux 发布服务器。`sma-relay.service` 为 `active/running`，公网 `/health` 返回 HTTP 200。服务器磁盘上的 stable 界面和 Windows 原生清单 SHA-256 与公网回读一致。
- 回滚包、完整 SHA-256 与恢复步骤保存在主工作树 `.omx/artifacts/orca-p0-rollback/`。隔离更新存储已验证双端资源从 0.2.84 恢复至 0.2.83，并保留已接受的最高发布序号。全局 UI 恢复需用保留快照构建签名新序号。Android 一旦启用更高 versionCode，原生恢复需用同签名、更高 versionCode 的恢复包保留数据。
- 实体 Android 16 已从 0.2.78（code 80）同签名覆盖升级至 0.2.83（code 85），原首次安装时间保留。设备读回安装包 SHA-256 与留档 APK 完全一致，冷启动成功，界面显示 0.2.84，终端显示「已连接」。
- Windows 签名安装包 0.2.83 在隔离目录完成安装，独立 daemon `/health` 返回 200，实际 WebView2 加载工作区和编辑器。现有桌面进程继续运行。
- 远端、Android 实机和 Windows 原生回读在主工作树 `.omx/artifacts/orca-p0-native/`。本步骤发布基线记录，产物版本沿用本节已验证的 stable。

## P0 跨网闭环

Windows 测试 daemon 在现有家庭 Wi‑Fi 的 WLAN 接口上建立 TLS 上联，经过阿里云现有 Relay 的一次性测试房间。实体 Android 使用默认蜂窝链路和 NR，无 ADB 数据转发。ADB 仅控制原签名应用的实际 WebView。手机向隔离 CMD 提交单次计算，看到 `95540`，重连后仍能读回。同一终端在 Windows 留有两次实际查看端 HELLO、对应订阅以及一次完整提交。Windows 默认路由为 Meta，本次测试只绑定了测试 socket。证据位于主工作树 `.omx/artifacts/orca-p0-closed-loop/` 与 `.omx/artifacts/orca-p0-native/phone-loop.json`。

独立实测的 Codex 0.167.0 验证了一次真实受理、同一 nonce 回执、完成状态和唯一最终回复。Claude Code 2.1.310 已验证实际启动与持久回执，模型轮次返回 `provider_turn_failed`。这两次 Provider 测试使用隔离的本机 daemon，与三端 CMD 验证分别留证。

## Ewan 独立诊断

Ewan 的探测继续单列。以 `probe-short.json` 的 HELLO/PONG 和 `server-monitor.json` 的中继队列、daemon 指标为分段证据。证据目录为主工作树 `.omx/artifacts/ewan-latency-20261128/`。

## P1 运行时事件试点

- 新旧端通过 HELLO 协商 OP26。状态事件随 OP16 订阅授权，25 ms 合并，在同一会话保留 128 条修订。缺口走权威状态校准。权限详情按需走 OP24，提交用持久 nonce 回执去重。旧后台保留桌面三秒、Android 两秒轮询。
- 同款隔离 Edge/Relay/daemon 测试连续 600.016 秒。十个页签各成功校准一次后保持 ready，周期 `managed_status` 从 2,000 次降至零。40 次 PONG 全部返回，p95 由 3.548 ms 降至 3.050 ms，长任务和空闲重连均为零。全部帧字节总量下降 11.8%，下行增长 29.1%，仍有 916 次其他 OP24 请求。对话记录阅读器的查询留给后续验证和优化。证据为主工作树 `.omx/artifacts/orca-p1-performance/p1-performance.json`。
- 家庭 WLAN 经阿里云 Relay 到实体 Android 5G NR 的独立试点使用真实 Codex 0.167.0。手机发送一次结构化请求，收到三个 OP26 事件。重连带修订 5 的游标，SQLite 读回一条输入和一条符合预期的最终回复。试点房间、临时凭据和手机 QA 数据已清理，QA APK 保留在独立应用中。
- 在同一真实网络拓扑上用合成 Provider 测两轮，共记录 60 次权限、30 次完成和 30 次失败状态。按最近秩计算的端到端可见 p95 分别为 196.391、186.154 和 217.146 ms，均优于 300 ms。原首轮 462.433 ms 慢样本保留，Windows 到发出 22.251 ms，其余 440.182 ms 发生在发出后至手机可见之间。证据为主工作树 `.omx/artifacts/orca-p1-release/wan-latency-pooled.json`。
- Windows 0.2.85 签名安装包完成隔离原生安装、daemon 启动和 WebView2 启动。Android 独立 QA 0.2.85（code 86）已安装并读回 APK SHA-256。云端试点目录已接收双端签名界面、Windows 安装包及 Android QA APK，逐项哈希一致。证据在主工作树 `.omx/artifacts/orca-p1-release/`。公开 stable 为界面 0.2.84、原生 0.2.83。
- 真实 Edge、隔离 Relay、daemon 和 SQLite 的混合版本矩阵验证了 0.2.84 界面配新后台，以及新界面配 0.2.84 后台。两组 7 秒内各发两次旧状态查询，OP26 均为零，均收到一次关联原 nonce 的持久回执。证据在主工作树 `.omx/artifacts/orca-p1-mixed/matrix.json`。
- JavaScript 全量 215 项通过。冻结 P1 的 Python 全量结果为 2074 通过、155 跳过、25 失败，另有 72 个子测试通过。已在原 0.2.84 对照失败入口，并在 P2 修正测试操作。三项 Windows Cursor `block` 与 `bar` 的断言差异在原 0.2.84 和 P2 均重现，沿用该方向的基线边界。原始摘要为主工作树 `.omx/artifacts/orca-p1-release/full-pytest-summary.txt`。

## P2 状态归属与恢复

P2 从试点提交 `eee23f953c13d19a40acede4d758ef897bd5ecf7` 开始，在 `codex/orca-p2-runtime-state` 工作树独立验证。冻结起点的后端事件和 Hub 测试 6 项、共享控制器测试 36 项通过。

- 目录、控制查询和运行时事件共用同一份状态投影。每份投影绑定原进程代次，变更后只生成一次。旧投影到达时保留较新的修订。事件继续携带有界的公开字段，权限详情由授权 OP24 查询。
- 两个独立 Windows daemon 进程使用同一个隔离 SQLite。首个进程在托管轮次进行中终止，第二个进程保留准确的原输入和持久 nonce 回执，发布新的 `runtime_epoch`。协商客户端读回 `runtime_verification: unverifiable`，旧端继续收到 `ambiguous`。SQLite 完整性通过，重启后台保留日志与回执，避免补写退出状态或重发任务。证据为主工作树 `.omx/artifacts/orca-p2-restart/restart.json`。
- 托管、Relay、协议与客户端回归 283 项及 23 个子测试通过。三个旧版历史测试按正常界面入口展开工具栏后通过原断言。Windows 0.2.85 签名候选在独立目录实测安装、daemon 与 WebView2 启动。
- 同一套 Android QA 前端经 5G NR、阿里云 Relay 和家庭 WLAN 连接精确 P2 后台源码。实际 Codex 保存一条输入和一条准确最终回复，重连携带修订 5，无重复提交。重开同一物理试点 SQLite 后原回执和最终回复仍可读，运行状态保持未核实。房间和临时数据已清理。
- 云端独立 P2 目录完成签名 Windows 安装包与双端界面暂存、SHA-256 读回。隔离更新存储两端均实测 0.2.84 → 0.2.85 → 恢复原 0.2.84 字节，防回退序号保留。证据在主工作树 `.omx/artifacts/orca-p2-release/`。
- 真实 Windows 签名安装器在隔离目录完成 0.2.83 → 0.2.85 → 0.2.83，两次切换期间已连接的测试 daemon 保留原进程，并回复 PONG。重启恢复版后仍使用原安装标识和状态目录。记录为 `.omx/artifacts/orca-p2-release/native-recovery/verification.json`。
- Android 历史、刷新、多主机、界面重载和真实 Codex 输入测试通过可见按钮进入工具栏。合并重跑 14 项通过。保留一条较早的 Codex 合并输出回显超时记录，0.2.84 和 P2 同一组合分别重复 6 次均通过，具体原因仍待确认。跨设备接管、方向键建议和本地全屏归档的原版失败也已复现，并通过正常可见的工具栏或非原生翻页会话重新验证。
- 在 0.2.84 与 P2 均复现两类测试时序问题。回执丢失测试只在故意丢回执时缩短等待。权限到期测试等待实际持久撤回完成，再断言旧权限已撤销。对延迟注入和原三项断言均完成复测。
- P2 的实际 Edge 混合版本三组通过，旧 UI/新后台及新 UI/旧后台保留轮询，新版组合空闲零状态查询。公开发布工具 60 项通过，1 项按环境条件跳过。
- 三十分钟稳定性测试验证十个页签 1800.010 秒保持就绪，周期 `managed_status`、空闲 OP26、重连和主线程长任务均为零。120 次 PONG 全部返回，p95 为 2.627 ms。31 个采样点中，状态轨迹数量和保留事件数量均为 10，队列为空。空闲结束后 10 个测试轮次各派发一次，产生 33 个 OP26，并重连恢复。证据为主工作树 `.omx/artifacts/orca-p2-soak/p2-soak.json`。
- Python 全量原始结果为 2080 通过、155 跳过、22 失败，另有 72 个子测试通过。其中 19 个在已修正的旧界面入口或测试时序下通过复测，三项 Cursor 光标断言仍与 0.2.84 相同。一次 PTY 测试退出警告在同一用例的基线和候选重跑中均未再出现。原始记录为 `.omx/artifacts/orca-p2-release/full-pytest.xml`。

### 0.2.85 stable 现场发布

- 阿里云先切换 Windows 完整程序，再切换两端的签名界面。已从公网回读 Windows 安装包和两个界面 ZIP，与候选文件逐字节一致，并核对签名。Relay `active/running`、重启计数 0，公网 `/health` 返回 200。
- 本机 Windows 默认安装从 0.2.36 升到 0.2.85。原配置指向已不存在的凭据文件，经同状态目录的现有凭据完成只读 owner HELLO 核验后修复路径，原凭据字节、状态目录和三条已运行进程均保留。证据为 `.omx/artifacts/orca-p2-release/windows/desktop-install.json`。
- 实体 Android 经 5G NR 激活并显示签名 UI 0.2.85，原生为 0.2.83（构建 85）。原房间已不在授权发现结果中，用同一保存凭据重新发现三个可用房间，并选择经本机只读核验的 Windows 房间，实际显示「已连接」。证据为 `.omx/artifacts/orca-p2-release/android-stable-readback.json`。

## P3 独立 WebRTC 技术验证

- Windows 家庭 WLAN 与实体 Android QA WebView 完成 UDP、ICE、DTLS 和 SCTP 合成回显。启用 mDNS 查询的 Pion 在同 LAN 无 STUN、WLAN 到 5G 两个场景准确回显 47 字节，单次回显往返分别为 6.323 和 45.516 ms。独立 IPv6-only 探针的实测为 49.981 ms。三个试验所选路径均为 IPv6，TURN 配置为空。ADB 只承载控制，DataChannel 承载合成回显。证据为主工作树 `.omx/artifacts/orca-p3-webrtc/p3-summary.json`。
- 初始 Pion 同 LAN 测试的 SDP 已有 8 个 host 候选，ICE 最后失败。将试验探针的 mDNS 调整为 QueryOnly 后，同一物理 WLAN 通过，并复查 5G。aiortc 在实际同 LAN 通过，在 WLAN 到 5G 的两次尝试超时。Pion 的实际 IPv4-only 尝试也超时，关闭试验后 Relay 仍可用。
- Pion 暂定为可选 sidecar 候选。校验的 Windows 单文件样本为 10,597,888 字节，aiortc 对照单文件为 49,412,219 字节，两者均实测本机回显。正式签名和安全软件兼容留在原生交付验收。对照来源为 [Pion API](https://pkg.go.dev/github.com/pion/webrtc/v4) 和 [aiortc API](https://aiortc.readthedocs.io/en/stable/api.html)。
- Windows 绑定实际 WLAN 后，两处公开 STUN 地址均未返回有效 IPv4 UDP Binding 响应。5G 手机在相应 WebView 试验仍产生 srflx 候选。结果只界定本次客户端路径，不判定运营商的 NAT 类型。[Cloudflare STUN 文档](https://developers.cloudflare.com/realtime/sfu/get-started/connection-patterns/) · [WebRTC STUN 样例](https://webrtc.github.io/samples/src/content/peerconnection/trickle-ice/)。
- 两端地址和端口相关映射、过滤的对称 NAT 模型在 Pion 进程内仿真。两端收到 STUN 响应，对照组完成回显，对称模型的直连在约 6 秒内超时。真实对称 NAT 的设备样本仍缺。验收需确认是否以隔离仿真覆盖该场景后进入 P4。证据为 `.omx/artifacts/orca-p3-webrtc/native-comparison/synthetic-nat-test.log`。
- 两个 QA 测试房间清理后，以原凭据查找结果均为零，临时凭据和状态已清理。手机 Wi‑Fi 恢复原关闭状态，正式 App 通过 5G 显示已连接。业务继续使用已经发布的 0.2.85 stable。
