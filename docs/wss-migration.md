# Current relay TLS migration

The shared source default is `wss://117.50.48.22/ws`, using HTTPS/WSS port 443.
The signed update feed uses the same HTTPS origin:
`https://117.50.48.22/updates/stable/manifest.json`.
Changing the suggestion does not establish a connection or modify user profiles.

## Certificate requirement

The server certificate must be trusted by Android and Windows and contain the
IP address `117.50.48.22` in its subject alternative names. A certificate for a
different hostname does not validate this IP endpoint. Do not bypass hostname
verification, install an accept-all trust manager, or fall back to public WS.
The endpoint is a deployment target, not evidence that the current server has
already passed certificate verification.

## Exact legacy mapping

`defaults.migrate_legacy_relay()` upgrades only `ws`/`http` URLs for the exact
IP `117.50.48.22`, port `8817`, with root or `/ws` paths and no embedded
credentials, query, or fragment. The destination is fixed to the same host's
WSS endpoint; a `SUPERMONO_DEFAULT_RELAY` override cannot redirect saved tokens
to another server. Android and Windows saved-connection loaders must apply the
same exact mapping before attempting an authenticated connection.

Existing TLS targets, other hosts, other ports, custom paths, and local
development connections remain untouched. Loopback WS/HTTP remains available
for local testing. This migration performs no live-profile writes by itself.

## Preserve existing terminal sessions

Terminate TLS at the existing server's reverse proxy and forward `/ws` upgrades
to its running relay on loopback port 6770. Preserve WebSocket upgrade headers,
binary frames, room codes, and authorization semantics. Forward the existing
signed update publication directory under `/updates/` over HTTPS. No Windows
PTY daemon or coding-agent process needs to restart for this edge change.

Before switching saved connections, verify the certificate and the HTTPS update
feed from both clients. Switch the desktop's relay uplink and phone to WSS for
the same room, then verify terminal output/input, notification delivery, and a
signed hot update. A phone alone cannot reach a room whose desktop uplink is
still connected to an isolated relay instance. Record the actual reverse-proxy
configuration and end-to-end evidence during deployment; this document does
not claim that deployment has occurred.

## Deployment evidence, 2026-10-23

The existing nginx now terminates trusted IP TLS on port 443 and forwards to
the original relay at `127.0.0.1:6770`; existing domain SNI services are preserved.
The public certificate includes the relay IP and renews through a dedicated
six-hour systemd timer. A renewal dry run and deploy hook passed. Configuration
and renewal units are maintained in `deploy/relay/`.

New legacy `/ws` connections on port 8817 receive HTTP 426. Existing established
connections from unrelated programs are not forcibly terminated. The old local
terminal daemon remains running to preserve PTYs. An authenticated loopback
compatibility bridge joins the same relay room over WSS; a scoped firewall rule
blocks the old daemon executable from reconnecting to the legacy public port.
`secure-relay.json` explicitly opts this machine into bridge startup. The
notification bridge also uses WSS. Loopback HTTP/WS remains local only.

Android manually downloaded, verified and activated signed UI release
`ui-20261023-wss-readable-controls-r2` through the top-right Update action.
The physical device passed eleven portrait/landscape and dialog checks, with
readable saved 18px font, connected terminal, and unchanged desktop 158×44 grid.
Private execution evidence is under `.omx/artifacts/wss-migration/` and
`.omx/artifacts/android-readable-verified/`; credentials and private signing
keys are excluded from source control.
