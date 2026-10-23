# TD-001～TD-006 验证记录

## 当前完成审计摘要（2026-10-24）

本页保留各轮证据，不能把旧构建目录、旧测试总数或源码通过合并为“所有 TODO 已完成”。完整目标仍有修复后 Android 原生故障回退的真机门槛；本轮系统输入法也未实测。独立房间所有权的严格生产迁移尚未启用，不能把 TLS 或 Token 目录已上线当作该项完成。两项集成审查发现的连接回归已修复并通过定向验证。当前总览见 [TODO](TODO.md)。

| 当前事项 | 已证明的范围 | 证据 / 边界 |
| --- | --- | --- |
| 最新授权要求 | 用户明确选择 Token＋房间号，不再要求交付统一邮箱登录 | 统一账号高级源码保留；生产账号创建、grants、邮箱绑定不再是本轮门槛。此为用户范围调整，不是把 Token 目录当作统一账号已部署。 |
| 默认 Token 与房间范围 | 已找到 `dist/windows/0.2.24/token.txt`；私有路径配置不含凭据正文；6769 真实后台与 Windows 0.2.28 打包 UI 已做鉴权和目标范围验证 | `.omx/artifacts/token-room-provision/runtime-source.json`、`live-owner-wire.json`、`packaged-default-ui.json`：正确默认值与已认证 wire 一致，外部目标清空预填，测试期间输入/resize 发送 0、不额外写入浏览器长期存储或缓存视图。 |
| 底部翻屏 | 重连 generation、CSI 3J、本地历史与 Codex 备用屏四键门控均有针对性验证 | `tests/test_workspace_paging.py`、`tests/test_codex_owned_paging.py`；`.omx/artifacts/codex-owned-paging/real-codex.json` 为真实隔离 Codex 0.164.0，不复制用户认证，不发模型请求或用户 PTY 输入。不能给未确认的其他全屏程序通用发送这些键。 |
| 线上加密与更新 | HTTPS/WSS、公信 IP 证书、签名更新发布及回读验签已完成；自动续期 timer 已实测 | `.omx/artifacts/wss-migration/public-tls.json`、`.omx/artifacts/ui-menu-profiles/publication-verified.json`；`.omx/artifacts/goal-completion-audit/cert-renew-timer.json` 记录 timer active/enabled、service success/exit0。旧“缺 SSH/TLS/首次公网发布”文字仅是历史，不是当前阻塞。 |
| Android API2 实机 | 0.2.24（code26）保留数据安装，六种模式、655 字中英长草稿、临时 WSS 多主机和切换后草稿保留已有真实设备证据 | `.omx/artifacts/android-api2-live/initial-state.json`、`multihost-validation.json`、`restored-after-probe.json`：真实主机保留、无模型请求、FakePTY 输入0，原选择/空草稿/sender 恢复，临时房间清理。composition 事件是合成事件，真实 OS IME 仍未验证。 |
| Android 集成回归修复 | 网络直连需真实 HELLO 鉴权回执；字段改变使回执失效。自动预填或解密失败不触发密文保存/删除，显式用户编辑/取消保存才写入 | 41 项定向测试及全部 Java 编译通过；真实 TLS 非 loopback DNS fixture 验证直连、错 Token 和 relay 认证失败，JVM 验证保存策略及 AES/GCM。证据：`.omx/artifacts/android-credential-direct-fixes/verification.json`。 |
| Android 回退的实际失败与后续修复 | 旧 QA 0.0.2 的 good 更新和状态保存通过；throw2 的最终 active 却变为 bundled，不能算恢复 good2 成功。已排除 good2 文件或签名损坏，Native 生命周期/退休及状态字段修复已完成源码回归 | 私有 QA 目录 `.omx/artifacts/android-ui-rollback-qa/qa-20261023T153711Z-00fddc0833a0/` 的 `physical-verification.json`、`cached-good2-integrity.json`、`cached-good2-java-verification.txt`、`native-fixed-source.json`。修复 QA 0.0.3 的 81224 安装会话终态拒绝，见 `fixed-install-terminal.json`；修复后的 throw/hang 真机验收仍未执行，保持未完成。 |

### 版本与验证范围

Root 的修复后扩大复验为 **46 passed、8 subtests passed**，包括既有 `test_android_probe.py`；原始输出 `.omx/artifacts/goal-completion-audit/post-fix.{xml,txt}`。0.2.30 Windows 的 46 个源文件哈希全部匹配，Android 包含 renderer 恢复修复且公开默认凭据为空，见 `build-0.2.30-audit.json`。

