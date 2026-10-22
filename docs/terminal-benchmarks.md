# Terminal transport benchmark

This benchmark checks the shared daemon through actual loopback WebSockets. It creates isolated temporary state, an ephemeral direct server, and an ephemeral relay with the real outbound host uplink. It never reads production relay settings, tokens, or desktop configuration.

```powershell
.venv/Scripts/python.exe -m pytest tests/test_client_integration.py -q
.venv/Scripts/python.exe scripts/benchmark-relay.py --compare-legacy
```

The benchmark reuses the integration test's controlled PTY fixture, so run it from the complete repository checkout. Results are written to `.omx/artifacts/benchmarks/current.json`. Optional arguments: `--pings 200 --rounds 100 --chunk-bytes 8192 --output <path>`.

## Recorded local result

Source: `.omx/artifacts/benchmarks/current.json`, recorded by the machine clock at `2026-10-20T17:15:48Z`. Windows 11 build 26300, x64 Intel Family 6 Model 197, Python 3.14.3, websockets 17.2. Each scenario uses one socket subscribed to all sessions, 200 application-level PING round trips, and 100 rounds of 8 KiB output per session. Numbers describe this run and are not improvement claims.

| Sessions | Route | PING p50 / p95 (ms) | Output MiB/s | Expected = received payload bytes | Process CPU seconds | Working set MiB |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | Direct | 0.083 / 0.120 | 149.58 | 819,200 | 0.0313 | 35.48 |
| 1 | Local relay | 0.188 / 0.335 | 51.24 | 819,200 | 0.0625 | 36.30 |
| 4 | Direct | 0.082 / 0.089 | 224.49 | 3,276,800 | 0.0313 | 37.19 |
| 4 | Local relay | 0.178 / 0.227 | 97.15 | 3,276,800 | 0.0781 | 37.90 |
| 16 | Direct | 0.083 / 0.129 | 242.16 | 13,107,200 | 0.0625 | 39.40 |
| 16 | Local relay | 0.176 / 0.241 | 126.21 | 13,107,200 | 0.1250 | 40.20 |

Every scenario verified the session identifier and exact payload of each frame, rejected duplicate frames, and checked a final PING barrier for trailing output. These are bounded loopback transfer measurements, not a remote WAN, continuous-load, or graphical-rendering benchmark.

With `--compare-legacy`, an additional experiment opens one historical relay socket per pane, all viewing sessions in the same channel, alongside one multiplex socket. For 1 / 4 / 16 sessions, total historical output payload was 8,192 / 131,072 / 2,097,152 bytes versus 8,192 / 32,768 / 131,072 bytes on the multiplex socket: exactly 1x / 4x / 16x payload copying. This measures the prior relay broadcast behavior, including output that individual pane UIs would discard. It excludes framing overhead and is not a corresponding claim about CPU or visible latency improvement.

CPU includes the benchmark client, daemon, and relay running inside one Python process. Windows CPU timing is coarse for these short intervals; normalized active-work CPU percentages in the JSON can exceed 100% due to thread activity and timing quantization. They do not measure idle daemon CPU. Working set is sampled in the same process after each scenario, so allocator/cache retention prevents treating differences as isolated per-session memory cost. Client compression is disabled; the host uplink retains the production default compression negotiation.

Output is injected at `Hub._flushed`, bypassing shell execution, ConPTY reading, pump batching, and terminal rendering. This isolates routing and transport overhead. It does not prove keyboard-to-screen latency or DOM/WebGL quality, and it cannot explain Orca's production relay CPU usage by itself.

## Real shell verification

`tests/test_client_integration.py` covers four tests on Windows:

- HTTP Bearer authentication accepts a protocol PING before HELLO.
- A new subscriber receives pending output in its history snapshot once; the later pump flush does not duplicate the snapshot bytes.
- Direct and local-relay clients receive each subscribed session once, even when subscription IDs repeat; separate clients write to the intended session, and writes to an unsubscribed session are rejected.
- A real PowerShell process and a real CMD process accept commands sent through the local relay. The same output marker reaches both the direct desktop connection and relay phone connection. Closing both views leaves the daemon session alive.

The last test is skipped on non-Windows hosts. It validates ASCII shell markers and shared-session lifetime, not Chinese IME, full-screen TUI fidelity, or physical Android hardware.

Next comparisons should use the same host and shell workload for classic browser, Orca-style desktop, Intelligent Terminal attach bridge, and Android DOM/WebGL. Record renderer, viewport/DPI, byte counts, key-to-visible p95, frame gaps, foreground/background CPU, and reconnect behavior separately. A frontend speed claim requires those measurements, not just this loopback transport result.
