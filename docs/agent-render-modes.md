# Agent 独立渲染模式

支持“自动”、Codex、Claude Code、Grok、Kimi、pi.dev、Cursor CLI。Windows 在终端底部选择模式；Android 在终端设置中选择。选择按电脑、房间和终端分别保存，切换模式只修改显示，不发送按键、不清空终端、不重启 agent。

自动模式优先使用会话提供的 agent 信息，再参考命令与标题；无法识别时使用基础高亮。嵌套在 PowerShell 内运行的程序可能没有可靠元数据，此时可以手动指定。

## 独立配置文件

每种模式有两份独立配置。修改某一端、某一模式只需改对应文件，另一端及其他模式不必同步修改。

| 模式 | Windows | Android |
| --- | --- | --- |
| Codex | [codex.json](../src/supermonoagent/static/agent-modes/codex.json) | [codex.json](../android/assets/mobile/agent-modes/codex.json) |
| Claude Code | [claude-code.json](../src/supermonoagent/static/agent-modes/claude-code.json) | [claude-code.json](../android/assets/mobile/agent-modes/claude-code.json) |
| Grok | [grok.json](../src/supermonoagent/static/agent-modes/grok.json) | [grok.json](../android/assets/mobile/agent-modes/grok.json) |
| Kimi | [kimi.json](../src/supermonoagent/static/agent-modes/kimi.json) | [kimi.json](../android/assets/mobile/agent-modes/kimi.json) |
| pi.dev | [pi-dev.json](../src/supermonoagent/static/agent-modes/pi-dev.json) | [pi-dev.json](../android/assets/mobile/agent-modes/pi-dev.json) |
| Cursor CLI | [cursor-cli.json](../src/supermonoagent/static/agent-modes/cursor-cli.json) | [cursor-cli.json](../android/assets/mobile/agent-modes/cursor-cli.json) |

格式为 `id`、`label`、`theme`、`tokens`。`theme` 控制终端背景、文字及 ANSI 调色板；`tokens` 是独立的文本匹配规则，每条包含 `pattern`、`flags`、`color`。文件通过字段、大小、对比度与规则限制校验后才能应用。字格解析、传输和输入处理仍共用，避免维护十二份终端引擎。

补色作用于未指定前景/背景的文字，不覆盖程序明确给出的 ANSI 前景、背景、反色和隐藏属性；真彩色值保持原样。模式并非完整的编程语言语法解析器，也不替换 agent 的交互协议。

## 分别修改与调试

1. 编辑上表中的单个 JSON，在对应端选择该模式。
2. 开发调试时重新加载界面以清除页面内配置缓存。正式资源按 revision 固定，不能就地覆盖已发布的包。
3. 独立回归：Windows `tests/test_windows_agent_modes.py`，Android `tests/test_android_agent_modes.py`；共享规则与释放行为为 `tests/agent-render-modes.test.cjs`。
4. 使用既有签名工具打包发布。桌面完整包为 17 个文件，Android 为 16 个文件，各带本端六份 JSON。

模板资源需要 Native API 2，首次必须安装支持它的 Windows 外壳或 Android APK；旧 API 1 不接受缺少资源的新包。之后在相同 API 内修改模板可继续热更新。旧缓存和旧安装包保留，升级不终止后台终端。

验证截图与字格记录：`.omx/artifacts/windows-agent-modes/`、`.omx/artifacts/android-agent-modes/`。固定白名单与签名验证：`.omx/artifacts/agent-mode-api2/`。
