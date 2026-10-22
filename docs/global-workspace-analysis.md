# 全局桌面工作区互联分析

日期：2026-10-21
状态：初始分析存档；2026-10-22 已实施，当前状态见 [验证记录](todo-completion.md) 与 [安全设计](global-workspace-security.md)。下文的“当前模式”描述分析时的旧实现。
范围：Windows 桌面端、浏览器工作区、Android 端、daemon 与 WebSocket relay

## 目标

让一台 Windows 桌面端不仅能连接本机 daemon，也能同时看到并操作其他电脑上正在运行的 SuperMonoAgent 桌面终端。手机端与桌面端应看到同一组电脑、项目和终端，并能在权限允许时操作任意一台电脑上的会话。

这里的“全局统一”指客户端目录、状态和操作能力统一；不是把所有电脑上的 shell 合并成一个进程，也不是让 relay 解码或合并终端内容。

## 结论

**可以做，而且现有架构已经具备大部分底层能力。** 当前桌面和 Android 都已支持保存多台电脑、每台电脑独立 Token、通过房间码连接远端 daemon，并在切换时清理旧视图。relay 也已保存每台主机的机器名和项目名。

当前模式的本质是：

```text
客户端一次连接一台电脑 → 切换电脑时断开旧连接并清空视图
```

目标模式需要变成：

```text
客户端同时连接多台电脑 → 每台电脑一条连接 → UI 按电脑聚合所有项目和终端
```

最小可行方案可以先只改桌面 workspace 和 Android 前端，不改 daemon、relay 转发协议或终端帧格式。

## 当前已有能力

| 能力 | 当前状态 | 位置 |
| --- | --- | --- |
| daemon 持有本机全部 PTY | 已实现 | `src/supermonoagent/daemon.py`、`hub.py`、`pty.py` |
| 一条连接订阅多会话、定向输入 | 已实现 | `protocol.py` OP16/OP17、`hub.py` |
| relay 按房间转发 host 与 phone | 已实现 | `relay.py` |
| host 上报机器名和项目名 | 已实现 | `daemon.py`、`relay.py` |
| relay 提供项目/主机目录 | 已实现但未接入 workspace | `relay.py` 的 `/health` |
| 桌面保存多台电脑并切换 | 已实现 | `static/workspace.js`、`docs/workspace-hosts.md` |
| 每台电脑独立 Token | 已实现 | `workspace.js` 的 per-host token 存储 |
| Android 保存多台电脑并切换 | 已实现 | `android/assets/mobile/app.js` |
| 相同会话号防串台 | 切换式场景已有回归 | `tests/test_workspace_hosts.py` |

## 关键缺口

### 1. 客户端仍是单连接切换模型

`workspace.js` 当前只有全局 `ws`、`catalog`、`controls` 和 `panes`。切换电脑时会清空旧连接和视图。Android 的 `chooseHost()` 也采用先断开再连接的方式。

全局感知要求每台已保存电脑维持一条连接，并把连接状态、目录、终端视图和尺寸租约分别保存。

### 2. 会话缺少跨主机命名空间

会话 ID 只在单台 daemon 内唯一。两台电脑可能使用相同的 session id。当前切换清场可以规避；同时连接后必须把内部键改为：

```text
hostId + ":" + sessionId
```

订阅、输出分发、输入、resize、关闭视图和结束会话都必须按 host 分流。

### 3. workspace 尚未使用 relay 主机目录

relay `/health` 已返回 `hosts` 与 `projects`，旧版页面也已有“发现电脑/选择房间”的流程；新版 workspace 仍要求用户手工输入房间码。

### 4. 目录接口当前不适合公网公开

`relay.py` 的 `/health` 会列出所有活跃房间码、机器名和项目名，没有鉴权。若把全局发现做成正式能力，必须先限制目录访问，或只返回用户已知房间的在线状态。

### 5. 跨主机身份仍不统一

账户与 Token 保存在每台 daemon 本地。同一用户在多台电脑上没有统一身份；relay 只校验目标房间 Token，不负责跨房间授权。聚合客户端会同时持有多台电脑的 Token，本地存储泄露的影响范围也会随之扩大。

