# Visible console capture prototype

This branch adds `read_console_viewport()` and the `console-snapshot --viewport`
action. It does not connect this data to daemon/client traffic and is not a
repair deployed to any existing user session.

The reader binds PID, parent and creation time, captures the actual visible
rectangle rather than the whole backing buffer, and accepts two matching complete
samples with matching metadata. It preserves raw UTF-16 units and Win32 attributes,
including LVB bits, plus palette/default attributes and cursor metadata. Limits
are 40,000 cells, 500 columns, 200 rows, 1 MiB JSON and a bounded child timeout.

Grid reads use a read handle. An owned Windows 11 experiment found cursor-info
queries denied on that handle; only that access-denied case uses a short-lived
read/write-rights handle exclusively for `GetConsoleCursorInfo`. No write, input
or resize function is called through it. All handles close on success/failure.

The old reader/footer functions and their behavior remain unchanged. Validation:
72 tests passed, including a hidden owned native console with an offset viewport,
Chinese/emoji raw units, bottom-row text, hidden cursor, PID identity and cleanup.
No real user's console was read for this proof.

Two equal reads do not form an application-level atomic transaction. This data
does not preserve all VT state, truecolor, dim, combining glyph semantics or
scrollback. Future product integration must also prove authorization, session-to-
PTY binding, output/lease epochs, packet bounds and stale-result rejection.

A separate owned native ConPTY experiment disproved assuming that shrinking and
restoring columns forces an application-independent repaint: its static body
did not reappear in the stream. Do not introduce an automatic resize workaround
for missing Grok history based on the capture prototype.

An explicit OP22 `console.viewport` preview route now exists on this isolated
branch. Only the owning authenticated, subscribed client can request its session;
the server chooses the cached native process identity. It shares the existing
four-reader budget, checks authorization and identity again after capture, and
retains a running reader's slot through request cancellation or timeout. Requests
cancelled while waiting for the executor are checked before starting a read.

The wire payload is marked `console_viewport_preview` and
`stream_checkpoint: false`; it must never be injected into the VT output stream.
UTF-16 cells and attributes use bounded little-endian base64 matrices. Windows
creation FILETIME uses a canonical decimal uint64 string because JavaScript JSON
numbers cannot preserve its precision. Native identity checks still use integers.
The preview can capture a later display while output continues; it is not a
synchronized VT checkpoint. No client display or automatic polling is included.

Fresh root verification: 131 tests passed across viewport RPC, native viewport,
existing console snapshot and protocol modules. A deterministic cancellation
regression also passed 100 consecutive handoffs. This route is not included in
the Windows 0.2.51 build and has not been deployed to a user's daemon.
