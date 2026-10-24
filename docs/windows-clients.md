# Three Windows clients, one daemon

## Simple relay connection

The primary connection form asks for server IP/address, port (HTTPS defaults to 443), and Token. “记住此服务器的 Token” is selected by default; turn it off for session-only storage. Credentials are autofilled only for their saved endpoint or the relay configured by the authenticated local daemon. Editing the server clears the autofilled credential and invalidates any pending test.

Click “测试连接” to retrieve all authorized online rooms, selected by default, then “连接所选房间”. The room list continues updating; newly authorized rooms are included automatically. Uncheck an unwanted room to detach its client views while its backend terminals keep running. Manual room filtering is under advanced settings. An empty directory can be saved to wait for rooms to come online.

From the executable directory, start Windows with only the relay and Token options:

```powershell
.\SuperMonoAgent.exe --relay wss://117.50.48.22/ws --token "YOUR_TOKEN"
```

No mode, bind address, port or room argument is required: the default workspace uses the local daemon at `127.0.0.1:6769`. A new daemon configuration registers the computer name as its room. Existing saved room numbers and running backends retain their configuration; startup options configure a newly started daemon. An explicit `--code` remains available. `--token-file` is also supported as an alternative to `--token`.

## Remote terminal colors

The modern workspace now shares Android's display-only text highlighter, enabled by default. The footer's “文本高亮” button toggles it and saves the local preference. Files, numbers, quoted strings, inline code, function names and error labels can receive color when the application supplied none. This is auxiliary text highlighting, not a complete programming-language parser. It never rewrites terminal bytes or replaces explicit ANSI foreground/background colors. Closing a view releases its decorations without stopping the terminal.

The 2026-10-23 read-only JINLONG3090 sample contained reset/bold/dim/underline SGR parameters, with no foreground/background colors in its three bounded histories or the two active streams observed. No input or resize frames were sent. Actual relay regressions preserve 16-color, 256-color and truecolor output on both desktop and Android in DOM and WebGL; color loss is not inherent to the relay. A separate backend correction preserves SGR state when old history is cropped, advertised by `snapshot_sgr:true`. That correction requires updating the computer hosting the daemon; existing live terminals are not restarted automatically.

The desktop and Android UI fix was published as **0.2.19** to the existing HTTPS update channel and downloaded again for signature and archive verification. Use “检查更新” and install the update on each client. This publication does not force-reload any open view or restart a remote daemon. Evidence: `.omx/artifacts/remote-terminal-colors/publication-verified.json`.

The current Windows release is **0.2.33**, matching Android, in `dist/windows/0.2.33/`. Windows releases use the same numeric version as Android, never feature-based names. The build reads Android's manifest version and embeds it in EXE FileVersion/ProductVersion, `version.txt` and `build-manifest.json`; the default output is `dist/windows/<version>/`. When building both platforms, build Android first because its existing build advances the release version, then build Windows. Numeric output folders must match the build version, and each platform's build holds an exclusive project lock. Older named folders below are historical artifacts.

The top-left “菜单” sits between the sidebar toggle and supermono, with “检查更新”, “下载并更新”, “刷新中继” and “连接工作区”. “刷新中继” requests a fresh authorized room directory from every configured relay and retrieves each computer's current window catalog. Live terminal sockets and views stay open; disconnected directory connections can retry. It lists only rooms accessible to the configured credentials. Repeated clicks are throttled. The connection dialog accepts address/IP, port and Token; multiple credential profiles discover their authorized rooms automatically. Tokens stay in session storage unless “在此设备保存 Token” is selected, and are bound to their endpoint. Advanced room/account settings remain collapsed.

Every machine has a room menu with “新增窗口”, “刷新所有窗口”, and “关闭所有窗口”. Refresh redraws/subscribes only to that room's already open views without input or resize. Closing requires confirmation that the captured windows and their processes will terminate; windows added after confirmation begins and other rooms are not closed. Six independent [Agent render modes](agent-render-modes.md) are available in the footer and Android settings.

New native UI generations use separate cache namespaces, inheriting only the sequence protection. Old hot-update pointers cannot silently hide the new built-in UI. Existing windows and old cache files remain untouched; bundled asset URLs include the bundle hash to prevent stale browser caching.

The sidebar lists each machine once with its room code and all open windows directly underneath, without a search box. Use the arrow beside a machine to collapse or expand its window list; the state survives page reloads in the same browser session. Collapsing affects only the sidebar and keeps its terminals running. Project and terminal-group headings and the create-group button are removed. Existing windows from every backend group remain listed; status dots, colors, rename and per-machine connection settings remain available. For direct connections without a supplied room code, it displays that no room is configured.

