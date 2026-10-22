# TD-001～TD-006 验证记录

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
