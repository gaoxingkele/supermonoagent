# Folder transfer development

Status: Windows source integration is reviewed; not packaged or deployed. The
public 0.2.51 installer and services do not advertise folder support. Ordinary
files continue through their existing protocol and single-file receiver.

The requested result is a folder selected or pasted on Windows/Android, staged
through the relay and downloaded into the selected terminal host's project
exchange directory, defaulting to `tmp`. Keep folder hierarchy and empty
directories. Flattening files or requiring a manual ZIP does not complete it.

## Implemented foundations

`file_folders.py` validates a bounded versioned inventory, returns a detached
canonical object and computes its digest. Components are original names, with
explicit parent directories. Reject Windows unsafe names, traversal, absolute
paths, ADS, malformed Unicode, NFC/case aliases, prefix conflicts, duplicate JSON
keys and excessive bytes/count/depth. `.supermono-partials` is reserved by the
existing receiver; report it as unsupported instead of silently dropping it.

`file_folder_store.py` exclusively allocates the host-selected root and preserves
all child names. It reuses `TransferStore` at each leaf without relaxing the
single-filename rules. At most four transfers are active across the batch. It
rechecks recorded ancestor, root, directory and partial identities during writes
and immediately before publication. Snapshot identities use decimal strings;
reopen accepts a separately trusted exchange root and an exact bounded snapshot.
Cancellation keeps saved files and does not recursively remove receiving roots.

## Integrated and verified in isolated fixtures

The broker/helper now persist batches, immutable manifests, project bindings,
entry identities and receipts. Lost ACKs are reconciled without duplicate files;
restart recovery checks existing content and records its receipt before ACK.
Cancellation retires only uncommitted uploads, persists pending controls across
disconnects and prevents a late preparation from consuming receiving quota.
Already accepted jobs remain eligible and saved files are preserved.

Windows source APIs select a native directory or snapshot clipboard folders,
retaining bounded source handles and rejecting links or changed identities. The
editor adds explicit folder selection/paste, upload, state queries and stopping a
batch. Selection remains tied to its original connection even after reopening.
The existing signed workspace resource includes the new scripts without changing
the 17-file UI archive format.

Evidence is from owned files, mocked native selection and real local HTTP/helper
fixtures: backend regression 270 passed / 2 skipped; native/source/editor group
87 passed / 1 skipped; independent client/editor review 7 passed, including six
actual broker/helper browser scenarios. These are separate runs, not a single
combined-suite count. Independent backend recheck passed 8 targeted tests.

## Integration gates still open

- Real Windows clipboard/directory dialog acceptance remains unverified after the
  user stopped desktop control. Automatic terminal Ctrl+V routing for folders is
  separate from the editor's explicit folder-paste button.
- Android granted-tree folder selection and the broker UI consumer remain open.
- Verify upgrade activation for an already running older V2 helper, and actual
  public HTTPS folder delivery without affecting existing user jobs.
- Run a single combined release suite after integrating the reviewed history
  branch; the separate source groups above do not replace this gate.
- Build and publish a new numeric release after integration and regression QA.

These modules do not authenticate a session or select its current project. The
helper must supply and revalidate that authority. The filesystem checks retain
the existing receiver's trusted local directory assumption; they do not claim
administrator-race-proof atomic traversal across individual OS calls.

## Verification

Tests cover canonical inventories, real filesystem hierarchy and empty folders,
same basenames in different parents, exact bytes/digests, strict conflicts, quota,
wrong owner, final publication callbacks, replacement of ancestors/root/leaf/
partial directories, Windows junctions and detached reconnect snapshots. Some
symbolic-link tests require OS privileges and may skip on this Windows account.
Existing single-file, workspace and V2 tests run alongside these foundations.
