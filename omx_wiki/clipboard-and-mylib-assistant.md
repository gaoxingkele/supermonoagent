---
type: decision
updated: 2026-10-27
links: ["windows-release-0.2.52.md"]
---

# 文件粘贴与 mylib 助理的隔离边界

最新发布与后续接手边界见 [Windows 0.2.52 发布交接](windows-release-0.2.52.md)。本页保留各阶段的原始验收记录。

## 右键菜单与异步剪贴板

xterm 的隐藏 textarea 属于终端输入路径，不能按普通文本框处理。普通文本框的异步粘贴必须重新验证窗口、连接代次、原文本、选区和只读状态，避免用户移动光标后贴到旧位置。菜单放在当前 modal dialog 内，Esc 仅关闭菜单并返回输入焦点；复制选中的终端内容不能向 PTY 发送 Ctrl+C。

source_path: `src/supermonoagent/static/clipboard-context-menu.js`, `tests/test_clipboard_context_menu.py`, `tests/test_six_cli_clipboard_keys.py`

本次七项菜单／六种模式测试共 13 passed，使用自有浏览器与 PTY、模拟原生剪贴板。它证明菜单路由与生命周期保护，不证明已发布或六个真实 CLI 的系统剪贴板验收通过。

source_path: `.omx/artifacts/six-cli/clipboard-context-menu.xml`, `docs/TODO.md`

终端稳定性优先。文件和助理请求不能进入 PTY 输入，也不能使同连接的输入、心跳等待慢文件系统。实现用每连接有界侧栏任务队列及独立执行器，保留既有终端处理路径。

source_path: `src/supermonoagent/daemon.py`, `tests/test_file_transfer_responsiveness.py`

## 文件接收

OP21 绑定连接身份与原始 session，每步检查所有者、频道和会话；异步后再次核查，发布前再检查权限。文件只写本地预设目录，分块校验长度和 SHA-256 后原子发布；重名编号、不覆盖。未完成文件按断线、取消或空闲超时回收。发布后的暂存清理失败不能把成功报告成失败。

source_path: `src/supermonoagent/file_transfer.py`, `src/supermonoagent/file_transfer_rpc.py`, `tests/test_daemon_file_transfer.py`, `docs/clipboard-files.md`

原生桥接只接收随机源 ID，不接受网页传入本地文件路径。源句柄只来自显式粘贴的剪贴板快照。普通文字维持 xterm 原处理路径；过期格式提示需重新辨别。浏览器文件逐个准备及释放，不能将一整批文件全部载入 JS 内存。关闭或重载回调不能等待文件散列锁。

source_path: `src/supermonoagent/desktop_files.py`, `src/supermonoagent/windows_clipboard.py`, `src/supermonoagent/static/clipboard-files.js`, `tests/test_clipboard_file_ui.py`

## 助理与证据

维护库已快进同步到 mylib `78c7e3b`，原 `Graphic/` 未提交目录保留。技能来源按维护清单解析，不能把备份目录和全部上游快照当作活跃技能；来源路径必须留在库内。

source_path: `D:/aicoding/mylib/README.md`, `D:/aicoding/mylib/WIKI.md`, `D:/aicoding/mylib/skill-runtime/manifest.json`, `src/supermonoagent/mylib_assistant.py`

OP22 只读目标终端启动目录内的固定 metaRSI wiki 页面及目标技能清单。启动目录不代表 shell 后续 cd 的目录。终端文字不选择文件路径，不输出自行推断的账本模式，不初始化账本或批准受保护变更。故障输出只构成待验证建议。

source_path: `src/supermonoagent/assistant_context.py`, `D:/aicoding/mylib/skills/meta-rsi/SKILL.md`, `tests/test_daemon_assistant_context.py`

## 验收边界

2026-10-27 的实际桌面检查补充：安装目录的 EXE 为 0.2.48，编辑框真实 Ctrl+C／Ctrl+V 可用，选中文字后未出现右键菜单。隔离的 0.2.51 WebView2（显式 `--url`，未启用原生桥接）已实测右键菜单、复制及授权后粘贴，快捷键复制粘贴也通过；假 PTY 写入数为 0。首次浏览器剪贴板权限确认后，最初一次读取仍失败，需要再次粘贴，具体原因未证实，不能称为原生桥接通过。

用户随后按物理 Escape 停止桌面控制。两个自有验证窗口均已清理，原生桥接模式尚未交互验收；不因自动目标继续而恢复桌面控制。上述证明不涵盖真实文件／图片剪贴板，也不涵盖六种 CLI 的完整输入与光标验收。

source_path: `.omx/artifacts/six-cli/clipboard-cua-20261027.json`, `.omx/artifacts/six-cli/live-native-51-cleanup.json`, `.omx/artifacts/six-cli/live-native-bridge-51-cleanup.json`, `.omx/artifacts/six-cli/release-feature-audit-summary.md`

真实隐藏 WebView2、pywebview 原生桥接和测试文件源已完成 192000 字节上传及 SHA-256 核对，假 PTY 输入为 0，测试子进程无残留。该证据不能替代真实系统剪贴板读取：当前环境创建私有窗口站、只读打开剪贴板均被系统拒绝，没有覆盖用户剪贴板。

source_path: `.omx/artifacts/file-paste/native-webview-evidence.json`, `.omx/artifacts/file-paste/native-clipboard-evidence.json`, `.omx/artifacts/file-paste/clipboard-metadata-preflight.json`

实际用户提供的 `D:/BaiduSyncdisk/temp/SuperMonoAgent-0.2.36-Setup` 已验证可安装到隔离目录、默认凭据匹配，未执行普通用户目录安装、注册或启动。启动器原有主窗口分支静默返回；后续源码增加激活尝试与成功说明，不强开第二个窗口。源码修复不等于原下载目录已经更新。

source_path: `.omx/artifacts/actual-setup-20261025/verification.json`, `scripts/windows-installer/Launch.ps1`, `scripts/windows-installer/Install.cmd`, `tests/test_windows_installer.py`

## 暂停目标菜单的后续定位

真实 Codex 0.164.0 在 auto/always 中首次绘制就将目标恢复菜单放在中间，同尺寸 OutputPump 回放的可见文本及坐标一致；never 模式也未达到干净的输入栏上方布局。不能依据合成裁切实验去认定这个原生菜单是前端错误。用户所需位置仍待原生布局或独立显示适配，产品终端渲染未修改。

source_path: `docs/codex-goal-menu.md`, `tests/test_codex_goal_popup.py`, `.omx/artifacts/goal-popup/aggregate-evidence.json`
