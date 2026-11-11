# Private journal service candidate

These files are additive examples. They have not been installed on a production
server. The existing terminal relay, its configuration and live sessions do not
need a restart. Production installation requires a working administrator channel.

## Administrator preflight

1. Inspect the existing HTTPS relay authority, nginx configuration, service users,
   owner authentication file and listeners. Reserve an unused loopback port
   (the example uses 8794). Do not replace an existing listener or configuration.
2. Install this application and its existing dependencies in a separate service
   directory. Create a dedicated `sma-journal` service account with no login shell.
   Create `/var/lib/supermono-journal` owned by that account with mode 0700.
3. Provide a private, read-only owner authentication file at
   `/etc/supermono-journal/owners.json`. It uses **exactly** the existing
   `file_broker._auth` schema: schema=1, owners with scope, token_sha256 array,
   rooms array and enrollment_key. Grant only the service account read access.
   Reuse the configured scopes and grants; update this configuration atomically
   whenever a token rotates or a room grant is revoked. Never place plaintext
   viewer tokens in systemd arguments, a public repository or a URL.
4. Review and copy the example systemd unit. Replace its installation paths and
   the explicit allowed browser Origin with the actual relay HTTPS authority.
   Start only the new journal unit. The CLI binds **only 127.0.0.1**. Confirm the
   listener and private SQLite/WAL/lock-file ownership before exposing its route.
5. Add only the exact `/journals/ws` nginx location to the existing HTTPS virtual
   host. Retain its current TLS settings and terminal routes. Run `nginx -t`
   before a graceful nginx reload. Never replace the existing virtual host.
6. Use synthetic enrolled-host and viewer fixtures to verify owner A/B isolation,
   rejected unknown Origins, replay rejection and current revocation. Disconnect
   the source entirely, restart the journal service, and reconstruct a large
   archived final from the central content chunks. Local tests are not evidence
   that this production gate has passed.

Back up the SQLite database with SQLite's backup API (or stop only this new
service before copying its database). Copying a live database without its WAL can
lose committed records. Restore the database, settings and host pins together.
The writer lock rejects a second process using the same root. Capacity errors are
explicit; no automatic private-history deletion or retention policy is enabled.

## Transport and authentication

The independent protocol is `sma-journal-1` at the existing relay authority's
`wss://<relay>/journals/ws`. HTTPS/WSS certificate validation remains enabled.
Plain HTTP/WS destinations are accepted only for loopback development. No token
appears in a query string, cookie, browser storage key, error, or application log.
An absent Origin is allowed for native clients; every supplied browser Origin
must match an explicit server allowlist entry.

A viewer sends this first message (values below are structural placeholders):

```json
{"op":"hello","protocol":"sma-journal-1","role":"viewer","token":"PRIVATE_VIEWER_TOKEN","room":"EXTERNAL_ROOM"}
```

The source additionally sends `host_installation_id`, a **separate private**
`host_secret`, integer Unix `timestamp`, random opaque `nonce`, and `proof`.
`proof` is hex HMAC-SHA256 using the host secret over canonical compact UTF-8 JSON
`["sma-journal-1","source",room,host_installation_id,timestamp,nonce]`.
The first connection also requires `enrollment_proof`: hex HMAC-SHA256 using the
owner's separate enrollment key over canonical JSON
`["sma-journal-1","enroll",room,host_installation_id,SHA256(host_secret),timestamp,nonce]`.
The supplied token derives the scope and current room grants; callers never
supply an authoritative owner scope. A viewer token alone cannot enroll or append.
Only a digest of the pinned source secret is persisted. Pins and nonce replay
state survive restarts; an existing pin cannot be changed by re-enrolling.

Successful hello: `{"op":"hello","protocol":"sma-journal-1","ok":true,"role":"viewer"}`.
Requests: `{"op":"request","request_id":"opaque","action":"list","params":{}}`.
Responses: `{"op":"response","request_id":"opaque","ok":true,"result":{...}}`,
or `ok:false,error:<fixed_code>`. Errors contain no payload, filesystem path or
credential. A token or room change applies to existing connections and sealed
archives. Accounts sharing a room still have different archive scopes; a room
grant never confers another owner's old archive access.

Read actions are `status` (`journal_id`), `list` (`cursor`, `limit`), `read_page`
(`journal_id`, `projection`, `cursor`, `limit`) and `read_content` (`journal_id`,
`content_id`, `offset`, `limit`). IDs are opaque; paths and caller scopes are
rejected. Page default/max are 50/100 and envelopes are at most 192 KiB. Content
chunks contain at most 48 KiB of decoded bytes, with base64 transport, full-body
length/hash and `next_offset`; concatenate bytes **before** decoding UTF-8.

## Source durability protocol

`MirrorClient(store, destination, SourceCredentials(token, host_secret,
enrollment_key))` exposes `await sync_once(journal_id)` and
`await run(journal_id, stop_event)`. Source credentials are private constructor
input and are never included in the store's pinned destination. One normalized
destination remains immutable for a journal. Local recording is independent of
network retries. Source startup/integration is explicit; this service does not
discover terminals or launch a daemon.

When an explicit trusted source route maps local and relay owner labels, pass
`local_owner_scope=<local owner>` to `MirrorClient`; the pinned destination holds
the external owner and room. The local manifest retains its original scope and
room. The service derives the external scope from the token and rejects any
mismatching batch, even when both accounts have the same room grant. Changing
the pinned external owner later is a destination conflict, including at the same
authority and destination ID. Viewer requests cannot configure this mapping.

Source-only `content.stage` parameters are `journal_id`, `content_id`, decoded
byte `offset`, `total_bytes`, full-body `sha256`, and `data_base64`. Identical
chunk retries are idempotent; gaps, changed bytes, sizes or hashes are rejected.
The result has `content_id`, `received_bytes`, `complete`. It is **not a journal
receipt**. A crash with only staged content cannot make an event visible.

Source-only `append_batch` parameters are `{batch,journal}`. `journal` is a
manifest snapshot; its immutable identity must match the authenticated source
and batch. The central lifecycle is derived only from committed immutable
`status` events' `metadata.journal_state`; a future sealed manifest cannot seal
an earlier pending batch. Replication ACKs preserve committed capture errors
such as `capture_gap`: successful replication does not establish capture
completeness or clear the source's known error. `batch.contents` is an ordered list of distinct
`{content_id,bytes,sha256}` descriptors. Every referenced body must be staged,
complete and length/hash verified before all events and the receipt commit in a
single SQLite FULL-synchronous transaction. Sequence gaps and reused event IDs
conflict; an exact duplicate batch returns the same persisted receipt.

Only the returned `result.receipt` advances a source cursor. It binds all of:
destination_id, owner_scope, external_room, host_installation_id, journal_id,
source_epoch, batch_id, first_seq, last_seq, batch_sha256, through_seq,
last_event_id, commit_id. The local store validates it against its **persisted
exact pending batch**. Lost ACKs retry the same batch and content IDs. A receipt
for another owner, target, epoch, batch, range or hash cannot advance the source.

Default limits are 64 connections, a single storage executor, 32 pending storage
slots, 64 MiB per body, 512 MiB incomplete staging, 2 GiB stored body allocation,
and 100,000 content entries. Configure capacity deliberately; full metadata and
raw output remain private, including intentionally printed synthetic secrets.
No provider/model calls are made by the service or mirror.
