# SuperMonoAgent

Windows 上的单进程主机。桌面只保留多窗口和多 CLI。手机和网页都能打开每个窗口。打开的窗口都会把输出转发到中转，远程切换时能看到最新内容。键盘输入只进该客户端当前正在看的那条 CLI。

## 运行

```powershell
.\.venv\Scripts\python.exe -m supermonoagent.relay
.\.venv\Scripts\python.exe -m supermonoagent --relay ws://127.0.0.1:6770
```

只在本机看：`python -m supermonoagent`。没有 Token 不能连。本机默认 Token 写在 `%USERPROFILE%\.supermonoagent\token`，本机页面会填上。用邮箱注册并登录会得到自己的 Token。每个 Token 只看自己的窗口和频道。

手机走中继时打开 `http://<relay>:6770/?code=<relay-code>`。code 在 `%USERPROFILE%\.supermonoagent\relay-code`。

公网中继在 `117.50.48.22`。程序目录 `/home/others/sma-relay`，systemd 单元 `sma-relay`，只听 `127.0.0.1:6770`。nginx 的 `8817` 把 `/` 反代到这台中继，`/GEO/` 仍走原来的 `8871`。页面是 `http://117.50.48.22:8817/?code=<code>`，WebSocket 是 `ws://117.50.48.22:8817/ws`。`/home/others/web` 没删。

## 布局

- `src/supermonoagent/web.py` 桌面。每个 CLI 是一张可拖动、可拉伸、可最大化和最小化的子窗口，按 z 序叠放。只有点中的那张接收键盘，其余继续收自己的输出。
- `src/supermonoagent/hub.py` 窗口、分组和 CLI。分组有名称和频道。直连窗口只收到自己这条 CLI 的原始输出。中继客户端订阅一个频道，只收该频道里打开的窗口，帧头带会话号，页面先剥掉再画。命令行预设和分组存在 `%USERPROFILE%\.supermonoagent\desktop.json`。
- `src/supermonoagent/daemon.py` 主机。网页每个连接自己选窗口。可以同时向外连接多台中继，列表在 `%USERPROFILE%\.supermonoagent\relays.json`。每台中继上的手机单独编号。
- `src/supermonoagent/pty.py` 每个 CLI 一个 ConPTY，读线程阻塞在 `read`。
- `src/supermonoagent/pump.py` 每条打开的会话按 20ms 合并输出。键盘输入仍只写进当前看着的那条 CLI。
- `src/supermonoagent/relay.py` 按手机编号转发，不解码终端内容。
- `study/orca` 是当前提交的完整源码，已 gitignore。历史仍是 depth 1。

## 文档

- `docs/protocol.md` 线协议：帧格式、opcode、中继消息、背压、限额、安全现状与收紧落点。
- `docs/architecture.md` 代码说明：进程线程模型、模块分工、数据流、状态文件、关键不变量。

## 不做

不实现 Orca Mobile 的私有协议。不在默认路径上套 Docker。不在事件循环里轮询。
