---
title: "Windows 0.2.80 修改日志与发布状态"
type: Reference
category: reference
status: active
owner: llm
created: 2026-11-19
updated: 2026-11-19
tags: [windows, android, release, changelog, managed-recording]
source_paths: ["dist/README.md", "dist/versions.json", "dist/windows/0.2.80/build-manifest.json", "memory/project_status.md", ".omx/worktrees/conversation-journal/HANDOFF.md", ".omx/worktrees/conversation-journal/.omx/artifacts/managed-real-acceptance-latest.json"]
source_urls: ["https://github.com/gaoxingkele/supermonoagent/commit/847dc2f", "https://github.com/gaoxingkele/supermonoagent/commit/b2cb3b5", "https://117.50.48.22/updates/stable/manifest.json", "https://117.50.48.22/updates/windows/stable/manifest.json"]
links: ["session-handoff-2026-11-11.md"]
confidence: high
schemaVersion: 1
---

# Windows 0.2.80 修改日志与发布状态

## 0.2.80 相比 0.2.79 的主要改动

- 加入 Codex 和 Claude Code 的管理式会话记录，保存结构化输入、回执和输出。原始 CLI 窗口仍可正常交互。 [终端交互约定](../.omx/worktrees/conversation-journal/docs/unified-terminal.md)
- Claude Code 在备用屏幕中使用自身的翻页机制。应用启用鼠标跟踪后转发滚轮事件，启用前发送翻页键。 [功能提交 `847dc2f`](https://github.com/gaoxingkele/supermonoagent/commit/847dc2f)
- 桌面端点击终端里的 HTTP(S) 链接会弹出菜单，可用当前设备或主机的浏览器打开。主机选项取决于后台是否支持对应操作。 [功能提交 `847dc2f`](https://github.com/gaoxingkele/supermonoagent/commit/847dc2f)

## 发布与验证

- Windows 最新构建为 **0.2.80**，于 2026-11-18 00:15（UTC+8）从 `codex/conversation-journal` 的 `05be7b4` 构建。产物提交 `b2cb3b5` 已推送到 `codex/tablet-adaptation-wip`。公共 ZIP、EXE 和构建清单中的 97 项源码 SHA-256 已复核，均匹配。 [公共安装包](../dist/windows/SuperMonoAgent-0.2.80-Setup.zip) · [产物索引](../dist/README.md) · [产物提交](https://github.com/gaoxingkele/supermonoagent/commit/b2cb3b5)
- 2026-11-19 读取线上清单，Windows 完整程序与签名界面的稳定频道均为 **0.2.67**。Android 最新 APK 和最新签名界面候选均为 **0.2.78**。0.2.78 曾在 Honor FLC-AN00 上完成覆盖安装和连接检查。 [Windows 稳定清单](https://117.50.48.22/updates/windows/stable/manifest.json) · [界面稳定清单](https://117.50.48.22/updates/stable/manifest.json) · [Android 真机交接](../.omx/worktrees/conversation-journal/HANDOFF.md)
- 冻结包的三个记录服务入口通过 `--help` 冒烟检查。现存真实 CLI 验证中，Codex 已获得并记录最终输出，Claude 已接收输入但未获得最终输出。0.2.80 的 Windows 安装后整体验收和 Claude 管理式端到端验收待完成。 [构建记录](../dist/README.md#0280-构建说明) · [真实-CLI 验证记录](../.omx/worktrees/conversation-journal/.omx/artifacts/managed-real-acceptance-latest.json)
- 当前工作树的端口占用自动换端口、原始输入调整属于后续工作。已预填 Token 的切换行为见项目状态。Cursor CLI 切换后的滚动和历史回放异常仍待定位。 [项目状态](../memory/project_status.md)

相关交接见 [[session-handoff-2026-11-11]]。
