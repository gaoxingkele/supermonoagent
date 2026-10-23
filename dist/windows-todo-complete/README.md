# Three Windows clients, one daemon

## TODO completion build

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
