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

Scope: footer bindings currently cover this machine's running legacy consoles.
Other hosts need corresponding host support; this does not reconstruct every
fixed TUI cell. Real native GUI verification requires an unlocked device.
