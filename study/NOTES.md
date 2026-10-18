# 从 Orca 配对模型里留下的东西

学习副本在 `study/orca`（当前提交的完整工作区，历史仍是 depth 1，不进 git）。对照过的文件：

- `cloud/README.md`：手机和桌面各自向外连中继，中继配对后拼接帧，不让两端互打洞。
- `cloud/packages/relay-contract/src/splice-state-machine.ts`：正式拼接前要走 8 个状态，两边转发函数都装上才允许确认。
- `cloud/packages/relay-contract/src/protocol-limits.ts`：单帧上限 8MiB，因为桌面会把整个 worktree 目录塞进一条加密帧。
- `mobile/src/transport/direct-socket-framing.ts`：直连还有 E2EE v2 / legacy 两套握手。
- `src/main/daemon/AGENTS.md`：终端守护进程和 Electron 是两个进程，Unix socket 的名字所有权单独做了一套协议，避免把活着的 PTY 端点删掉。

SuperMonoAgent 保留的连通性：

- 主机是会话主人。手机只是客户端。
- 局域网直连，或者两边都向外连同一个中继。
- 中继在 `ready` 之前不转发。宿主还没到，手机直接被拒绝。
- 终端字节是长度前缀的二进制帧。中继不解码。

故意不搬的复杂度：

- 不兼容 Orca Mobile 那套版本化 RPC 和 E2EE。那是 Electron 桌面的私有协议，接上它就等于把 Orca 的 Windows 进程模型再做一遍。
- 没有 director、区域迁移、PostgreSQL、推送网关。
- 没有第二个 daemon 进程，也就没有 socket 名字抢占。
- 单帧上限 256KiB。终端输出先在主机上按 20ms 合并，撑满则改发环形缓冲快照，而不是把 8MiB 目录帧转发出去。

从另外两个项目拿来的点：

- Paseo：一个 daemon 上可以挂多个客户端；Claude / Codex 是本机 CLI，原样启动。
- OpenMonoAgent：容器是以后的可选沙箱，不是 Windows 日常路径。默认就是本机 PowerShell。
