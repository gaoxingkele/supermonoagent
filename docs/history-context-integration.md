# Opt-in terminal history context integration

This branch adds the history-only changes from main `84fd8c4` to the reviewed
Windows 0.2.51 source, preserving its bounded replay scheduling and footer reader
lifetimes. It does not replace the published installer or update a running daemon.

An explicitly opted-in multiplex client can receive OP23 history context and
request a retained interval by stream ID, epoch and byte position. Grid changes
start a new epoch; a cursor outside the retained interval cannot silently claim
an exact resume. Legacy clients continue their existing OP8/output behavior.

The existing legacy ring and the additional epoch ring are each limited to
256 KiB. Metadata is limited to 4096 bytes and pending resume cursors to 256.
Queue capacity remains 32 frames. Snapshot framing remains ordered before later
live output, with lazy replay and dequeue credit. Native footer recovery shares
the existing four actual reader slots; cancellation does not release a running
OS reader's slot before it finishes.

Verification includes mixed legacy/opt-in clients, 20-window bounded replay,
empty resumes followed by live output, cursor gaps, resize epochs and stale or
blocked native footer results. The implementation lane's 17-module run passed
189 tests and 7 subtests; independent review passed 77 targeted tests and found
no P1/P2. The final root eight-module verification passed 116 tests. Twenty AST
preservation checks confirm the critical 0.2.51 sender, scheduling, authorization
and native reader lifetime behavior remains unchanged.

This is rendition/interval metadata, not a full VT checkpoint. It does not
restore Grok body content already absent from retained output. Android archive
integration, client rendering, package/device QA and production rollout remain
separate completion gates; this source-only verification does not prove them.
