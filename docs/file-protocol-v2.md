# Parallel file protocol rollout

The configured server address remains `/file/ws`. New clients and helpers add
only the fixed internal selector `?file_protocol=2`; user-configured query strings
remain invalid. The new broker accepts that exact selector and rejects malformed,
duplicate or unrelated queries before authentication.

HELLO includes integer `file_protocol: 2`, checked before host enrollment/pinning.
READY must echo 2. A proxy accidentally forwarding a new connection to the old
service cannot replace an old host: the old strict HELLO allowlist rejects the
new field first. Clients never retry with a bare legacy URL.

HTTP tickets retain their existing path and Bearer authentication. The helper
sends exactly one `X-SuperMono-File-Protocol: 2` header and requires the response
echo. This allows the HTTPS reverse proxy to select an isolated V2 service while
leaving old requests on V1. Profile URLs and installer defaults do not change.

The manager reads the original private profile but runs in `file-exchange/v2`.
It never reuses or stops a V1 helper, its lease, identity or journal. An existing
private, bounded and validated workspace-settings file may be copied once using
create-only publication. Existing V2 settings cannot be overwritten. Host secrets
and journals are not copied. Existing room configuration is inherited normally.

## Deployment contract

- Keep V1's service, registry, payloads and pending jobs unchanged.
- Use a separate V2 service, loopback listeners, registry and staging directory.
- Both services read the same existing authorization configuration; do not make
  an independent credential copy that misses later revocations.
- Route the fixed WS selector and HTTP header to V2 on the existing HTTPS origin.
  Preserve URI/query, authentication and WebSocket upgrade headers.
- Validate V1 connection/PID/pending-task preservation and positive/negative V2
  traffic through the actual HTTPS router before promoting a client package.

Nginx reference: [map](https://nginx.org/en/docs/http/ngx_http_map_module.html),
[proxy_pass](https://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_pass),
[WebSocket proxying](https://nginx.org/en/docs/http/websocket.html).

## Verification boundary

Scoped V2 regressions: backend 302 passed / 2 existing skips; browser group
61 passed. Tests cover a stripped-query proxy against the prior source, rejected
old READY, malformed selectors/headers, and simultaneous V1/V2 helper lifetimes.
These are local fixtures, not production routing acceptance.

The earlier candidate 0.2.50 was committed and its archive is immutable. A new
distribution containing this compatibility change must use a higher numeric
version. The implementation does not add current-CLI-directory detection or
repair Grok's incomplete terminal replay.
