---
title: Relay functional check 2026-11-10
type: Reference
category: reference
status: active
created: 2026-11-10
source_paths:
  - .omx/artifacts/relay-health-20261110/summary.json
  - .omx/artifacts/relay-health-20261110/probe-hosts.json
  - .omx/artifacts/relay-health-20261110/probe-browser.json
---

# 中继可列电脑但客户端无法进入的检查

> 2026-11-12 补充：用户实测对同一远端 `117.50.48.22:22`，本机默认 Meta 虚拟网卡路径断开，指定本机 WLAN Wi-Fi 网卡后收到 SSH 标识并完成密钥交换。以下仍是 11 月 10 日的历史诊断，不能据其默认路径失败推断所有 SSH 路径均不可用。根因与认证状态尚未确认，详见[交接第 7 节](../docs/handoff-2026-11-11-conversation-journal.md#7-ssh--服务器状态与未解决问题)。source：用户 2026-11-12 实测说明。

用户报告点击测试可列出全部电脑，实际连接失败。本轮只做功能探测，没有修改程序、重启服务器或向终端发送输入、尺寸、刷新、关闭命令；未保存终端正文或凭据。

证据：`https://117.50.48.22/health` 返回 200、`ok:true`、`role:relay`。授权目录有 5 台在线电脑，每台均通过 WSS 101 → relay ready → 主机二进制 HELLO／会话目录 → 单个已存在焦点会话快照 → 主机 PING/PONG。无 Origin、Android WebView Origin 和本地桌面 Origin 的对照均通过。使用的操作凭据与用户此前要求填入平板的凭据相同；没有记录值。

真实归档 Android UI 0.2.67 在 Chromium 中按原生文档来源和 CSP 模拟两种流程：旧单房间连接，以及原生“选中全部电脑、继续发现”。两者都连接 5 台电脑并进入 ready，未出现页面或 WebSocket 错误。页面所有终端输入及尺寸帧都被诊断保护拦截；本次未触发任何此类帧。相关多房间／动态发现回归 14 项通过。

边界：首页最初返回 502，稍后复查为 200，说明网页上游曾异常，但不能据此断言 `/ws` 转发服务停机；静态签名更新清单为 200。SSH 22 在身份验证前断开，未能检查真实 systemd 进程、Nginx 配置或服务器日志。以上是外部端到端功能证据，不是“所有服务进程都正常”的结论。

初步检查时客户端故障尚未复现，用户随后补充为 Windows 桌面，后续确认与恢复见下节。本轮没有盲目重启中继、关闭现有会话、放宽鉴权或增加超时。暂停的平板适配仍未恢复。

## 后续确认：Windows 桌面当前连接已恢复

用户补充为 Windows 本机客户端。通过现有运行窗口确认版本为 0.2.67、frontend 6771 → daemon 6769，测试发现 5 个房间，保存的正式目录连接一直显示“正在连接”。隔离真实工作区测试复现缺陷：手动 Token 目录重连未重置上一 socket 的 revision，中继计数降低后目录消息被持续丢弃；独立测试连接不继承旧计数，因此仍成功。

在运行界面临时取消本机重复房间、提交后重新勾回全部房间，触发授权范围更新和目录重连。最终全部 5 个房间仍选中，本机及 4 台远程电脑均显示已连接，共 20 个终端；已实际打开 Ewan/FULU 并看到同步画面。没有更改 Token、重启服务器、关闭后台会话或在终端输入命令。视图打开使用正常应用路径。

独立 `codex/relay-connection` 分支将 `connectDirectory` 的目录 revision 在每次新连接代次置零，保留同 socket 内旧版本过滤及旧代次回调检查；新回归先失败后通过，相关 Python 8 项、Node 2 项通过。完整签名 UI 候选 0.2.73 已生成；当前旧版进程是状态恢复，尚未加载永久补丁。源码/说明：`.omx/worktrees/relay-connection/src/supermonoagent/static/workspace.js`、`tests/test_workspace_directory_reconnect.py`、`docs/windows-directory-reconnect.md`。状态证据 `.omx/artifacts/relay-health-20261110/desktop-recovery.json`。平板 WIP 与 Claude 候选保持独立。
