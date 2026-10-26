# Folder transfer development

Status: receiving foundations only. The public 0.2.51 installer and services do
not advertise folder support. Ordinary files continue through their existing
protocol and single-file receiver unchanged.

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

## Integration gates still open

- Persist authenticated batches and manifest digests in broker/helper journals.
- Pin host/window/session/epoch/project binding across every entry and HTTP job.
- Reconcile lost begin/finish ACKs without creating duplicate roots or deliveries.
- Reserve quotas for pending folder entries without double-counting file uploads.
- Add actual Windows folder clipboard/picker enumeration and Android granted-tree
  streams, plus selection/progress/partial-result UI.
- Verify nested folders, reconnect, cancellation, cross-owner rejection, target
  changes and exact public HTTPS download into project `tmp` end to end.
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