This release includes the unified-account and sidebar features below, signed UI-update checks and the same icon as Android in the executable, window and taskbar. Run `create-desktop-shortcuts.vbs` in the extracted package to create matching desktop shortcuts; keep the package together and rerun the helper if you move it. See [UI updates and deployment limits](ui-hot-updates.md).

The unified-account build is `dist/windows-relay-accounts/`. It includes selectable/manual default relay addresses, relay account login and authorized-computer discovery with automatic endpoint/room/session filling. Public login requires the new HTTPS/WSS relay and explicit room grants/local host bindings; the old public service has not been automatically migrated. See [setup and current deployment state](relay-account-setup.md).

The latest sidebar-lamp build is `dist/windows-sidebar-status/`, including the global workspace features below. Its window rows use red/yellow/pulsing green status dots and deeper group indentation. See [status meanings and verification](sidebar-activity.md).

## Automatic same-token discovery

The sidebar subscribes to the configured relay over WSS using the current token.
New online terminal rooms accepting that token appear automatically, including
their current windows. Other tokens and notification-service rooms remain hidden.
Host and window removal, identity changes and token revocation update the list.
Discovery does not open a terminal, send input, take resize control or change the
current layout. Discovered credentials remain in memory; manual connections stay
available. Automatic discovery is limited to the same explicitly configured relay.

The local authenticated daemon advertises its configured relay targets. Older
frozen daemons without room codes use token-only discovery; matching authenticated
machine identity and complete terminal inventories prevent a duplicate local
computer. A matching machine name alone never causes deduplication. Changing the
local relay configuration updates subscriptions immediately. Servers without the
new capability keep existing manual connections working.

The current public relay uses the documented legacy compatibility adapter in
[deploy/relay/token-discovery-compat.md](../deploy/relay/token-discovery-compat.md).
It retains its previous host enrollment limitations; this feature does not upgrade
that legacy relay to account-based host ownership. Public WSS tests cover new room
push, token isolation, notification exclusion, identity updates and revocation.

## Current global workspace build

Use `dist/windows-global-workspace/` for TD-001–TD-006. It adds simultaneous per-computer connections, tuple-scoped terminal views and authenticated known-room discovery. Each computer retains its own credentials, connection state and resize lease; switching computer selection leaves other connections alive. See [verification and remaining installation steps](todo-completion.md) and [TLS/enrollment migration](global-workspace-security.md). The older directories below record earlier trial builds.

## Earlier TODO completion build

`dist/windows-todo-complete/` contains the TD-001–TD-005 update. `01-Browser.vbs` now opens the same workspace as `02-Orca-Workspace.vbs` (the browser mode uses `--workspace`). The top right shows current connection, application-message bytes/sec and RTT-derived quality; its former legacy link is removed. An explicit `--legacy` option still supports existing routes.

Terminal rendering follows the daemon's confirmed grid and its resize lease. Shared viewers scale that grid to their area without stealing control on ordinary focus or resize. Explicit takeover/refresh can trigger one ACK-sequenced column change and restoration, delayed during input/composition and rate-limited. New daemons support 2×1–500×200 cells; older daemon limits are honored by fitting the confirmed grid.

Windows ConPTY paste handling preserves UTF-8 and split paste envelopes, writes completed paste atomically, and converts CR/CRLF inside paste to editor LF so short multiline Kimi input isn't submitted. Ordinary Enter and other control sequences stay unchanged. The loopback frontend applies the same adapter for existing older local daemons, without restarting their shells; directly connected older remote daemons require their backend update.

## Start without an empty console

The updated build is in `dist/windows-hidden/`. Double-click `02-Orca-Workspace.vbs` for the desktop or `01-Browser.vbs` for the browser. These launch without an extra console; their CMD equivalents hand off and exit. Run the EXE directly when console diagnostics are needed. The interactive Microsoft/native terminal still uses a console intentionally.

Background daemon/frontend startup uses `CREATE_NO_WINDOW` without the conflicting `DETACHED_PROCESS` flag. Hiding a display window does not terminate its service or shell. Existing running daemons are not restarted by this change. Build this separate output with `scripts/build-windows.ps1 -OutputDir dist/windows-hidden`.

The three trial entry points use the same Python daemon and its existing relay protocol. Closing a browser, workspace window or native attachment leaves daemon-owned shells running. Microsoft Intelligent Terminal supplies native terminal display and input; it does not replace the daemon or the phone relay.

Local browser and workspace launchers now attach to the daemon on port 6769 by default and open tabs for all its existing sessions. A loopback frontend serves the current UI and forwards the existing protocol; it owns no shells. This lets a newly built client display an already-running older daemon without restarting its Kimi/Codex processes. Matching frontend services are reused. Explicit `--port` and remote `--url` choices remain respected.

