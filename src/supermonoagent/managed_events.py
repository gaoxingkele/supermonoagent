"""Coalesced, bounded state revisions for authorized managed subscriptions."""
from collections import deque
from dataclasses import dataclass, field
from uuid import uuid4

from .protocol import OP_RUNTIME_EVENT, pack_json, unpack_json


TARGET = ('window_id', 'session_id', 'journal_id', 'process_generation')
CURSOR = ('host_installation_id', 'journal_id', 'process_generation', 'runtime_epoch', 'revision')
MAX_FRAME_BYTES = 16 * 1024
COALESCE_SECONDS = .025
RETAINED_REVISIONS = 128
_ACTIVE = ('submission_id', 'provider_turn_id', 'cancel_version')
_PERMISSION = ('permission_request_id', 'submission_id', 'provider_turn_id', 'tool_name',
               'choices', 'requires_user_interaction', 'expires_at', 'state')
_RECEIPT = (*TARGET, 'nonce', 'submission_id', 'event_id', 'seq', 'state', 'dispatch_state')
_STATUS = (*TARGET, 'interaction_mode', 'state', 'provider', 'provider_version',
           'provider_root_id', 'provider_root_confirmed', 'policy', 'last_outcome')


def _fields(value, allowed):
    return {key: value[key] for key in allowed if key in value}


def _public_status(status):
    return {**_fields(status, _STATUS),
            'active_submission': _fields(status['active_submission'], _ACTIVE) if status['active_submission'] else None,
            'permissions': [_fields(item, _PERMISSION) for item in status['permissions'] if item['state'] == 'pending']}


@dataclass
class _Track:
    session: object
    target: dict
    revision: int = 0
    status: dict | None = None
    pending: dict | None = None
    receipt: dict | None = None
    recent: deque = field(default_factory=lambda: deque(maxlen=RETAINED_REVISIONS))


class ManagedEvents:
    def __init__(self, hub, loop, installation, authorize):
        self._hub, self._loop = hub, loop
        self._authorize = authorize
        self._identity = {'host_installation_id': installation, 'runtime_epoch': uuid4().hex}
        self._tracks = {}
        self._scheduled = None
        self._closed = False

    def observe(self, session, status, receipt=None):
        if self._closed or self._hub.sessions.get(session.id) is not session:
            return None
        track = self._tracks.get(session.id)
        if track is None or track.session is not session:
            track = self._tracks[session.id] = _Track(session, _fields(status, TARGET))
        public = _public_status(status)
        if public != (track.pending if track.pending is not None else track.status) or receipt:
            track.pending = public
            if receipt and receipt['state'] == 'accepted':
                track.receipt = _fields(receipt, _RECEIPT)
            self._schedule()
        return track

    def snapshot_cursor(self, session, status):
        track = self.observe(session, status) if session is not None else None
        if track is not None:
            # A snapshot must name the exact committed revision it describes.
            self._commit(track)
        return self._cursor(track)

    def end(self, session, status):
        track = self.observe(session, status)
        if track is None:
            return
        # The session is still authorized here. EOF immediately removes its
        # subscription, so a terminal event must skip the coalescing delay.
        self._commit(track)
        for client in tuple(self._hub.clients.values()):
            if session.id in client.managed_after:
                self._deliver(client, session.id)

    def _cursor(self, track):
        return {**self._identity, 'revision': track.revision if track is not None else 0}

    def subscribe(self, client, after):
        client.managed_after = {sid: revision for sid, revision in client.managed_after.items()
                                if self._authorized(client, sid)}
        after = after if isinstance(after, dict) and len(after) <= 256 else {}
        for sid in client.subscriptions - client.managed_after.keys():
            track = self._tracks.get(sid)
            if track is None or not self._authorized(client, sid):
                continue
            cursor = after.get(sid)
            current = {**track.target, **self._cursor(track)}
            same = isinstance(cursor, dict) and all(cursor.get(key) == current[key] for key in CURSOR[:-1])
            revision = cursor.get('revision') if same else None
            client.managed_after[sid] = (track.revision if sid not in after else
                revision if type(revision) is int and 0 <= revision <= track.revision else None)
        if client.managed_after:
            self._schedule()

    def _authorized(self, client, sid):
        track = self._tracks.get(sid)
        return (client.managed_events and not client.delegated and track is not None
                and self._hub.subscribed_session(client, sid) is track.session
                and self._authorize(client, track.session))

    def delivery_current(self, client, payload):
        if self._closed:
            return False
        event = unpack_json(payload)
        sid = event['session_id']
        return self._authorized(client, sid) and all(
            event[key] == value for key, value in {**self._tracks[sid].target, **self._identity}.items())

    def forget(self, session):
        track = self._tracks.get(session.id)
        if track is not None and track.session is session:
            self._tracks.pop(session.id)

    def _schedule(self):
        if self._scheduled is None and not self._closed:
            self._scheduled = self._loop.call_later(COALESCE_SECONDS, self._tick)

    def _resync_frame(self, track):
        return pack_json(OP_RUNTIME_EVENT, {**track.target, **self._cursor(track), 'resync': True})

    def _commit(self, track):
        if track.pending is None:
            return
        public, receipt = track.pending, track.receipt
        track.pending = track.receipt = None
        if public == track.status and receipt is None:
            return
        track.status = public
        track.revision += 1
        event = {**public, **self._cursor(track)}
        if receipt is not None:
            event['receipt'] = receipt
        frame = pack_json(OP_RUNTIME_EVENT, event)
        track.recent.append((track.revision, frame if len(frame) <= MAX_FRAME_BYTES else self._resync_frame(track)))

    def _deliver(self, client, sid):
        if not self._authorized(client, sid):
            client.managed_after.pop(sid, None)
            return False
        track = self._tracks[sid]
        previous = client.managed_after[sid]
        if previous == track.revision:
            return False
        if client.remote_paused or self._queue_reserved(client):
            return True
        if previous is None or not track.recent or previous < track.recent[0][0] - 1:
            self._hub._send(client, self._resync_frame(track))
            client.managed_after[sid] = track.revision
            return False
        # No second unbounded client queue. Use available slots in the existing
        # FIFO and resume against the retained ring at the next bounded tick.
        for revision, frame in track.recent:
            if revision > previous:
                if self._queue_reserved(client):
                    return True
                self._hub._send(client, frame)
                client.managed_after[sid] = revision
        return False

    @staticmethod
    def _queue_reserved(client):
        # Leave capacity for non-event frames while replaying retained revisions.
        limit = client.queue.maxsize
        return bool(limit and client.queue.qsize() >= max(1, limit // 2))

    def _tick(self):
        self._scheduled = None
        for sid, track in tuple(self._tracks.items()):
            if self._hub.sessions.get(sid) is not track.session:
                self._tracks.pop(sid, None)
            else:
                self._commit(track)
        backlog = False
        for client in tuple(self._hub.clients.values()):
            for sid in tuple(client.managed_after):
                backlog = self._deliver(client, sid) or backlog
        if backlog:
            self._schedule()

    def close(self):
        self._closed = True
        if self._scheduled is not None:
            self._scheduled.cancel()
            self._scheduled = None
        self._tracks.clear()
