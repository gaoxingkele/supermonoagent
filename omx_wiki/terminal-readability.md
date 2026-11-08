---
title: "Terminal readability and discovery"
type: Decision
category: decision
status: active
created: 2026-10-23
updated: 2026-10-27
tags: [terminal, rendering, discovery, history]
links: ["windows-release-0.2.52.md"]
---

# Terminal readability and discovery

Type: decision
Updated: 2026-10-23

Windows and Android subscribe to the configured relay with an existing Token.
Authorized terminal rooms appear automatically; other Tokens and notification
rooms are excluded. Discovered profiles remain ephemeral. The mobile chooser
refreshes its existing directory connection and preserves drafts and selection.
Sources: `android/assets/mobile/app.js`, `src/supermonoagent/relay.py`,
`deploy/relay/token_discovery_compat.py`; regressions:
`tests/test_android_auto_discovery.py`, `tests/test_workspace_auto_discovery.py`.

Chooser rows and terminal headings use the window title and programming agent,
without a second Shell row. Backend OSC-derived identity has priority; older
hosts can supply a known app marker to the client parser. Unknown remains
`其他`. Sources: `src/supermonoagent/hub.py`, both client documents;
`tests/test_compact_agent_labels.py`, `tests/test_agent_catalog_metadata.py`.

Readable mobile mode expands local row spacing when the actual row surface is
shorter than its viewport. Font size, glyph width, PTY ownership and dimensions
remain unchanged. Measure `.xterm-screen`, not only its outer background.
Source: `tests/test_android_fullscreen_fit.py`; private numeric evidence:
`.omx/artifacts/mobile-short-grid/`.

Codex missing status labels were reproduced as raw-replay loss: the bounded ANSI
ring can discard labels painted once while retaining digit updates. The real
console still contained labels with visible foreground colors. The new explicit
Windows helper restores only that verified footer using read-only console APIs,
separate OP3 after the untouched snapshot and its size metadata. It validates
the session catalog, dimensions, PID creation time and parent. No input or resize
is sent. Sources and boundaries: [terminal-readability](../docs/terminal-readability.md),
`console_snapshot.py`, `tests/test_console_snapshot.py`,
`tests/test_footer_proxy_order.py`. Private live frozen-frontend proof:
`.omx/artifacts/console-snapshot/frozen-windows-footer-browser.json`.

Legacy recovery bindings cover this machine's current consoles. Updated hosts
capture their own WinPty PID generations automatically through both direct and
native relay senders; `tests/test_native_footer_delivery.py` covers this path.
An isolated frozen native 0.2.29 host restored the footer after raw ring cropping,
without changing original replay bytes or requiring manual mappings. This still
does not reconstruct every fixed TUI cell.

2026-10-24 production phone acceptance: APK 0.2.27, signed UI 0.2.31, 14px text,
canonical 158x43, real authorized room initially checked, Token retained on native
settings return, actual legacy refresh request and replay, zero terminal input.
The wrapped Codex status strip contains actual context/weekly rows rather than
prompt text. Portrait/landscape/dialog/IME probes passed for 13 actual screens.
Sources: `SavedConnectionTokens.java`, `ConnectionProbe.java`, `MainActivity.java`,
`tests/test_android_saved_tokens.py`, `tests/test_android_room_probe.py`,
`tests/test_android_selected_rooms.py`, `tests/test_android_refresh_status.py`.
Private source evidence: `.omx/artifacts/android-requested-room-discovery/`.

QA simulated-window confusion was caused by the isolated `.mobile.qa` package
being left in the foreground. It is disabled on this phone. Future QA operators
must reserve the device, verify the exact package, and restore the production app
afterwards. Native updater crash/hang verification has its own pending record;
these successful display checks do not certify that separate scenario.

## 2026-11-08：Cursor 高亮与 TimesJEV

用户指定 `JINLONG3090 / TimesJEV`。Codex 与 Cursor 的共享主题和 ANSI 调色板完全相同；Cursor 行首状态规则漏掉圆点、框线及箭头前缀。候选 `src/supermonoagent/static/agent-modes/cursor-cli.json` 已修正，回归位于 `tests/agent-render-modes.test.cjs`：14 项 Node 与 13 项 Windows/Android/打包检查通过。对比样例只给状态标签补色，保留普通正文、原生 RGB 和暗淡推荐；截图与 JSON 在 `.omx/artifacts/cursor-timesjev-20261108/profile-comparison.*`。

用户随后明确范围为两端所有 Cursor CLI 前端。此配置修改本身即为共享全局模式规则，不绑定 TimesJEV；所有自动识别或手动选择 Cursor CLI 的 Windows/Android 视图在加载新版后都会应用。

手机该终端原先为自动／其他，现已显式保存 Cursor CLI 模式。独立只读中继探针确认后台仅上报 PowerShell，且只提供 26 字节空的 `resize_boundary` 快照；8 秒无实时输出，实际 xterm 解析为 0 个非空字格。未保存论文正文，未发送终端输入或尺寸帧。原始数值证据：`.omx/artifacts/cursor-timesjev-20261108/probe-relay.json`。因此没有把候选样例验证等同于真实 TimesJEV 正文效果，也没有通过覆盖原生 ANSI 来人为增色。

0.2.70 候选签名 UI 包已准备，但服务器 SSH 握手前断开，尚未发布；在线 UI 仍为 0.2.67。手机选择已生效，前缀规则仍需交付新 UI 后才生效。详见 `docs/agent-render-modes.md`。桌面原生检查通道当前报 native pipe 缺失，未控制或重启桌面终端。
