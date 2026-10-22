# Microsoft Intelligent Terminal shared-session client

This third Windows client launches a console adapter inside Microsoft's
[experimental Intelligent Terminal](https://github.com/microsoft/intelligent-terminal).
The adapter forwards VT bytes and keys over the existing WebSocket protocol.
Python daemon retains the actual shell/ConPTY; a phone and this console attach to
the same session ID. ACP features in Intelligent Terminal do not automatically
manage that remote shell. This is an adapter, not a fork or embedded terminal control.

## Start

Start the normal Python daemon first. Install Intelligent Terminal using its
official signed MSIX releases and enable its **wtai.exe** app execution alias.
The launcher never silently substitutes Windows Terminal or an unrelated `it.exe`.

On the development Windows machine, the official package **0.2.2572.0** was
installed successfully on 2026-10-21 using
`winget install --id Microsoft.IntelligentTerminal -e --silent --accept-package-agreements --accept-source-agreements`.
The `wtai.exe` alias resolves and `--diagnose` reports available. Evidence is in
`.omx/artifacts/microsoft/`. Discovery is not a claim of an interactive rendering
test: no Intelligent Terminal window was opened during unattended validation.

```powershell
scripts\launch-microsoft.cmd --diagnose
scripts\launch-microsoft.cmd --install  # prints official instructions only
python -m supermonoagent.native_attach --list
scripts\launch-microsoft.cmd --session SESSION_ID
scripts\launch-microsoft.cmd --shell cmd
scripts\launch-microsoft.cmd --shell powershell --take-control
```

Set `SUPERMONO_WTAI` or pass `--exe` if discovery fails. The Python environment
must have the package installed, or retain the launch script's PYTHONPATH.
`--shell` creates a daemon session, never an isolated terminal-owned shell.
Existing sessions are selected using `--session`; list them before attaching.

Token loading: `SUPERMONO_TOKEN`, then `--token-file`, then the local daemon file
`$SUPERMONO_STATE_DIR/token` when that environment variable is set, otherwise
`~/.supermonoagent/token`. No token value is put in the terminal launch arguments.
Do not use token-bearing URL query strings. The daemon must already be running.

For relay access use `--url wss://YOUR_RELAY/ws --relay-code ROOM` and a token
valid on that host through the environment or token file. The relay remains a
transport broker; all input and resize frames identify the subscribed session.

## Controls and limits

- Ctrl+C reaches the remote shell. **Ctrl+] disconnects this view** and does not
  terminate the shell; reopening the same session leaves phone access intact.
- Viewport dimensions are checked every 100 ms and sent only when changed.
  `--take-control` explicitly requests the resize lease; other viewers cannot
  silently override an active controller. Disconnect releases the lease.
- Console input uses VT mode, raw Ctrl handling and UTF-8 Windows console code
  pages. Original modes and code pages are restored on normal/error exit.
- Subscription replays bounded output history. It is not a canonical screen
  snapshot and cannot guarantee exact restoration of an arbitrary running TUI.
- Reliable multiplex negotiation is required. Upgrade an old daemon rather than
  risking input being sent to whichever session another view selected.
- Chinese IME behavior, rendering, selection and scrollback depend on the
  installed terminal version. Measure these on the actual terminal/device;
  bridge unit tests alone do not establish a performance advantage.

Validation includes an actual hidden Windows console: UTF-8 Chinese output,
injected Ctrl+C/Chinese/Ctrl+] key records through ReadFile, viewport dimensions,
and restoration of original console modes/code pages. Socket tests cover targeted
input, output filtering, resize deduplication and preservation of the shell on
disconnect. This hidden console test exercises Windows Console APIs, not the
Intelligent Terminal application's GPU renderer.

The isolated real-host test also passed for daemon-owned **CMD and PowerShell**:
a hidden native console sent commands, a second phone-style WebSocket viewer saw
the output, Ctrl+] detached with exit code 0, and the same session remained alive.
Reproduce with `.omx/artifacts/microsoft/verify_real_host.py` against the isolated
host configured in that script; results are in `real-host-result.json` beside it.
ConPTY can enable Win32 input mode (`CSI ? 9001 h`), so the bridge recognizes
Ctrl+] in both ordinary input and Win32 key records and otherwise forwards those
records unchanged. This matters for PowerShell/PSReadLine key compatibility.

Frozen builds dispatch `SuperMonoAgent.exe native-attach ...`; the executable
must retain console support so Windows supplies stdin/stdout console handles.

The adapter itself can also run directly in an existing Windows console using
`python -m supermonoagent.native_attach --session SESSION_ID` for diagnostics.
