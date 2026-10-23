---
type: Decision
status: active
created: 2026-10-24
source_paths:
  - src/supermonoagent/token_config.py
  - android/src/dev/supermonoagent/mobile/RendererRecovery.java
  - android/src/dev/supermonoagent/mobile/RemoteUpdates.java
  - tests/test_workspace_paging.py
  - tests/test_codex_owned_paging.py
---

# 运行时配置与回滚验收

## Token 与房间

当前授权方式为 Token＋房间号，统一邮箱账户生产部署不再是交付前提。默认 Token 运行时读取 `token.txt`，不嵌入公开 APK、EXE 或界面包。文件定位配置只保存路径；后台所有者鉴权成功后才向本机界面提供默认值，远程目录不能覆盖默认值。不同目标不能继承自动预填凭据；保存凭据仍需明确选择。来源：`src/supermonoagent/token_config.py`、`frontend.py`、`static/workspace.js`。

本机实际文件位于 `dist/windows/0.2.24/token.txt`，已被 Git 忽略。私有路径配置已接入；真实后台鉴权及 Windows 0.2.28 打包界面预填验证通过。原始证据：`.omx/artifacts/token-room-provision/{runtime-source.json,live-owner-wire.json,packaged-default-ui.json}`。不得将文件内容或摘要写入公共验证报告。

## 翻屏

重连后 pane 回调必须读取当前连接代际，不能长期捕获创建时的代际。普通缓冲区使用本地历史；CSI 3J 不清除有界历史。备用屏仅对实际识别的 Codex 发送导航键，用户选择配色或修改标题不足以证明实际程序类型。来源：`static/workspace.js`、`tests/test_workspace_paging.py`。

实际 Codex 0.164.0 在隔离用户目录恢复 48 组 fixture 对话，四种导航键均验证，未发送换行或模型请求。边界：不能据此声称所有全屏 CLI 都支持同样的按键。证据：`.omx/artifacts/codex-owned-paging/real-codex.json`。

## Android 回滚：源码通过，修复后真机验收仍待完成

旧 QA 0.0.2 的正常 good 更新通过，但 throw 更新错误地回到了 bundled。已确认 good 备份的 16 个文件完整且 Java 验签可加载，不能把此失败归因于备份损坏。证据：`.omx/artifacts/android-ui-rollback-qa/qa-20261023T153711Z-00fddc0833a0/{physical-verification.json,cached-good2-integrity.json,cached-good2-java-verification.txt}`。

修复通过 renderer 身份和代际区分旧视图：普通 JavaScript 异常复用健康 renderer；挂死时先等待旧 renderer 的死亡回调，再创建恢复视图；已确认 good 的 renderer 崩溃只重建当前版本，不再次回退。回滚同时清除空版本指针对应的旧序号。来源：`RendererRecovery.java`、`MainActivity.java`、`RemoteUpdates.java`；JVM 验证：`.omx/artifacts/android-renderer-recovery/verification.json`。

修复 QA 0.0.3 安装最终被系统终止，返回 `INSTALL_FAILED_ABORTED: User rejected permissions`。该次安装没有成功，因此修复后的 throw/hang 不能标为真机通过；必须重新完成本人安装验证后再验收。证据：上述 run 的 `fixed-install-terminal.json`。不要反复触发安装器，不修改生产稳定频道投放故障包。

API2 原生壳、六模式、655 字长草稿和临时 WSS 多主机切换已在真机验证。系统 IME 候选词输入尚未完成实测；合成 composition 事件不能替代此项。证据：`.omx/artifacts/android-api2-live/{initial-state.json,multihost-validation.json}`。
