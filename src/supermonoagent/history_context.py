"""Opt-in, bounded history context. Legacy VT frames remain byte-compatible."""
from __future__ import annotations

import json
import struct
import copy

from . import protocol
from .ansi_state import SgrState, bounded_snapshot

METADATA_LIMIT = 4096
_LENGTH = struct.Struct('<H')
_FIELDS = frozenset({'v', 'kind', 'cols', 'rows', 'epoch', 'partial', 'reason'})
SAFE_INTEGER = 9007199254740991


def valid_stream_id(value):
    return isinstance(value, str) and len(value) == 24 and all(char in '0123456789abcdef' for char in value)


class ReplayRenditionState(SgrState):
    """Opt-in modes, not a saved screen/cursor or a ConPTY resize fence."""
    def __init__(self, *, emit_modes=True):
        super().__init__()
        self.emit_modes = emit_modes
        self.alternate = False
        self.origin = False
        self.wraparound = True
        self.region = None
        self.extra_modes = {}

    def copy(self):
        return copy.deepcopy(self)

    def _replay_reset(self, soft):
        if not soft:
            self.alternate = False
        self.origin = False
        self.wraparound = True
        self.region = None
        self.extra_modes.clear()

    def _replay_csi(self, final, raw):
        if final == ord('p') and raw == b'!':
            self._replay_reset(True)
        elif final in (ord('h'), ord('l')) and raw.startswith(b'?'):
            parts = raw[1:].split(b';')[:32]
            if not all(not part or part.isdigit() for part in parts):
                return
            enabled = final == ord('h')
            for part in parts:
                mode = int(part or b'0')
                if mode in (47, 1047, 1049):
                    self.alternate = enabled
                elif mode == 6:
                    self.origin = enabled
                elif mode == 7:
                    self.wraparound = enabled
                elif mode in (12, 1000, 1002, 1003, 1004, 1006, 1015, 2026):
                    self.extra_modes[mode] = enabled
        elif final == ord('r'):
            parts = raw.split(b';')
            if len(parts) > 2 or not all(not part or part.isdigit() for part in parts):
                return
            top = int(parts[0] or b'1') or 1
            bottom = int(parts[1] or b'0') if len(parts) == 2 else 0
            if top <= 200 and (bottom == 0 or top < bottom <= 200):
                self.region = None if top == 1 and not bottom else (top, bottom)

    def prefix(self):
        if not self.emit_modes:
            return super().prefix()
        modes = b'\x1b[?1049h' if self.alternate else b''
        if self.region is not None:
            top, bottom = self.region
            modes += ('\x1b[' + str(top) + ';' + (str(bottom) if bottom else '') + 'r').encode('ascii')
        if self.origin:
            modes += b'\x1b[?6h'
        if not self.wraparound:
            modes += b'\x1b[?7l'
        for mode, enabled in sorted(self.extra_modes.items()):
            if enabled or mode == 12:
                modes += ('\x1b[?' + str(mode) + ('h' if enabled else 'l')).encode('ascii')
        return modes + super().prefix()


def validate_metadata(value):
    if not isinstance(value, dict):
        raise protocol.ProtocolError('history metadata fields')
    version = value.get('v')
    fields = _FIELDS | {'end_position', 'stream_id'} if type(version) is int and version == 2 else _FIELDS
    if set(value) != fields:
        raise protocol.ProtocolError('history metadata fields')
    if (type(version) is not int or version not in (1, 2)
            or value['kind'] not in (('snapshot', 'output', 'resume') if version == 2 else ('snapshot', 'output'))):
        raise protocol.ProtocolError('history metadata version/kind')
    if version == 2 and (type(value['end_position']) is not int or not 0 <= value['end_position'] <= SAFE_INTEGER):
        raise protocol.ProtocolError('history metadata position')
    if version == 2 and not valid_stream_id(value['stream_id']):
        raise protocol.ProtocolError('history metadata stream')
    if any(type(value[key]) is not int for key in ('cols', 'rows', 'epoch')):
        raise protocol.ProtocolError('history metadata integers')
    if not (2 <= value['cols'] <= 500 and 1 <= value['rows'] <= 200 and 0 <= value['epoch'] <= 9007199254740991):
        raise protocol.ProtocolError('history metadata bounds')
    reason = value['reason']
    if (type(value['partial']) is not bool or not isinstance(reason, str) or len(reason) > 64
            or any(not ('a' <= char <= 'z' or char == '_') for char in reason)
            or value['partial'] != bool(reason)):
        raise protocol.ProtocolError('history metadata completeness')
    return dict(value)


def _session_prefix(session):
    if not isinstance(session, str) or not session or '\0' in session:
        raise protocol.ProtocolError('history session')
    encoded = session.encode('utf-8')
    if len(encoded) > 256 or any(ord(char) < 32 or ord(char) == 127 for char in session):
        raise protocol.ProtocolError('history session bounds')
    return encoded + b'\0'


def pack_history_context(session, metadata, data):
    """One queue item contains both context and bytes; never truncate live VT."""
    metadata = validate_metadata(metadata)
    prefix = _session_prefix(session)
    raw = json.dumps(metadata, separators=(',', ':'), sort_keys=True).encode('utf-8')
    if len(raw) > METADATA_LIMIT or not isinstance(data, bytes):
        raise protocol.ProtocolError('history payload')
    room = protocol.MAX_FRAME - 1 - len(prefix) - _LENGTH.size - len(raw)
    if room < 0:
        raise protocol.ProtocolError('history frame budget')
    if len(data) > room:
        if metadata['kind'] != 'snapshot':
            raise protocol.ProtocolError('live history frame overflow')
        metadata['partial'] = True
        metadata['reason'] = metadata['reason'] or 'history_ring_truncated'
        raw = json.dumps(metadata, separators=(',', ':'), sort_keys=True).encode('utf-8')
        room = protocol.MAX_FRAME - 1 - len(prefix) - _LENGTH.size - len(raw)
        if room < 0:
            raise protocol.ProtocolError('history frame budget')
        data = bounded_snapshot(data, room, ReplayRenditionState())
    return protocol.pack(protocol.OP_HISTORY_CONTEXT, prefix + _LENGTH.pack(len(raw)) + raw + data)


def unpack_history_context(payload):
    if not isinstance(payload, bytes):
        raise protocol.ProtocolError('history payload type')
    session, separator, body = payload.partition(b'\0')
    if not separator or len(body) < _LENGTH.size:
        raise protocol.ProtocolError('history header')
    try:
        session = session.decode('utf-8')
        _session_prefix(session)
        length = _LENGTH.unpack_from(body)[0]
        if not 0 < length <= METADATA_LIMIT or _LENGTH.size + length > len(body):
            raise protocol.ProtocolError('history metadata length')
        metadata = validate_metadata(json.loads(body[_LENGTH.size:_LENGTH.size + length].decode('utf-8')))
    except (UnicodeError, ValueError, TypeError) as error:
        raise protocol.ProtocolError('history metadata') from error
    return session, metadata, body[_LENGTH.size + length:]
