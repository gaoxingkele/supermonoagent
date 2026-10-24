# SuperMonoAgent

Windows 客户端与 Android APK：启动方法见 [Windows 说明](docs/windows-clients.md)、[Android 安装](docs/android.md)、[构建产物](dist/README.md)。工作区入口为 `/workspace`；当前功能与验收边界见 [TODO](docs/TODO.md)。公网 HTTPS/WSS 和签名界面更新频道已部署。

在 Windows 上用一个进程接住多窗口、多条 CLI。网页和手机都能进入每个窗口。工作区左侧按电脑与房间列出窗口，支持收起和展开。电脑把终端输出推送到中继，客户端使用 Token 和房间号连接；键盘输入只进当前会话。

推送使用现有 WebSocket 中继。终端内容作为连续二进制流传输；客户端只订阅已获授权的电脑和会话。

## 启动

Windows 桌面端在 EXE 所在目录只需两个选项：

```powershell
.\SuperMonoAgent.exe --relay wss://117.50.48.22/ws --token "YOUR_TOKEN"
```

默认使用本地 `127.0.0.1:6769` 后台；首次配置的房间号默认取本机名称，已有保存的房间号保留。连接页面填写 IP/地址、默认端口和 Token，点击“测试连接”后默认勾选全部授权房间，再连接所选；Token 默认记住，可取消。

```powershell
D:\Python314\python.exe -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m supermonoagent --host 127.0.0.1 --port 6769 --relay wss://117.50.48.22/ws
```

- 电脑浏览器打开 http://127.0.0.1:6769/ ，本机 Token 会填上。
- 手机使用 Android 客户端，或打开 https://117.50.48.22/ 。房间码在 `%USERPROFILE%\.supermonoagent\relay-code`；连接时填写对应 Token。
- 页面先建窗口，再在窗口里打开 PowerShell、cmd、pwsh、claude、codex。每个打开的窗口都持续把输出送到中转。同一窗口里两个客户端可以各看一条 CLI，键盘只进自己正在看的那条。关窗口、关 CLI 会停掉对应进程。按键、Tab、方向键和 Ctrl+C 进当前会话，终端大小随浏览器变化。

不连中继时，只跑 `python -m supermonoagent`。默认只听 `127.0.0.1`。给手机局域网用时加上 `--host 0.0.0.0`。

## 和 Orca Mobile 的关系

连通方式相同：主机是主人，手机是客户端，NAT 后面用向外的 WebSocket。线协议不同，官方 Orca App 连不上这个主机。官方协议绑着 E2EE 握手和一整份桌面 RPC，留着它就留着那套 CPU。

学习笔记在 `study/NOTES.md`。Orca 源码用 `scripts/pull-orca.ps1` 拉到 `study/orca`，是当前提交的完整工作区，历史仍是 depth 1。

## 文档

- [项目 Wiki](omx_wiki/index.md)：维护决策、代码证据及后续修改约束。
- [LLM 修改日志](omx_wiki/llm-log-2026-10-21.md)：近期 Android 升级、Windows 空控制台修复和验证记录。

- `docs/protocol.md` 线协议：帧格式、19 个 opcode、中继链路消息、背压与限额、安全现状。
- `docs/architecture.md` 代码说明：进程线程模型、模块分工、数据流、状态文件、关键不变量。
