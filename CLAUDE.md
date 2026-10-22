# SuperMonoAgent

Windows 上的单进程主机。桌面只保留多窗口和多 CLI。手机和网页都能打开每个窗口。打开的窗口都会把输出转发到中转，远程切换时能看到最新内容。键盘输入只进该客户端当前正在看的那条 CLI。

## 运行

```powershell
.\.venv\Scripts\python.exe -m supermonoagent --host 127.0.0.1 --port 6769 --relay ws://117.50.48.22:8817/ws --code 61914c
```

只在本机看：`python -m supermonoagent`。没有 Token 不能连。本机默认 Token 写在 `%USERPROFILE%\.supermonoagent\token`，本机页面会填上。用邮箱注册并登录会得到自己的 Token。每个 Token 只看自己的窗口和频道。

Windows 桌面窗口（WebView2，不开系统浏览器）：

```powershell
.\.venv\Scripts\python.exe -m pip install pywebview
.\.venv\Scripts\python.exe -m supermonoagent.desktop
```

6769 上已有主机就直接打开 `/?shell=desktop`。没有主机时这个命令会另起一个，关掉窗口不结束主机。浏览器里的网页照旧，不带 `shell=desktop`。

手机打开 `http://117.50.48.22:8817/?code=61914c`。房间码也在 `%USERPROFILE%\.supermonoagent\relay-code`。

公网中继在 `117.50.48.22`。程序目录 `/home/others/sma-relay`，systemd 单元 `sma-relay`，只听 `127.0.0.1:6770`。nginx 的 `8817` 把 `/` 反代到这台中继，`/GEO/` 仍走原来的 `8871`。页面是 `http://117.50.48.22:8817/?code=<code>`，WebSocket 是 `ws://117.50.48.22:8817/ws`。`/home/others/web` 没删。

## 布局

- `src/supermonoagent/web.py` 桌面。每个 CLI 是一张可拖动、可拉伸、可最大化和最小化的子窗口，按 z 序叠放。只有点中的那张接收键盘，其余继续收自己的输出。顶部「窗口」菜单排列当前 Project 里没有最小化的窗口：层叠 Ctrl+Alt+L，横向平铺 Ctrl+Alt+H，纵向平铺 Ctrl+Alt+V，网格平铺 Ctrl+Alt+G。顶栏左边有「收起菜单 / 展开菜单」，收起后按钮留在侧栏右边。窄屏上顶栏默认收起，点「展开菜单」打开，点进终端会再藏起来；侧栏点「侧栏 / 收起」开合，当前窗口铺满可视区域。`?shell=desktop` 时终端用 WebGL 画字形，中文宽度走 Unicode 11，字体用 Cascadia Mono。
- `src/supermonoagent/desktop.py` Windows 桌面窗口。用 WebView2 打开同一张页面，不替换网页，也不把协议改成 Electron。
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
