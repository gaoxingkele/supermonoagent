---
type: Review
title: 三套 Windows 客户端与 Android 试用交付
status: active
updated: 2026-10-21
owner: llm
tags: [windows, android, terminal, verification]
source_paths:
  - src/supermonoagent/launcher.py
  - src/supermonoagent/workspace.py
  - src/supermonoagent/native_attach.py
  - android/src/dev/supermonoagent/mobile/MainActivity.java
  - tests/test_client_integration.py
  - dist/windows/build-manifest.json
confidence: high
---

# 交付与试用

2026-10-22 更新：下文为早期试用交付记录。当前 TD-001～TD-006 的产物、源码验证及手机安装状态以 [本轮验证记录](todo-completion.md) 为准，使用 `dist/windows-global-workspace/` 和 `dist/android-global-workspace/`。经典页面已同步更新认证边界，不能继续按下文早期基线说明判断当前源码。

本轮交付是三个 Windows 前端入口和一个 Android APK，共享同一套 daemon/Hub/ConPTY/中继。没有替换为微软的会话后端，也没有引入 Orca 私有 RPC。原有未提交代码已备份在本地 `.omx/baseline/`；经典网页 PAGE 与该基线完全一致。

## 产物

- `dist/windows/01-Browser.cmd`：原网页与后台。
- `dist/windows/02-Orca-Workspace.cmd`：Windows WebView2 工作区，项目/主机/会话侧栏、标签、拖动分屏、聚焦、尺寸接管与独立结束会话操作。
- `dist/windows/03-Intelligent-Terminal.cmd`：通过 wtai.exe 启动本机控制台适配器，连接同一 daemon 会话。Microsoft Intelligent Terminal 0.2.2572.0 已安装到本机当前用户；不包含在分发包中。
- Windows 共享运行包约 31.14 MiB，无需单独安装 Python；必须保留整个 `SuperMonoAgent/_internal` 目录。
- `dist/android/SuperMonoAgent.apk`：16,792 字节，本地调试签名，Android 8+。APK 内嵌系统 WebView 控件，没有打包完整 Chromium。校验与工具版本在同目录报告中。

详细启动、安装与构建见 [Windows](windows-clients.md)、[Android](android.md)。三套入口复用后台，关闭显示端不终止后台 shell。浏览器端与 APK 均可通过升级后的中继访问同一会话；DOM/WebGL 是该页面内部的绘制方式，可切换并显示实际采用的后端。

## 实现边界与试用前提

本轮没有重启或部署用户的现有公网中继。**已有远端 daemon 和 relay 需要更新到本轮版本，才能提供 `/workspace` 及协商式多会话协议。** 更新前请保留运行任务；不可把升级客户端当成远端服务已自动升级。仅本地试用时，三个 Windows 入口使用同一回环主机；若默认 6769 已运行旧版后台，可先在独立端口启动本轮后台，再让三个客户端连接它。

```powershell
# 在一个空闲端口试用，不停止已有 6769 主机
.\SuperMonoAgent\SuperMonoAgent.exe host --port 6779 --state-dir .\trial-state --no-relay
.\SuperMonoAgent\SuperMonoAgent.exe browser --port 6779 --no-start
.\SuperMonoAgent\SuperMonoAgent.exe workspace --port 6779 --no-start
# 原生适配器连接相同主机（token 文件属于这个独立主机）
.\SuperMonoAgent\SuperMonoAgent.exe microsoft --url ws://127.0.0.1:6779/ws --token-file .\trial-state\token --list
.\SuperMonoAgent\SuperMonoAgent.exe microsoft --url ws://127.0.0.1:6779/ws --token-file .\trial-state\token --session SESSION_ID
```

中继更新可以使用相同 EXE 的 `relay` 模式或现有 Python 部署方式，主机继续主动外联。公网服务的停机迁移、TLS 和远端配置不在本轮自动部署范围。用户测试机器和网络连接也不会被构建脚本修改。

## 正确性与性能证据

- 新客户端每页面一个数据连接，按 session 订阅；按键携带目标 session，不依赖易变的全局焦点。订阅、输入和尺寸修改均校验所属账户与频道。
- 尺寸采用单控制者租约；显式接管或控制端退出后才能转移。慢客户端/泵溢出在可靠模式下显式断开，不悄然丢终端增量。
- 快照与待发流通过字节位置衔接，避免新订阅者重复收到已重放内容。历史仍是有界原始输出尾部，**不是任意全屏应用的完整 VT 状态序列化**；界面明确提示这一限制。
- 协议帧保留四字节长度前缀；新终端直接消费 UTF-8 字节，不再次正则着色。重放/终端查询响应不会被当作 CMD/PowerShell 用户输入。
- 真实 WebSocket 集成测试验证了 PowerShell/CMD 经本地中继执行构造标记命令，并被桌面、手机式连接同时观察；关闭客户端不结束该会话。
- 冻结 EXE 的 CMD/PowerShell 命令执行、HTTP 静态资源、native 列表、微软客户端发现、隐藏 WebView2 加载均验证通过。打包显式包含 pywinpty 所需 OpenConsole.exe 与 winpty-agent.exe。
- 冻结版后台的实际浏览器输入、DOM/WebGL 切换、分屏、390×844 手机布局、960×640、关闭并重新打开视图均通过，未出现页面脚本错误；关闭视图释放 xterm/WebGL/DOM/观察器。
- 1/4/16 会话旧版每 pane 中继广播与单连接订阅的有效载荷复制比为 1×/4×/16×。16 会话单轮为 2,097,152 对 131,072 字节。不是相同比例的 CPU 或公网延迟提升，见 [中继基准](terminal-benchmarks.md)。
- 实际 DOM/WebGL 文本及单元格样式哈希一致；CJK/emoji 均为两格。WebGL 报告 Intel Arc/D3D11 路径，真实 context loss 后 DOM 回退保留内容。见 [渲染对比](renderer-comparison.md)。

可复现入口：`python -m pytest -q`、`node --test tests/workspace-client.test.cjs`、`scripts/benchmark-relay.py --compare-legacy`、`scripts/benchmark-renderers.py`、两套 `build-*.ps1`。详细原始报告保存在本机 `.omx/artifacts/` 和 `dist/` 的签名/构建清单中。

## 明确保留的验证空白

没有连接实体 Android 设备，因此未验证真实中文输入法、触控、软键盘、旋转、移动 GPU/耗电和网络切换。没有展示或操作 Intelligent Terminal 可见窗口，因此其实际渲染与输入法体验仍需对比试用；隐藏 Win32 控制台与真实共享 shell 桥接已测，但不等同于微软应用完整 UI 验收。微软 ACP 面板不会自动获得对 daemon 内部共享 shell 的管理能力。

没有对生产公网中继施加负载，不能声称已解决其实际 CPU 或广域网卡顿；当前已消除可测的重复扇出，并提供进一步测量脚本。Windows EXE 未签名；APK 使用本地调试签名，均为试用产物。
