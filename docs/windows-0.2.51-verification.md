# Windows 0.2.51 verification

This numeric release includes the earlier candidate's project-bound attachment
delivery, editor, clipboard safeguards and skill previews. It adds coexistence
with the older file service and color-environment handling for newly created
Windows PTYs. No dependency was added.

## Compatibility

File clients keep a query-free configured `/file/ws` endpoint and select V2 on
the wire. The server routes V2 to an isolated service and state directory, while
older clients continue to use V1. The receiving helper checks the V2 HTTP response
marker, content digest and project binding before accepting a download. Old
helper identities and journals are not reused by the V2 helper.

New PTYs normalize empty/dumb terminal color detection and remove inherited
`NO_COLOR` under the default interactive policy. Set
`SUPERMONO_TERMINAL_COLOR=preserve` to retain the original color environment.
Existing terminal processes are unaffected.

## Evidence collected on 2026-10-27

- Source regression: 681 passed, 2 skipped.
- Frozen executable: 43 product modules match the source.
- Build provenance: 77 source fingerprints and 28 packaged static files match.
- Hidden WebView2 loads the workspace/editor without terminal input.
- Two isolated full installations preserve Token and private file configuration.
- The public archive contains neither of the two checked private configuration values.
- Public WSS/HTTPS: an actual Windows FileCompanion downloads PNG/PDF byte
  fixtures into an owned project's `tmp`, with exact content, digest and binding.
- The old authenticated socket stays connected across the routing reload; old
  service process identity, queued job and authentication file remain unchanged.

Public installer SHA-256:
`b1bfd384cd8f27683e3a7777520633b78b5b2f1fb8b848d30852fba1ca4746ae`.

## Remaining verification and work

Real OS clipboard use and the full six-CLI keyboard/cursor interaction audit
remain incomplete. The public transfer test uses an owned parent fixture, not a
user's active CLI. Automatic detection of a CLI's changing project directory,
folder uploads, remaining Android work and the full original task are pending.
No user daemon was restarted or locally replaced for this release verification.

The Grok missing-history display remains unresolved. A separate native-console
preview prototype is not part of this executable and is not a VT checkpoint or
a lossless history recovery mechanism.