The earlier `windows-tabs` trial scripts pinned an isolated daemon on 6789, which showed an empty workspace. The corrected `windows-linked` package uses the shared default instead. With older daemons, label colors are retained locally by the frontend; titles and terminal contents still use the existing daemon. Full color synchronization through an older remote relay requires an updated daemon. A frontend/client disconnect never sends a session-close command.

## Run the packaged clients

Keep the entire `dist/windows` folder together. Double-click:

1. `01-Browser.cmd`: shared workspace in the browser and an automatically started local daemon only when needed.
2. `02-Orca-Workspace.cmd`: Orca-style tabs, session management and split terminals in a Windows WebView2 window.
3. `03-Intelligent-Terminal.cmd`: opens Microsoft Intelligent Terminal with a native attachment to a daemon-owned shell. The external Microsoft application must be installed and its `wtai.exe` app execution alias available; it is not bundled.

The `SuperMonoAgent` subfolder contains one shared executable and runtime payload. These entry points deliberately share the payload instead of distributing three copies. The packaged clients do not require a separate Python installation. WebView2 Runtime is a system prerequisite for the second client; the Microsoft client requires its separately installed Microsoft application. The trial executable is unsigned and is not a production installer.

```powershell
# Keep a console host visible when diagnosing startup:
.\SuperMonoAgent\SuperMonoAgent.exe host --host 127.0.0.1 --port 6769
# Workspace renderer comparison:
.\SuperMonoAgent\SuperMonoAgent.exe workspace --renderer dom
.\SuperMonoAgent\SuperMonoAgent.exe workspace --renderer webgl
# Original page in the desktop shell:
.\SuperMonoAgent\SuperMonoAgent.exe workspace --legacy
# Validate the real WebView2 runtime without displaying a window:
.\SuperMonoAgent\SuperMonoAgent.exe workspace --smoke-test
# Remote daemon/relay; enter credentials in its workspace connection dialog:
.\SuperMonoAgent\SuperMonoAgent.exe workspace --url https://your-relay.example
# Discovery only; no Microsoft window opens:
.\SuperMonoAgent\SuperMonoAgent.exe microsoft --diagnose
# List shared sessions, then attach to one:
.\SuperMonoAgent\SuperMonoAgent.exe microsoft --list
.\SuperMonoAgent\SuperMonoAgent.exe microsoft --session SESSION_ID --take-control
```

Default local startup binds the daemon to `127.0.0.1:6769`. Browser and workspace launchers reuse it and retrieve its token only through the loopback health endpoint. Explicit remote URLs never receive that local token. Use `--no-start` to require an already running daemon, `--port` to select a different local daemon, and `--state-dir` when creating an isolated local daemon. `--print-url` is a diagnostic that may print a local token; do not share its output. Closing a UI does not stop the background host; stop its owning process when all sessions are no longer needed.

## Run from source

```powershell
py -m venv .venv
.venv\Scripts\python -m pip install -e ".[desktop]"
scripts\launch-browser.cmd
scripts\launch-workspace.cmd --renderer auto
.venv\Scripts\python -m supermonoagent.launcher microsoft --diagnose
```

The traditional `python -m supermonoagent` daemon command is preserved. `supermono-client` is the installed dispatcher entry point. CLI modes are `host`, `relay`, `browser`, `workspace`, `microsoft`, and `native-attach`.

## Build

```powershell
.venv\Scripts\python -m pip install pyinstaller
powershell -ExecutionPolicy Bypass -File scripts/build-windows.ps1
```

The build includes packaged terminal JavaScript/CSS, WebView2 interop assemblies, pywinpty and pythonnet runtime hooks. Playwright, test frameworks and unused Qt/Tk GUI stacks are excluded. Runtime data and the local daemon token stay in the established user state directory; no credentials or relay addresses are baked into the build.

`build-manifest.json` records source file hashes and the executable checksum for this build; rebuild after changing source or workspace assets. The application must be distributed with its entire `_internal` directory.

Reference tooling: [PyInstaller](https://pyinstaller.org/en/stable/usage.html), [pywebview freezing](https://pywebview.flowrl.com/guide/freezing), [Microsoft Intelligent Terminal](https://github.com/microsoft/intelligent-terminal). Compare native terminal fidelity separately from WebView renderer measurements; a successful package build does not establish typing latency or GPU performance.

## Explicit host Token

Starting with 0.2.25, the host accepts an explicit owner Token:

```text
host --host 127.0.0.1 --port 6769 --relay "wss://117.50.48.22/ws" --code "office-pc" --token "YOUR_TOKEN" --state-dir "C:\Users\iamaf\.supermonoagent"
```

The value is validated and atomically saved to the state directory's `token`
file so clients and later restarts use the same identity. Omitting `--token`
keeps the existing saved/generated behavior. A second host cannot overwrite the
saved Token when its requested listening address is already occupied. This
option applies at host startup; it does not change a running daemon in place.
