"""Resumable one-destination mirroring of already committed local journals.

Credentials are private constructor input, never part of a stored destination,
event, manifest, batch or diagnostic. Network failure cannot affect recording.
"""
from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass, field
import hashlib
import secrets
import time

from websockets.asyncio.client import connect

from .conversation_contract import (CONTENT_CHUNK, MAX_ENVELOPE, PROTOCOL,
    JournalError, canonical_json, checked_id, decode_json, normalize_destination,
    validate_receipt)
from .conversation_service import enrollment_proof, source_proof


@dataclass(frozen=True)
class SourceCredentials:
    token: str = field(repr=False)
    host_secret: str = field(repr=False)
    enrollment_key: str | None = field(default=None, repr=False)


class MirrorClient:
    """One immutable normalized relay authority and one source identity.

    ``sync_once`` retries the store's persisted pending envelope. It returns False
    only when there is nothing to send; transport errors are fixed JournalErrors.
    ``run`` continues offline retries until its caller sets an asyncio.Event.
    """
    def __init__(self, store, destination, credentials: SourceCredentials, *,
                 local_owner_scope=None, request_timeout=20, clock=time.time):
        self.store = store
        self._destination = normalize_destination(destination)
        if self._destination['service_path'] != '/journals/ws':
            raise JournalError('invalid_destination')
        if (not isinstance(credentials, SourceCredentials)
                or not isinstance(credentials.token, str) or not 1 <= len(credentials.token) <= 8192
                or not isinstance(credentials.host_secret, str) or not 16 <= len(credentials.host_secret) <= 8192
                or credentials.enrollment_key is not None and (
                    not isinstance(credentials.enrollment_key, str) or not 32 <= len(credentials.enrollment_key) <= 8192)):
            raise JournalError('invalid_credentials')
        if type(request_timeout) not in (int, float) or not 0 < request_timeout <= 120:
            raise JournalError()
        self._credentials = credentials
        self.local_owner_scope = checked_id(local_owner_scope or self._destination['owner_scope'])
        self._timeout = request_timeout
        self._clock = clock
        self._connect = connect
        self._sync_lock = asyncio.Lock()

    def _hello(self):
        destination, credentials = self._destination, self._credentials
        stamp, nonce = int(self._clock()), secrets.token_hex(16)
        room, host = destination['external_room'], destination['host_installation_id']
        hello = dict(op='hello', protocol=PROTOCOL, role='source', token=credentials.token,
            room=room, host_installation_id=host, host_secret=credentials.host_secret,
            timestamp=stamp, nonce=nonce,
            proof=source_proof(credentials.host_secret, room, host, stamp, nonce))
        if credentials.enrollment_key is not None:
            hello['enrollment_proof'] = enrollment_proof(credentials.enrollment_key,
                room, host, credentials.host_secret, stamp, nonce)
        return hello

    async def _request(self, ws, action, params):
        request_id = secrets.token_hex(16)
        raw = canonical_json(dict(op='request', request_id=request_id, action=action, params=params))
        if len(raw) > MAX_ENVELOPE:
            raise JournalError('frame_too_large')
        await asyncio.wait_for(ws.send(raw.decode()), self._timeout)
        response = decode_json(await asyncio.wait_for(ws.recv(), self._timeout))
        if (response.get('op') != 'response' or response.get('request_id') != request_id
                or response.get('ok') is not True or not isinstance(response.get('result'), dict)):
            raise JournalError('mirror_rejected')
        return response['result']

    async def sync_once(self, journal_id):
        checked_id(journal_id)
        async with self._sync_lock:
            await asyncio.to_thread(self.store.pin_mirror, journal_id, dict(self._destination))
            batch = await asyncio.to_thread(self.store.mirror_batch, journal_id)
            if batch is None:
                return False
            expected = {key: self._destination[key] for key in ('destination_id', 'owner_scope',
                'external_room', 'host_installation_id', 'source_epoch')}
            if batch.get('journal_id') != journal_id or any(batch.get(key) != value for key, value in expected.items()):
                raise JournalError('invalid_batch')
            journal = await asyncio.to_thread(self.store.status, self.local_owner_scope, journal_id)
            journal = dict(journal, owner_scope=self._destination['owner_scope'],
                room=self._destination['external_room'])
            # Source outbox details are local bookkeeping, not central metadata.
            journal['mirror'] = {}
            authority = self._destination['authority']
            url = ('wss' + authority[5:] if authority.startswith('https:') else 'ws' + authority[4:]) + self._destination['service_path']
            try:
                async with self._connect(url, max_size=MAX_ENVELOPE, max_queue=8,
                        compression=None, open_timeout=self._timeout, close_timeout=3) as ws:
                    await asyncio.wait_for(ws.send(canonical_json(self._hello()).decode()), self._timeout)
                    hello = decode_json(await asyncio.wait_for(ws.recv(), self._timeout))
                    if (hello.get('op') != 'hello' or hello.get('protocol') != PROTOCOL
                            or hello.get('ok') is not True or hello.get('role') != 'source'):
                        raise JournalError('mirror_rejected')
                    for content in batch['contents']:
                        body = await asyncio.to_thread(self.store.content_bytes, journal_id, content['content_id'])
                        if not isinstance(body, bytes) or len(body) != content['bytes'] or hashlib.sha256(body).hexdigest() != content['sha256']:
                            raise JournalError('content_hash_mismatch')
                        for offset in range(0, max(1, len(body)), CONTENT_CHUNK):
                            result = await self._request(ws, 'content.stage', dict(journal_id=journal_id,
                                content_id=content['content_id'], offset=offset, total_bytes=len(body),
                                sha256=content['sha256'], data_base64=base64.b64encode(body[offset:offset + CONTENT_CHUNK]).decode()))
                            if (result.get('content_id') != content['content_id']
                                    or type(result.get('received_bytes')) is not int
                                    or result['received_bytes'] < offset + len(body[offset:offset + CONTENT_CHUNK])
                                    or result['received_bytes'] > len(body)):
                                raise JournalError('invalid_stage_response')
                    result = await self._request(ws, 'append_batch', dict(batch=batch, journal=journal))
                    receipt = result.get('receipt')
                    validate_receipt(batch, receipt)
                    await asyncio.to_thread(self.store.accept_receipt, journal_id, receipt)
                    return True
            except JournalError:
                raise
            except asyncio.CancelledError:
                raise
            except Exception:
                raise JournalError('mirror_unavailable') from None

    async def run(self, journal_id, stop: asyncio.Event, *, retry_delay=2, max_retry_delay=30):
        if not 0 < retry_delay <= max_retry_delay <= 300:
            raise JournalError()
        delay = retry_delay
        while not stop.is_set():
            try:
                sent = await self.sync_once(journal_id)
                delay = retry_delay
                if sent:
                    continue
            except JournalError:
                delay = min(max_retry_delay, delay * 2)
            try:
                await asyncio.wait_for(stop.wait(), delay)
            except asyncio.TimeoutError:
                pass