- 0.2.24 是本轮有明确记录的 Android 实机基础验收版本，0.2.28 是默认 Token 的 Windows 打包 UI 定向证据。后续观察到外部安装的 Android 0.2.27；外部 0.2.29（code31）构建和最新 `dist/README.md` 只能说明产物存在，不能代替本轮真机或正式完整验收。
- 修复前整套基线为 499 passed、1 skipped、57 subtests，耗时 505.96 秒；证据 `.omx/artifacts/goal-completion-audit/full-suite.{xml,txt}`，Node 42 passed。随后两项 Android 集成问题修复后的 41 项定向测试通过，0.2.30 双端构建成功，仍不代替物理故障恢复验收。下面 129/235 等更旧轮次数值属于各自历史，不跨轮合计。
- QA 仅使用独立 package `dev.supermonoagent.mobile.qa`、本地 FakePTY 和独立 HTTPS QA 频道；未修改生产 app 私有数据、全局 IME 或剪贴板。QA 公共文件已按已知 hash 退役，恢复生产前台并清理自建映射；未把测试失败或安装拒绝写成通过。

## 历史验证快照：2026-10-22

以下保留当时原始报告。旧版本、旧安装会话 56125、当时的安装阻塞与线上部署边界不代表今天仍存在；当前状态以文首摘要为准。

2026-10-22。本轮目标已按用户确认扩展到 TD-006。源码实现、最终 Windows 原生缩放回归和两个平台构建已完成；Android 真机覆盖安装被手机系统拒绝，等待用户允许 USB 安装后继续。本页不把构建通过等同于手机已升级。

| 事项 | 实现及证据 |
| --- | --- |
| TD-001 通信状态 | `terminal-client.js::ConnectionMeter` 统一记录消息字节，按实际时间采样；顶部显示收发 B/s、RTT/心跳/重连决定的质量格；空闲为零。`connection-size.test.cjs`、`test_workspace_runtime.py` 覆盖状态和窗口宽度。 |
| TD-002 换行与宽度 | 以后台确认的网格渲染，区分请求与确认；新后台范围 2×1～500×200，旧后台按确认网格缩放共享视图。真实 PowerShell/Codex 中英文未提交草稿、窄分屏、IME 与 ACK 丢失回归。 |
| TD-003 顶部入口 | 去除新版“经典版”链接、专属样式及错误提示中的入口指引；经典路由保留；浏览器默认打开新版 workspace。 |
| TD-004 Windows 粘贴 | `pty.py::ConptyInputFilter` 流式识别跨包粘贴边界、增量 UTF-8；完整粘贴单次写入，内部 CR/CRLF 规范成 LF，普通 Enter 不变。旧本地后台通过 frontend 兼容层适配。 |
| TD-005 全量重绘 | 有尺寸租约时用受限列数变化再恢复，逐次等待确认；限频、输入/IME 延迟、超时恢复；不因普通聚焦或窗口变化抢共享租约。旧协议用 PING/PONG 屏障区分 resize 前后的状态。 |
| TD-006 多电脑 | 桌面 per-host socket/目录/控制/速率/重连，JSON 元组区分同会话 ID；Android 并行目录、单活动终端输出、后台关闭连接；已知房间鉴权推送，独立房间 owner 登记，TLS/Host/Origin/Token 撤销。 |

## 源码落点与简化

- 客户端：`workspace.py`、`static/workspace.js`、`static/workspace.css`、`static/terminal-client.js`、`desktop.py`、Android mobile 资源。
- 后台：`pty.py`、`hub.py`、`daemon.py`、`frontend.py`、`relay.py`、`protocol.py`、新增 `security.py`。
- 构建：`scripts/build-windows.ps1`、`scripts/build-android.ps1`。没有增加运行时依赖。
- 复用共享尺寸约束和输入过滤器，统一消息计量；移除不安全的 health 凭证/目录发布与 WS URL 查询凭证，避免重复轮询。关闭视图保留后台 PTY，显式终止动作另行处理。

## 已完成的验证

最终整套测试：129 项 Python、21 项子测试通过（91.11 秒）；26 项 Node 测试通过。另 1 项 Windows 符号链接测试因本账户不能创建链接而跳过，未作为通过计算。

