---
title: "Windows hidden startup"
type: Decision
status: active
owner: llm
tags: ["windows", "startup", "console", "background", "ConPTY"]
created: 2026-10-21T14:56:07.243Z
updated: 2026-10-21T14:56:07.243Z
sources: ["src/supermonoagent/desktop.py", "src/supermonoagent/frontend.py", "tests/test_hidden_startup.py", ".omx/artifacts/hidden-startup/smoke.json"]
source_paths: ["src/supermonoagent/desktop.py", "src/supermonoagent/frontend.py", "scripts/launch-hidden.vbs", "scripts/build-windows.ps1", "tests/test_hidden_startup.py", ".omx/artifacts/hidden-startup/tests.txt", ".omx/artifacts/hidden-startup/smoke.json", ".omx/artifacts/hidden-startup/hidden-windows.json"]
source_urls: ["https://learn.microsoft.com/en-us/windows/win32/procthread/process-creation-flags"]
links: ["llm-log-2026-10-21.md"]
category: decision
confidence: high
schemaVersion: 1
---

# 隐藏空控制台，保留真实终端

## 决策
图形桌面、浏览器入口和后台服务不应留下额外空控制台。交互式 Microsoft/native-attach 仍保留控制台。隐藏窗口不等于结束后台或关闭会话。

## 根因与修复
- 控制台型 EXE 通过同步 CMD 脚本启动，脚本等待图形程序退出而留下空窗。图形入口改为 WScript 隐藏启动，并提供无需 CMD 的 VBS 入口。
- 后台同时设置 DETACHED_PROCESS 和 CREATE_NO_WINDOW，前者会使后者失效。后台只使用 CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW。
- 保留控制台型主 EXE，避免把 native-attach 所需控制台一起去掉；诊断仍可直接执行 EXE。

## 使用入口
- [无控制台桌面入口](../dist/windows-hidden/02-Orca-Workspace.vbs)
- [无控制台浏览器入口](../dist/windows-hidden/01-Browser.vbs)
- [Windows 使用说明](../docs/windows-clients.md)
- 重建：powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build-windows.ps1 -OutputDir dist/windows-hidden

## 代码证据
- [desktop.py](../src/supermonoagent/desktop.py)：后台主机创建标志。
- [frontend.py](../src/supermonoagent/frontend.py)：后台 UI 服务创建标志。
- [launch-hidden.vbs](../scripts/launch-hidden.vbs)：隐藏运行、参数转义和失败提示。
- [build-windows.ps1](../scripts/build-windows.ps1)：独立输出目录、CMD/VBS 入口生成。
- [启动回归测试](../tests/test_hidden_startup.py)：实际 WSH 启动的控制台可见性，以及参数传递、创建标志。
- 官方依据：https://learn.microsoft.com/en-us/windows/win32/procthread/process-creation-flags

## 验证与边界
- 本次知识回写时重新运行相关回归，**20 项测试、6 个子测试通过**：[tests.txt](../.omx/artifacts/hidden-startup/tests.txt)。这次复核不修改产品代码或重新操作窗口。
- 冻结 WebView2 隐藏加载成功，测试后台仍存活，可见新控制台为零：[smoke.json](../.omx/artifacts/hidden-startup/smoke.json)。
- 已对可确认属于项目的 3 个空控制台执行隐藏，没有结束进程或重启已有服务：[隐藏记录](../.omx/artifacts/hidden-startup/hidden-windows.json)。这是当时的执行记录，不代表这些 HWND/PID 以后仍有效。
- 不按 WindowsTerminal 的进程 PID 整体隐藏或终止；同一宿主进程可能包含用户其他窗口。不得把真正的交互终端当作空启动器处理。

## 后续修改约束
不要同时设置 DETACHED_PROCESS 与 CREATE_NO_WINDOW；不要将所有 EXE 改为 windowed 而破坏 native-attach；不要为消除空窗而重启或杀掉承载会话的 daemon。

相关：[[llm-log-2026-10-21]]。
