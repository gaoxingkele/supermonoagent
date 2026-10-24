"""Authenticated-session RPC adapter; contains no sockets or terminal operations."""
from __future__ import annotations

import base64
import binascii
import threading
import time

from .file_transfer import TransferStore


class FileTransferRPC:
    def __init__(self, root, session_allowed, *, clock=time.monotonic, max_idle=300):
        if not isinstance(max_idle, (int, float)) or not 0 < max_idle <= 3600:
            raise ValueError('Invalid upload idle timeout')
        self._store = TransferStore(root)
        self._session_allowed = session_allowed
        self._bindings: dict[str, tuple[str, str]] = {}
        self._lock = threading.RLock()
        self._clock, self._max_idle = clock, max_idle
        self._touched = {}

    @staticmethod
    def _error(request, code):
        messages = {
            'invalid_request': 'Invalid file transfer request.',
            'session_unavailable': 'Target session is unavailable or unauthorized.',
            'unknown_transfer': 'File transfer is unavailable for this session.',
            'invalid_transfer': 'File transfer data or metadata was rejected.',
            'unavailable': 'File receiver is temporarily unavailable.',
        }
        return {'request': request, 'ok': False, 'error_code': code, 'error': messages[code]}

    def _discard(self, owner, transfer_id):
        if not isinstance(transfer_id, str):
            return
        if self._bindings.get(transfer_id, (None,))[0] != owner:
            return
        self._bindings.pop(transfer_id, None)
        self._touched.pop(transfer_id, None)
        try:
            self._store.cancel(owner, transfer_id)
        except (OSError, ValueError):
            # Store also removes finished, corrupt, or unwritable transfers.
            pass

    def handle(self, owner: str, body: dict) -> dict:
        request = body.get('request', '') if isinstance(body, dict) else ''
        if not isinstance(request, str) or len(request) > 64:
            return self._error('', 'invalid_request')
        if (not isinstance(body, dict) or len(body) > 10
                or not isinstance(owner, str) or not owner or len(owner) > 256):
            return self._error(request, 'invalid_request')
        action, session, transfer_id = body.get('action'), body.get('session'), body.get('id')
        if (action not in ('begin', 'chunk', 'finish', 'cancel')
                or not isinstance(session, str) or not session or len(session) > 256
                or (action != 'begin' and (not isinstance(transfer_id, str) or len(transfer_id) != 48))):
            return self._error(request, 'invalid_request')
        with self._lock:
            self.expire()
            if not self._session_allowed(owner, session):
                if isinstance(transfer_id, str):
                    self._discard(owner, transfer_id)
                return self._error(request, 'session_unavailable')
            if action != 'begin' and self._bindings.get(transfer_id) != (owner, session):
                self._discard(owner, transfer_id)
                return self._error(request, 'unknown_transfer')
            try:
                if action == 'begin':
                    name = body.get('name')
                    sha256 = body.get('sha256')
                    if (not isinstance(name, str) or len(name) > 200
                            or not isinstance(sha256, str) or len(sha256) != 64):
                        return self._error(request, 'invalid_request')
                    transfer_id = self._store.begin(owner, session, name, body.get('size'), sha256)
                    self._bindings[transfer_id] = (owner, session)
                    self._touched[transfer_id] = self._clock()
                    result = {'id': transfer_id, 'received': 0}
                elif action == 'chunk':
                    data = body.get('data')
                    if not isinstance(data, str) or len(data) > 131072:
                        return self._error(request, 'invalid_request')
                    decoded = base64.b64decode(data, validate=True)
                    received = self._store.append(owner, transfer_id, body.get('offset'), decoded)
                    self._touched[transfer_id] = self._clock()
                    result = {'id': transfer_id, 'received': received}
                elif action == 'finish':
                    result = {'id': transfer_id, 'path': self._store.finish(owner, transfer_id,
                        can_publish=lambda: self._session_allowed(owner, session))}
                    self._bindings.pop(transfer_id)
                    self._touched.pop(transfer_id, None)
                else:
                    self._store.cancel(owner, transfer_id)
                    self._bindings.pop(transfer_id)
                    self._touched.pop(transfer_id, None)
                    result = {'id': transfer_id}
            except (ValueError, binascii.Error):
                if action == 'finish':
                    self._discard(owner, transfer_id)
                return self._error(request, 'invalid_transfer')
            except OSError:
                self._discard(owner, transfer_id)
                return self._error(request, 'unavailable')
            return {'request': request, 'ok': True, **result}

    def cancel_owner(self, owner):
        with self._lock:
            for key, binding in self._bindings.items():
                if binding[0] == owner:
                    self._touched.pop(key, None)
            self._bindings = {key: binding for key, binding in self._bindings.items() if binding[0] != owner}
            return self._store.cancel_owner(owner)

    def expire(self):
        """Release idle uploads, including a begin whose acknowledgement was lost."""
        with self._lock:
            now = self._clock()
            expired = [key for key, touched in self._touched.items() if now - touched >= self._max_idle]
            for key in expired:
                binding = self._bindings.get(key)
                if binding is not None:
                    self._discard(binding[0], key)
            try:
                self._store.cleanup_published()
            except (OSError, ValueError):
                pass
            return len(expired)