### 6. 主机上下线没有推送

客户端只能连接目标房间或轮询 relay `/health`。relay 不会向已连接客户端推送“某台电脑上线/离线”的目录变化。

## 推荐架构

### 第一阶段：客户端并行聚合，服务端零改动

桌面 workspace 将当前单连接闭包状态抽成按 host 隔离的连接对象：

```text
HostConnection
├── id / name / relay / code / token
├── ws / reconnect / meter / phase
├── catalog / groups / controls
└── subscriptions / output queues
```

视图层继续使用现有 xterm pane，但 pane 键改为 `hostId:sessionId`。侧栏改为：

```text
项目
└── 电脑（在线状态、RTT、速率）
    └── 分组
        └── 终端会话
```

每个 pane 标题显示电脑名，输入和 resize 发送到对应连接。结束会话等破坏性操作必须明确显示目标电脑。

Android 使用同一思路，但手机上仍只渲染一个活动终端；其他主机保持轻量目录连接和心跳，只有打开的会话订阅输出，避免耗电和流量放大。

### 第二阶段：发现和命名

把 relay `/health` 的项目/房间目录接入 workspace 的“增加工作区”对话框。目录接口先增加访问控制或改为“已知房间在线状态查询”。项目名作为跨电脑归组依据，但需要明确它只是用户命名，不是全局唯一 ID。

### 第三阶段：统一身份和状态推送

可选方向：

1. relay 增加已鉴权目录订阅，向 phone 推送房间上下线。
2. 建立一个账户 Token 可分发到多台 daemon 的机制。
3. 仍保持 relay 不解析终端帧，不把所有房间的终端数据合并到一个转发流。

### 第四阶段：生产安全加固

- relay 与 daemon 使用 `wss://`/TLS。
- Token 不再放入 URL 查询串，改为 WebSocket 握手帧或 Authorization。
- 校验浏览器 WS Origin。
- 目录接口鉴权，不公开全部房间码。
- 更新 `docs/protocol.md` 与 `docs/architecture.md`；当前文档的部分安全描述已滞后于代码。

## 安全与边界

- 聚合视图会让一个客户端同时持有多台电脑的操作权限，必须逐台保存 Token，并明确显示当前操作目标。
- relay 不是端到端加密边界；它可以接触原始终端字节。全局能力不能改变这个信任模型。
- 手机端并行连接会增加耗电、流量和重连复杂度，应只在打开聚合视图时保持全部目录连接。
- 跨主机聚合不应合并 shell，也不应让一台电脑的客户端直接控制另一台电脑的本地窗口管理。
- 远端 daemon 或 relay 版本过旧时，对应电脑显示“协议不兼容”，不能影响其他已连接电脑。

## 测试与验收

最低验收使用两台隔离 daemon 和一个本地 relay：

1. 两台电脑强制产生相同 session id，同时连接、同时输出，画面不串。
2. 向左边电脑输入只写入左边 PTY，向右边电脑输入只写入右边 PTY。
3. 断开其中一台电脑的连接，另一台的目录、输入和输出不受影响。
4. 一台电脑 4001 验证失败，另一台保持在线。
5. 每台电脑显示独立 RTT、速率和在线状态。
6. Token 只保存在对应 host 的存储键中，不被另一台电脑使用。
7. Android 上聚合目录可浏览，活动终端切换不结束远端进程。
8. 4 台以上主机时统计空闲心跳字节数、重连频率和内存增长。

## 实施判断

第一阶段是最小可行版本，主要修改：

- `src/supermonoagent/static/workspace.js`
- `src/supermonoagent/workspace.py`
- `src/supermonoagent/static/workspace.css`
- `android/assets/mobile/app.js`
- 对应 Playwright 与 Android 页面测试

daemon、relay 转发和线协议可以先保持不变。发现目录、统一身份与目录推送属于后续增强，不应阻塞第一版“同时看到并操作多台电脑”。
