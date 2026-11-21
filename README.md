# SuperMonoAgent

Windows 完整 ZIP 一键安装及默认配置注入见 [安装说明](docs/windows-oneclick-install.md)。专用下载包自动配置默认中继/Token，新电脑启动时用本机名称注册；通用公开包不包含私有 Token。

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

## 多机驾驶舱试点

桌面从「菜单 → 驾驶舱」进入，安卓从终端设置（⋯）进入。填写运维配置的 `wss://服务地址/hub/view`、项目 ID 或 `*`、单独签发的只读凭据。本机隔离测试可使用 `ws://127.0.0.1:端口/hub/view`。连接后顶栏出现快捷入口。此设备只保存连接地址和项目选择，读凭据随本次会话使用。服务需允许该桌面或安卓文档的实际 Origin。

「对话」向总管查询项目证据。「待处理」可切换全部任务，查看具体执行与项目 wiki 版本。「额度」按授权额度池显示当前可用量、周重置和采集时间，失效的读数放在历史快照内。「mylib」可阅读技能与进化记录；原生桌面默认读取本机的 `D:\aicoding\mylib`，在「目录设置」更改，中央技能库可另行选择。安卓先提供只读视图。

桌面在任务详情中选中具体执行后可进入反馈预览。确认凭据与驾驶舱只读凭据独立，支持的投递或草稿操作由目标宿主实际能力决定。终端窗口绑定不完整时请先在来源机补齐绑定。返回或取消会保留原终端会话。

## 和 Orca Mobile 的关系

连通方式相同：主机是主人，手机是客户端，NAT 后面用向外的 WebSocket。线协议不同，官方 Orca App 连不上这个主机。官方协议绑着 E2EE 握手和一整份桌面 RPC，留着它就留着那套 CPU。

学习笔记在 `study/NOTES.md`。Orca 源码用 `scripts/pull-orca.ps1` 拉到 `study/orca`，是当前提交的完整工作区，历史仍是 depth 1。

## 文档

- [项目 Wiki](omx_wiki/index.md)：维护决策、代码证据及后续修改约束。
- [LLM 修改日志](omx_wiki/llm-log-2026-10-21.md)：近期 Android 升级、Windows 空控制台修复和验证记录。

- `docs/protocol.md` 线协议：帧格式、19 个 opcode、中继链路消息、背压与限额、安全现状。
- `docs/architecture.md` 代码说明：进程线程模型、模块分工、数据流、状态文件、关键不变量。