真实 CLI 使用隔离临时目录和测试拥有的进程，未向用户现有会话发送提示词或命令。证据在 `.omx/artifacts/todo-completion/`：

- `powershell-summary.json` 与 40/80 列原始 trace，`cli-render-proof.json`：长 ASCII/中文草稿及光标位置匹配。
- `kimi-summary.json`、`dist/kimi-paste-evidence.bin`、`cli-render-proof.json`：多行/中文/96 ASCII 粘贴仍在输入编辑区，未提交。
- `codex-real-summary.json`、`codex-render-proof.json`、`codex-paste-proof.json`：diff-only 回放无法恢复草稿，受控 40→39→40 重绘可恢复完整画面与草稿。
- 桌面与 Android 双隔离 daemon、同 relay、相同 session ID 测试：定向输入/输出、单主机验证失败和掉线隔离；延迟事件、编辑目标后的状态清理。
- `test_security_directory.py`：房间枚举隐私、所有权持久化、错误 Origin/Host、Token 撤销和队列清理、TLS 默认拒绝不受信证书与显式受信上下文成功。没有安装系统证书或禁用证书验证。
- `webview2-source-qa.json` 与 `webview2-native-*.png`：实际 WebView2，普通/最大化/窄窗口 × 100%/125%/150% 共 9 组通过；窗口系统 DPI 200%，有效 DPR 2/2.5/3；缩放后跟底，点击/底部向下滚动不丢跟随，上滚历史位置保留。隔离 FakePTY 输入字节为零。早期 CDP 截图在高缩放下裁切，只保留作诊断；最终视觉证据为完整原生窗口截图。
- `packaged-smoke.json`：实际最终 EXE 的 27 个源码哈希匹配，隔离后台启动、health 无凭证、390/850/1440/1920 宽度的确认网格与 fit 一致，页面没有横向溢出。
- `scripts/benchmark-global-workspace.py` 与 `.omx/artifacts/global-workspace/resources.json`：1/4/16 主机，未订阅终端输出为零；应用心跳约 2.67/10.67/42.67 B/s。内存/功耗边界见 [安全与身份设计](global-workspace-security.md)。

## 交付与限制

Windows 新包目录 `dist/windows-global-workspace/`，EXE SHA256 `3c9dcd811e4150a0f5684b8cbfc43c4c61e9ea88c9801866d8ceceaf485b3a96`；Android 新包 `dist/android-global-workspace/SuperMonoAgent.apk`，版本 0.2.13(15)，SHA256 `f1f9afd671ddd33e1f6cd066ddbef92a2d63ab081411956f4f9de72597ed32f6`。Android 12 个资源哈希匹配、generic 配置为空、v2/v3 签名通过。源码快照为 `dist/global-workspace-source.zip`，避免其他并发会话后续修改导致交付依据不清。

最终哈希与包内资源一致性以各目录 `build-manifest.json`、`verification.json` 及本轮最终验证结果为准，不使用旧试用包作为本轮交付。Windows 打包器对可选模块及 Windows API-set DLL 的提示未造成启动失败，最终 EXE 的隔离真实 PowerShell 创建、页面与尺寸检查通过；EXE 未进行发行签名。

Android 已获用户覆盖安装并保留数据的授权。第一次安装被手机系统拒绝（`INSTALL_FAILED_ABORTED: User rejected permissions`）。后续检查确认手机亮屏未锁定，已重试并完成本次普通安装确认；荣耀身份验证弹窗要求本人指纹或系统密码，`adb install -r` session 56125 仍在等待。手机仍为 0.2.4，不宣称真机已升级。继续时先检查此安装进程及设备版本，不因观察超时而重复发起安装。

现有本机 daemon/Kimi/Codex 与远程生产 relay 未重启；线上仍可能运行旧协议版本。新中继所有权登记与 TLS 迁移见 [部署步骤](global-workspace-security.md)。统一跨主机账户/短期凭证是 TD-006G 的设计交付，尚未上线。长时间手机电池消耗、公网弱网续航没有测量；资源评估不冒充电池实测。

环境未安装 AGENTS 中提及的 `visual-verdict` 技能（用户技能目录和指定 mylib 均未找到）。本轮使用可复现的真实 WebView2 几何/缓冲断言与原生窗口截图人工检查作为视觉验收，不声明执行了该缺失技能。项目没有额外配置的独立 lint/typecheck 命令；执行了 Python 编译、JavaScript 语法、完整测试和 diff 空白检查。
