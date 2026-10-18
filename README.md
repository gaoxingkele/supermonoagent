# SuperMonoAgent

在 Windows 上用一个进程接住多窗口、多条 CLI。网页和手机都能进入每个窗口。左侧分组可以命名，一个分组就是一个推送频道。电脑是生产者，手机和网页订阅频道后操作终端、看实时输出。键盘输入只进当前正在看的那条 CLI。

推送仍用现有的 WebSocket 中继，不另开 MQTT。终端是连续的二进制流，再套一个 broker 会多一个常驻进程，按键也多一跳。频道名写在分组上，订阅端只收这个频道。

## 启动

```powershell
D:\Python314\python.exe -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m supermonoagent.relay --port 6770
.\.venv\Scripts\python.exe -m supermonoagent --port 6769 --relay ws://127.0.0.1:6770
```

- 电脑浏览器打开守护进程打印的地址，token 在查询串里。
- 手机打开中继打印的页面，地址带 `?code=`，code 写在 `%USERPROFILE%\.supermonoagent\relay-code`。
- 页面先建窗口，再在窗口里打开 PowerShell、cmd、pwsh、claude、codex。每个打开的窗口都持续把输出送到中转。同一窗口里两个客户端可以各看一条 CLI，键盘只进自己正在看的那条。关窗口、关 CLI 会停掉对应进程。按键、Tab、方向键和 Ctrl+C 进当前会话，终端大小随浏览器变化。

不连中继时，只跑 `python -m supermonoagent`。默认只听 `127.0.0.1`。给手机局域网用时加上 `--host 0.0.0.0`。

## 和 Orca Mobile 的关系

连通方式相同：主机是主人，手机是客户端，NAT 后面用向外的 WebSocket。线协议不同，官方 Orca App 连不上这个主机。官方协议绑着 E2EE 握手和一整份桌面 RPC，留着它就留着那套 CPU。

学习笔记在 `study/NOTES.md`。Orca 源码用 `scripts/pull-orca.ps1` 拉到 `study/orca`，是当前提交的完整工作区，历史仍是 depth 1。

## 文档

- `docs/protocol.md` 线协议：帧格式、15 个 opcode、中继链路消息、背压与限额、安全现状。
- `docs/architecture.md` 代码说明：进程线程模型、模块分工、数据流、状态文件、关键不变量。
