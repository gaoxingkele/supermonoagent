# Terminal readability and retained Codex footer

## Visible changes

The mobile viewer keeps its chosen font and canonical terminal width. A short
canonical grid expands local row spacing to fill the terminal area; a taller
grid keeps natural spacing and local scrolling. This never requests a different
PTY size. Both clients set xterm's minimum contrast ratio to 4.5.

Window lists use one row: `window title（Codex）`, `（Claude Code）`, or another
recognized programming agent. Shell names are retained as creation options and
metadata, but not shown as a second line in the chooser. Explicit backend agent
metadata has priority. Older backends can supply an explicit OSC 7501 app marker;
unknown tools remain `其他`. The classification does not affect authorization.

## Why a footer can contain only numbers

`OutputPump` retains a bounded raw ANSI stream. An incremental application can
paint its fixed labels once, then update only the digits. Once that original
paint leaves the ring, a fresh xterm replay cannot recover those characters.
Keeping SGR state repairs cropped colors but does not recreate missing cells.

The current Windows consoles still contained their complete `context` and
`weekly` labels, confirmed through read-only capture. Their foreground colors
were distinct from the background. Private evidence is in
`.omx/artifacts/console-snapshot/`; terminal contents are excluded from Git.

## Recovery for the running legacy Windows daemon

`console_snapshot.py` uses an isolated hidden helper to read a previously bound
console. Its explicit local configuration is `console-recovery.json` in the
daemon state directory. Each session binding contains the shell PID and its
creation FILETIME; the expected daemon parent PID and exact loopback backend are
also checked. An absent or mismatched configuration performs no console reads.
The helper checks process identity before and after capture and bounds its data.

Only the authorized session's known canonical dimensions are accepted. A genuine
Codex footer near the bottom is reconstructed from its current character cells
and legacy console palette. The original history frame stays byte-for-byte
intact. `Frontend` and `RelayBridge` wait for size metadata, then send a separate
OP_OUTPUT supplement before later live bytes. Saved cursor, origin mode,
rendition, other screen rows and history remain intact. The final screen row is
excluded to prevent scrolling. No PTY input or resize is sent.

When the console process list positively contains `codex.exe`, the supplement
also carries an app-only display identity marker. It carries no task state and
cannot produce a task-completion notification. It never infers an agent from a
process command line or authentication data.

This recovery covers the bound current Windows Codex footer. Other computers
need their own corresponding host support and bindings. Other fixed TUI regions
still use bounded history. Console attributes provide the traditional palette,
not every original true-color or underline attribute. These constraints are
why recovery is narrow and explicit rather than a replacement for raw output.

The Windows API behavior is documented in
[AttachConsole](https://learn.microsoft.com/en-us/windows/console/attachconsole)
and [ReadConsoleOutput](https://learn.microsoft.com/en-us/windows/console/readconsoleoutput).

## Verification

- `tests/test_android_fullscreen_fit.py`: actual row surface, short and tall
  grids, folded-size viewports, rotation, DOM and WebGL, unchanged font/grid.
- `tests/test_compact_agent_labels.py`, `tests/test_agent_catalog_metadata.py`:
  compact actual DOMs, explicit and learned identities, clear/unknown handling.
- `tests/test_console_snapshot.py`: generation validation, opt-out, exact cells,
  cursor/history/SGR preservation, full-size replay plus separate supplement.
- `tests/test_footer_proxy_order.py`: both real proxy paths, legacy metadata
  ordering, unauthorized session isolation, no input or resize.

Signed UI release `ui-20261023-terminal-readability-r8` includes the mobile
automatic discovery, compact agent labels, row fitting and contrast changes.
The Windows native package is `dist/windows/0.2.20/`; Android UI updates remain
compatible with the installed native updater without reinstalling the APK.

Native Windows 0.2.20 frontend 6778 was independently verified with a headless
client against both running consoles: complete footer labels, canonical 158x44
and no input or resize. Final native screenshots were blocked by the Windows
and Android lock screens. The phone was last observed on r6 before locking;
r8 is published but its on-device activation still requires an unlocked screen.
