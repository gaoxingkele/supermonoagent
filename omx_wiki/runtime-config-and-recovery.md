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

## 2026-10-26 本机直连与回放队列

用户指定：`127.0.0.1` 本机直连不要求 Token 或房间号；中继仍要求对应认证。源码只对真实 IPv4 回环连接且 Host/Origin 校验通过的请求提供本机所有者身份；显式非空凭据仍按原身份验证，不能把中继会话升级为本机所有者。界面直连忽略过期缓存凭据。来源：`src/supermonoagent/daemon.py`、`src/supermonoagent/frontend.py`、`src/supermonoagent/static/workspace.js`；测试：`tests/test_loopback_daemon_auth.py`、`tests/test_loopback_frontend_auth.py`、`tests/test_workspace_loopback_auth.py`。

原窗口曾显示访问验证失败，但端口健康、当前所有者凭据的独立 HELLO 成功。用户随后提供多页签回放触发 `1013 Slow client output queue overflow` 的复现。不能将所有断连都归因于 Token。Hub 的可靠发送队列会在同步回放突发塞满时断开；改为只保存待回放会话 ID，队列有余量后生成快照，维持原队列上限及真实慢客户端断开逻辑。来源：`src/supermonoagent/hub.py`、`tests/test_replay_backpressure.py`。合成压力测试证明队列边界及顺序，不能单独证明真实六页签在默认队列下的完整触发条件。

为保留运行中的 Codex，前端可在进程内部使用私有所有者凭据连接旧后台，并通过订阅排空屏障分批回放。界面不需要输入或传递该凭据。旧后台源码改动不会自动加载；不能把替换前端称为旧后台已升级。来源：`src/supermonoagent/frontend.py`、`tests/test_frontend_replay_backpressure.py`。

本次用户授权清理后只保留 `s桌面端`、`s手机端` 两个 Codex 会话，关闭另外 6 个终端会话及 2 个空窗口；远程电脑会话未操作。验收证据：`.omx/artifacts/loopback-auth/cleanup.json`。禁止在 wiki 或验证报告中写入真实 Token 或其摘要。

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
# 2026-10-25 剩余项只读复核

当前生产手机为 0.2.34，独立 QA 仍为 0.0.2，修复包 0.0.3 的包名及 SHA-256 已重新核对。设备 Dozing 且锁屏，未发起新安装或重用旧失败会话 81224。后续需要解锁与 OEM 安装验证；真实 OS 输入法及修复后回滚仍未完成。

source_path: `.omx/artifacts/remaining-todo-audit-20261025/phone-readiness.json`

默认 Token 当前可见 3 个在线房间，每个后台有 3 个终端会话，均未声明 file_transfer/assistant_context。新客户端发布不等于这些运行后台已经升级。生产中继仍 host_enrollment=false；本次仅目录认证和 HELLO，未输入、resize、开关终端或重启服务。

source_path: `.omx/artifacts/remaining-todo-audit-20261025/host-readiness.json`, `.omx/artifacts/remaining-todo-audit-20261025/directory-capabilities.json`, `docs/global-workspace-security.md`
