---
title: Free terminal task notifications
category: decision
updated: 2026-10-22
tags: [android, notifications, relay, terminal]
---

# 免费任务通知与兼容桥

用户选择复用现有中继，避免付费推送和 Google/厂商账号依赖。Android 使用用户显式开启的 specialUse 前台服务，保留可见停止入口；不依赖 WebView 后台运行。

实现、维护命令及当前限制以 [通知文档](../docs/task-notifications.md) 为准。证据源：[task_events.py](../src/supermonoagent/task_events.py)、[notification_bridge.py](../src/supermonoagent/notification_bridge.py)、[TaskNotifications.java](../android/src/dev/supermonoagent/mobile/TaskNotifications.java)。

关键约束：

- 旧 Windows daemon 仍承载用户 PTY，不能为更新通知重启它。兼容桥在本机连接旧 daemon，必须带 Authorization Bearer 请求头，并发送二进制 HELLO；仅有 HELLO 会被旧实例拒绝。桥通过原中继的独立 `原房间-events` 路由轻量通知，手机不订阅全部终端输出。
- 明确 OSC 7501 working→idle 表示 agent 返回等待输入，不证明测试或业务成功。启动、快照、重连和静默均不能触发完成。当前没有普通 CLI 通用任务结束识别，不将 Kimi 等未验证程序宣称为已支持。
- 通知只提供通用结果提示，点击原电脑/终端，不创建 shell、不执行文字。手机当前只监测一个电脑的全部终端，改变目标会替换连接。
- 保留有界事件、游标和去重；不持久保存新的监测 token。服务被强制停止/系统结束后需重新开启。不能以短时锁屏或 USB 充电测试推断长期待机送达率。
- 当前既有公网地址仍是 WS，新增功能没有解决公网 TLS 或提供端到端加密。凭据不进入 URL、日志或通知正文。
- 安卓首次安装需系统指纹时必须由用户完成。荣耀密码保险箱可能在输入测试凭据后弹出，自动化只能取消，不能保存测试或真实 token。

测试源：[test_task_events.py](../tests/test_task_events.py)、[test_notification_bridge.py](../tests/test_notification_bridge.py)、[test_android_notifications.py](../tests/test_android_notifications.py)、[实机探针](../scripts/test-android-notifications.py)。实机记录在 `.omx/artifacts/terminal-notifications/`；不能把待实施的规划验收指标写成已完成结果。
