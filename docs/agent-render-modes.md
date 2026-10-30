# Agent 独立渲染模式

支持“自动”、Codex、Claude Code、Grok、Kimi、pi.dev、Cursor CLI。Windows 在终端底部选择模式；Android 在终端设置中选择。选择按电脑、房间和终端分别保存，切换模式只修改显示，不发送按键、不清空终端、不重启 agent。

自动模式优先使用会话提供的 agent 信息，再参考命令与标题；无法识别时使用基础高亮。嵌套在 PowerShell 内运行的程序可能没有可靠元数据，此时可以手动指定。

## 跨端共享配置

每种模式只有一份配置，Windows 与 Android 使用完全相同的主题、ANSI 调色板和文本高亮规则。Android 的 APK 构建、USB 热更新及签名更新均直接读取以下共享文件，不再维护移动端分叉副本。

| 模式 | 共享配置文件 |
| --- | --- |
| Codex | [codex.json](../src/supermonoagent/static/agent-modes/codex.json) |
| Claude Code | [claude-code.json](../src/supermonoagent/static/agent-modes/claude-code.json) |
| Grok | [grok.json](../src/supermonoagent/static/agent-modes/grok.json) |
| Kimi | [kimi.json](../src/supermonoagent/static/agent-modes/kimi.json) |
| pi.dev | [pi-dev.json](../src/supermonoagent/static/agent-modes/pi-dev.json) |
| Cursor CLI | [cursor-cli.json](../src/supermonoagent/static/agent-modes/cursor-cli.json) |

格式为 `id`、`label`、`theme`、`tokens`。`theme` 控制终端背景、文字及 ANSI 调色板；`tokens` 是文本匹配规则，每条包含 `pattern`、`flags`、`color`。文件通过字段、大小、对比度与规则限制校验后才能应用。字格解析、传输、输入处理和高亮器也由两端共用。

补色作用于未指定前景/背景的文字，不覆盖程序明确给出的 ANSI 前景、背景、反色和隐藏属性；真彩色值保持原样。模式并非完整的编程语言语法解析器，也不替换 agent 的交互协议。

### Grok、Claude Code 与 Cursor CLI 的语义细分

Grok、Claude Code 与 Cursor CLI 在共用主题上进一步区分工具调用、命令和链接：工具调用使用紫色 `#c678dd`，命令和 URL 使用青色 `#56b6c2`；Agent 与思考/搜索状态沿用蓝色，路径、字符串、数字和行内代码沿用黄色。警告使用黄色，错误使用红色，成功使用绿色。工具规则先于通用函数规则，避免 `Read(...)` 被当成普通函数着色；普通正文不整行补色。

这些配置只修饰默认前景字格，不改变终端字节、原生 ANSI/真彩色、暗淡提示或输入区。Grok 额外覆盖搜索、引用及常见读写/执行工具，Claude Code 覆盖 Read、Write、Edit、MultiEdit、Bash、Task、Glob、Grep、TodoWrite、WebSearch 等工具，Cursor CLI 覆盖 Cursor、Agent、Composer、Review、Apply、Diff 等状态与同类工具。

## 统一修改与调试

1. 编辑上表中的单个 JSON，在 Windows 和 Android 选择同一模式验证。
2. 开发调试时重新加载界面以清除页面内配置缓存。正式资源按 revision 固定，不能就地覆盖已发布的包。
3. 独立回归：Windows `tests/test_windows_agent_modes.py`，Android `tests/test_android_agent_modes.py`；共享规则与释放行为为 `tests/agent-render-modes.test.cjs`。
4. 使用既有签名工具打包发布。桌面完整包为 17 个文件，Android 为 18 个文件，两端打包的六份 JSON 字节一致。

模板资源需要 Native API 2，首次必须安装支持它的 Windows 外壳或 Android APK；旧 API 1 不接受缺少资源的新包。之后在相同 API 内修改模板可继续热更新。旧缓存和旧安装包保留，升级不终止后台终端。

验证截图与字格记录：`.omx/artifacts/windows-agent-modes/`、`.omx/artifacts/android-agent-modes/`。固定白名单与签名验证：`.omx/artifacts/agent-mode-api2/`。

## 0.2.56 高亮验收

共享规则 Node 回归 12/12 通过；Windows/Android profile、真实 DOM/WebGL、原生 ANSI 和打包回归 14 项通过。Android 与 Windows 包中的 Grok、Claude Code JSON 均与源文件逐字节一致，APK v2/v3 签名和 Windows EXE 的隔离 WebView2 启动检查通过。

本机“审稿”已显式选择 Grok，EWAN 主机下的“FULU”已显式选择 Claude Code；本机同名 FULU 未被用于远程模式设置。Windows 0.2.56 显示端的资源 revision 为 `bdc822bc33ea94af`，两份 profile 均通过该显示端实际读取并核对源文件一致；后续 0.2.57 沿用相同 profile。6769 后台仍由原 PID 23440 持有，未向目标终端发送输入或重启 CLI，渲染器恢复为 DOM。

隔离样例截图 `windows-agent-modes/source-grok.png` 与 `source-claude-code.png` 展示了紫色工具、青色命令/链接和独立警告色。解锁后已采集实际窗口 `refresh-diagnosis/windows-056-review.png` 与 `windows-056-fulu.png`，选择器分别确认 Grok 和 Claude Code，未见叠字或裁切；完整色板覆盖以隔离样例为据，不把当前窗口未出现的工具类型写成已目测验证。
