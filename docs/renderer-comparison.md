# DOM and WebGL terminal comparison

The renderer comparison runs against a temporary daemon on a random loopback port, with an inert controlled PTY. It does not connect to the user's daemon or relay, open a real shell, or alter saved desktop settings.

```powershell
.venv/Scripts/python.exe scripts/benchmark-renderers.py
```

Requires the repository's Playwright Python installation and installed Edge. Override the executable with `--edge <path>` and the artifact directory with `--output <directory>`. Default artifacts are `.omx/artifacts/renderers/current.json`, `workspace-dom.png`, `workspace-webgl.png`, and `workspace-context-loss-dom.png`.

## Verified behavior

On Windows 11 build 26300, Edge **155.0.4283.45** in headless mode reported effective **DOM** and effective **WebGL 2**, respectively. WebGL reported ANGLE with **Intel Arc 140T GPU, Direct3D 11**, rather than SwiftShader. This is the browser's reported backend; it does not independently measure GPU utilization.

Both runs used 1440 × 900 CSS pixels, device scale factor 1, the same 100 × 30 terminal grid, Unicode version 11, and `Consolas, monospace` at 14 px. Automatic fitting is disconnected for the measurement after opening the workspace. Consolas was available; Windows can still choose other fonts for CJK and emoji. The workspace's status bar may retain the last automatically fitted dimensions while the measurement deliberately fixes the terminal grid.

Three deterministic samples were verified:

| Sample | UTF-8 bytes | Writes | Checks |
| --- | --- | --- | --- |
| Complete UTF-8/ANSI sample | 105 | 1 | CJK, emoji, combining accent, SGR including RGB color, carriage-return progress update, erase-line |
| Same sample split at every byte | 105 | 105 | Splits UTF-8 characters and ANSI sequences; identical final text and cell attributes |
| 1,000 output lines | 58,007 | 1 | Identical final text, cell attributes, cursor, and Unicode cell widths across renderers |

Buffer SHA-256 and cell-style SHA-256 matched between DOM and WebGL and between whole and byte-fragmented small samples. Cell-style checks include position, text, width, foreground/background color mode and value, bold, italic, underline, and inverse. `中`, `文`, and `🙂` each occupied two cells in both renderers. The bulk buffer contained 1,001 lines including the trailing empty line. Equal-viewport screenshots are retained for visual inspection; rasterization and antialiasing are allowed to differ.

The test invoked `WEBGL_lose_context.loseContext()` on the actual terminal WebGL context. The addon waited for its recovery timeout, the workspace switched to effective DOM, the fallback counter increased from 0 to 1, and the terminal text hash stayed unchanged. The benchmark asserts this preservation and captures the resulting DOM screenshot. No page JavaScript errors occurred.

## Timing interpretation

The JSON records each sample's write-to-parser-callback interval, time until the first subsequent `requestAnimationFrame`, and time until a second animation frame. These are separate observations. A parser callback includes browser scheduling and is not a pure parser profile; an animation frame is a rendering opportunity and does not prove physical screen presentation.

The observed 58,007-byte sample parsed in single-digit milliseconds in both renderers, with two-frame observations around 34 ms on this machine. Each number is a single sample, not a p95, and run order/cache state can affect it. No claim that either renderer is globally smoother follows from these timings.

## Remaining device comparisons

No physical Android device was attached, and this run did not test the native Intelligent Terminal graphical UI. The Android APK should expose the same DOM/WebGL choice, then repeat text/attribute checks, screenshots, IME, soft keyboard resizing, actual touch scrolling, GPU context recovery, battery/CPU, and key-to-visible p95 on representative devices. WebView is the browser host; DOM and WebGL are alternative xterm rendering strategies within that host.

Keep DOM fallback available. Selecting WebGL only counts as a WebGL result when the effective renderer and live WebGL context confirm it; the JSON records both requested and effective values, plus the GPU identity and software/hardware report classification.
