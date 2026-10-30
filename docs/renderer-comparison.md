# DOM and WebGL terminal comparison

The renderer comparison runs against a temporary daemon on a random loopback port, with an inert controlled PTY. It does not connect to the user's daemon or relay, open a real shell, or alter saved desktop settings.

```powershell
.venv/Scripts/python.exe scripts/benchmark-renderers.py
```

Requires the repository's Playwright Python installation and installed Edge. Override the executable with `--edge <path>` and the artifact directory with `--output <directory>`. Default artifacts are `.omx/artifacts/renderers/current.json`, `workspace-dom.png`, `workspace-webgl.png`, and `workspace-context-loss-dom.png`.

## Verified behavior

On Windows 11 build 26300, Edge **156.0.4314.42** in headless mode reported effective **DOM** and effective **WebGL 2**, respectively. WebGL reported ANGLE with **Intel Arc 140T GPU, Direct3D 11**, rather than SwiftShader. This is the browser's reported backend; it does not independently measure GPU utilization.

Both runs used 1440 × 900 CSS pixels, device scale factor 1, the same 100 × 30 terminal grid, Unicode version 11, and `Consolas, monospace` at 14 px. Automatic fitting is disconnected for the measurement after opening the workspace. Consolas was available; Windows can still choose other fonts for CJK and emoji. The workspace's status bar may retain the last automatically fitted dimensions while the measurement deliberately fixes the terminal grid.

Three deterministic samples were verified:

| Sample | UTF-8 bytes | Writes | Checks |
| --- | --- | --- | --- |
| Complete UTF-8/ANSI sample | 105 | 1 | CJK, emoji, combining accent, SGR including RGB color, carriage-return progress update, erase-line |
| Same sample split at every byte | 105 | 105 | Splits UTF-8 characters and ANSI sequences; identical final text and cell attributes |
| 1,000 output lines | 58,007 | 1 | Identical final text, cell attributes, cursor, and Unicode cell widths across renderers |

Buffer SHA-256 and cell-style SHA-256 matched between DOM and WebGL and between whole and byte-fragmented small samples. Cell-style checks include position, text, width, foreground/background color mode and value, bold, italic, underline, and inverse. `中`, `文`, and `🙂` each occupied two cells in both renderers. The bulk buffer contained 1,001 lines including the trailing empty line. Equal-viewport screenshots are retained for visual inspection; rasterization and antialiasing are allowed to differ.

The test invoked `WEBGL_lose_context.loseContext()` on the actual terminal WebGL context. The addon waited for its recovery timeout, the workspace switched to effective DOM, the fallback counter increased from 0 to 1, and the terminal text hash stayed unchanged. The benchmark asserts this preservation and captures the resulting DOM screenshot. No page JavaScript errors occurred.

## Fractional-DPR backing canvas check

Run `.venv/Scripts/python.exe -m pytest tests/test_webgl_canvas_pixels.py -q`. This loads the shipped xterm/WebGL files in Edge at device scale factors 1, 1.25, 1.5, 2 and 3, with the desktop CJK fallback fonts, shared highlighting, Unicode, emoji and repeated cursor-addressed clear/repaint cycles. It checks the terminal buffer, WebGL viewport, actual backing-canvas size and visible ink in every expected text row. Artifacts are written to `.omx/artifacts/webgl-canvas-pixels/`.

The shared vendor file contains a bounded compatibility guard in `observeDevicePixelDimensions`: a Chromium implementation can report CSS-sized `devicePixelContentBoxSize` values instead of physical pixels, shrinking the backing canvas after WebGL has established its larger viewport. If the observation differs from computed CSS size times DPR by more than two pixels, the guard uses the consistent CSS-times-DPR size. Small rounding differences retain the upstream observation; CSS transforms are not measured through `getBoundingClientRect`. This is a canvas-size correction, not a terminal-data rewrite or a forced DOM fallback.

## Viewport and reading-state checks

The shared xterm viewport ignores native scroll events until its row height is finite and positive. This prevents an early `0 / 0` calculation from storing `NaN` as the viewport position; `tests/test_workspace_runtime.py::test_zero_height_viewport_scroll_keeps_finite_position` exercises that exact condition.

Workspace history recovery permits another snapshot after a completed recovery in the same connection, instead of blocking all later gaps. Contiguous live bytes from the same stream can cross a grid epoch; recovery is reserved for stream changes and byte gaps. While a user is reading normal-buffer history, a snapshot no longer resets the terminal buffer. Returning to the latest output requests a fresh synchronized frame. Ordinary resize and snapshot paths preserve the reading distance. Cover these cases with `tests/test_resize_recovery.py`, `tests/test_workspace_paging.py`, `tests/test_workspace_runtime.py`, `tests/test_workspace_history_resume.py` and `tests/test_relay_account_ui.py`.

## Timing interpretation

The JSON records each sample's write-to-parser-callback interval, time until the first subsequent `requestAnimationFrame`, and time until a second animation frame. These are separate observations. A parser callback includes browser scheduling and is not a pure parser profile; an animation frame is a rendering opportunity and does not prove physical screen presentation.

In the latest run, the 58,007-byte sample's parser-callback interval was 13.2 ms for DOM and 17.5 ms for WebGL, with two-frame observations around 32 ms. These are single observations, not a p95 or a pure parser benchmark; run order, cache state, and browser scheduling can affect them. No claim that either renderer is globally smoother follows from these timings.

## Remaining device comparisons

No physical Android device was attached, and this run did not test the native Intelligent Terminal graphical UI. The Android APK should expose the same DOM/WebGL choice, then repeat text/attribute checks, screenshots, IME, soft keyboard resizing, actual touch scrolling, GPU context recovery, battery/CPU, and key-to-visible p95 on representative devices. WebView is the browser host; DOM and WebGL are alternative xterm rendering strategies within that host.

Keep DOM fallback available. Selecting WebGL only counts as a WebGL result when the effective renderer and live WebGL context confirm it; the JSON records both requested and effective values, plus the GPU identity and software/hardware report classification.
