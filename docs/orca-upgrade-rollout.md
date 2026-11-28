# Orca 升级实施与发布记录

实施依据为 [P0–P6 开发计划](orca-upgrade-and-direct-plan.md)。每个阶段保存源码版本、候选产物、兼容性、实机或远端回读以及回滚证据。阶段产物按验证范围发布。

| 阶段 | 发布目标 | 状态 |
| --- | --- | --- |
| P0 基线和回滚 | 0.2.84 界面、0.2.83 原生包的回归与恢复记录 | 通过 |
| P1 运行时事件 | 0.2.85 后台候选，随后双端界面候选 | 实施中 |
| P2 统一状态 | 0.2.85 完整候选，验证后逐端升级 | 待 P1 |
| P3 WebRTC 验证 | 独立 QA 包、状态目录和 Relay 测试房间 | 待 P2 |
| P4 本地 Bot 与 Worker | 0.2.86 原生候选，完成接管和重启恢复后升级 | 待 P3 |
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
