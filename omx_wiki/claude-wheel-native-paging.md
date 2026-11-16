# Claude 窗口滚屏：滚轮归还给智能体自身

2026-11-16。用户报告桌面 Claude 窗口鼠标滚屏后渲染异常、无语法高亮、上/下翻屏失灵。排查证实这不是渲染器缺陷，而是滚屏路由把滚轮吞进了本地纯文本存档。

## 根因链（每步都有实测证据）

1. 用项目自带 `WinPty` 抓真实 Claude Code 2.1.310 字节流（`.omx/artifacts/claude-scroll-repro/claude-tui-capture.bin`）：Claude 跑在 **alternate 屏**（`\x1b[?1049h`），且欢迎屏一开始就开启 SGR 鼠标跟踪（`\x1b[?1000h/1002h/1003h/1006h` 各 2 次），输出 truecolor 高亮。
2. 桌面前端在 capture 阶段 `preventDefault + stopImmediatePropagation` 拦截滚轮（workspace.js wheel 监听），alternate 屏一律走 `pageHistory`。
3. `localHistoryPolicy` 的 native 路由只准 Codex 且仅 button 来源；Claude 落入 archive 路由 = 最多 128 帧的**纯文本屏幕快照**（无 ANSI 颜色）→ 正好解释"没有高亮语法 + 滚不动"。
4. 另一个探针（claude-pageup-probe.bin）：给真实 Claude 发 `\x1b[5~`（PageUp），它立即进入自己的滚动模式并开启鼠标跟踪——说明 Claude 自身完全具备滚屏能力，前端却从未把滚轮交给它。
5. E2E 复现（tests/test_desktop_claude_scroll.py，重放真实抓包字节）：滚轮后 PTY 写入为零（只剩 focus 报告 `\x1b[O`），`historyMode:true` 纯文本层接管。

## 修复（三层，对齐已验证的 Codex 模式）

- terminal-client.js `localHistoryPolicy`：claude/claude-code 的 native 路由扩到 button/wheel/touch 三个来源；**Codex 维持仅 button**（无实证不扩）。
- workspace.js：新增 `pane.appMouseTracking`（CSI `?1000/1002/1003` h/l 处理器跟踪）；滚轮在 alternate 屏且应用已开鼠标跟踪时**直接放行给 xterm**，由 xterm 翻译成 SGR 鼠标事件（`\x1b[<64/65;…M`）直达 Claude——保真滚动 + Claude 原生高亮。
- 未开鼠标跟踪时：滚轮/触摸/按钮 → PageUp/PageDown 键直达 CLI（Claude 收到后自会进入滚动模式）。

结果：Claude 窗口滚轮先转 SGR 鼠标（常态），退鼠标模式后转 Page 键；纯文本存档不再劫持。192 项 Node + 39 项桌面/工作区浏览器回归通过。6773 起了 worktree 源码前端连 6769 后端供真机验证；6772（0.2.79 EXE）未动。

## 移植提示

别的终端宿主接智能体 CLI 时：先抓真实字节确认 buffer 类型和鼠标模式，再决定滚轮归属——alternate 屏 + 应用开鼠标跟踪 = 滚轮属于应用，宿主不得截胡；只有 normal 缓冲区才轮到宿主自己的 scrollback。诊断套路（ConPTY 抓包 → 重放进 e2e 测试 → 断言 PTY 实写字节）可原样搬到任何"终端转发层吞输入"类 bug。

相关：[[conversation-journal]]、[[runtime-config-and-recovery]]
