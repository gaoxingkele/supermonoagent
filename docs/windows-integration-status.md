# Windows feature integration (unpublished)

Latest verification (2026-10-27): 626 passed, 2 skipped in one selected integration
run, including the review fixes and manual-mode display regression. The initial
run's 10 failures were missing sidecar files in the publisher's synthetic source
fixture; that fixture is now complete and missing new sidecars still fail closed.
The original failing XML is retained separately. No runtime behavior was weakened
to make those tests pass.

The final 0.2.50 candidate has been rebuilt. Its 43 frozen product modules, 77
source fingerprints and 28 packaged static files match this branch. Hidden
WebView2 loads successfully; two isolated full-installer runs preserve private
configuration. Known private defaults do not occur in the public archive/source
scan. This is an unpublished candidate, not a claim that installed clients have
these fixes.

Production rollout is not yet ready: the existing file service has one unbound
queued job whose ownership cannot be proven from available records. It remains
unchanged. A parallel file-protocol rollout is being designed to preserve that
service and its task. The new broker/Windows-helper pairing has passed actual
Linux/Windows tests through an isolated encrypted SSH tunnel; that is not a
public HTTPS deployment.

This branch starts at published Windows 0.2.49 (`fd9fbb0`), preserving its
bounded history replay and slow native-footer recovery. Its baseline composer
and replay tests passed: 36 tests, recorded in the main workspace at
`.omx/artifacts/six-cli/integration-baseline.xml`.

Reviewed feature snapshots add central mylib metadata ranking and pinned skill
previews, shared terminal colors, question markers, explicit clipboard menus,
and refusal to default file exchange to the profile root. File content and
terminal context are not sent to the central skill catalog. The snapshot
allowlist and hashes are in `.omx/artifacts/six-cli/integration-stage.json`.

The source workspace contains concurrent changes. Only six reviewed hunks from
its workspace.js were applied. Loopback authentication changes, terminal-close
workflow changes, Android WIP, and unrelated daemon/history modifications were
not included. The clean 0.2.49 backend remains the integration base.

## Remaining gates

- Native text clipboard bridge and editor attachment queue integration is now
  verified within the scoped tests above; actual user OS clipboard and six live
  CLI checks remain.
- Resolve the actual active CLI project directory. Session.cwd is a startup
  value; a shell's PID may represent a launcher rather than its active child.
  Do not substitute home/tmp or parse displayed text to infer a directory.
- Bind any automatic directory selection to the file job through delivery.
  An unchanged numeric settings revision alone cannot detect a cwd change.
- Verify a packaged native client and real clipboard behavior for the six CLIs.
- Build, install into an isolated directory, then publish a version above 49.
  This branch has no new package or server publication yet.

Existing user daemons and PTYs have not been restarted to apply these changes.

## Verification recorded on 2026-10-26

- Feature group: 316 passed, 2 skipped; independent Node palette group: 11 passed.
- Clipboard/editor plus existing replay regression group: 178 passed.
- Consecutive editor pastes used real DesktopFiles, retained ClipboardFiles
  snapshots, a test broker, HTTP downloads and two test companions. Each test
  project/tmp received its own expected bytes; drafts and receipts stayed bound,
  all source handles were released and no PTY received input. This used supplied
  test source files rather than the user's live OS clipboard.
- The 0.2.50 executable builds. All 43 frozen product modules match this branch's
  source bytecode, with no unexpected product modules. Hidden real WebView2 loads
  the workspace/editor successfully, zero visible test windows and zero PTY input.

Evidence files live under the main workspace's `.omx/artifacts/six-cli/`:
`integration-feature-pass.xml`, `integration-clipboard-pass.xml`,
`frozen-verification.json`, and `native-smoke.json`. These are separate scoped
passes, not one full-suite result. The candidate has not been published.

## Review follow-ups

The earlier executable predates these review fixes and was superseded by the
rebuilt candidate described at the top. Its earlier verification files are not
used as proof of the current executable.

- DesktopAssistant cancels its model service before closing the central index.
  A gated regression reproduced a model starting after close in the old order;
  assistant/index/client regressions now pass (127 passed, 1 skipped).
- Clipboard menu teardown ignores late status callbacks. A gated read previously
  restored two removed UI nodes after destroy; menu and six-mode key tests now
  pass (15 tests).
- Image capture reservations now include pending memory and retained exception
  buffers. Upload project binding is checked again after fsync; legacy partial
  journal cleanup does not prevent new helpers starting. Independent review and
  the final combined run cover these fixes. No user daemon was restarted.
