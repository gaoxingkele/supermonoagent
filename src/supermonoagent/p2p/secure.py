"""TLS-PSK contexts and peer frames. Possession of the network key is the only credential."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import ssl
import struct

CIPHERS = 'ECDHE-PSK-CHACHA20-POLY1305:PSK-AES256-GCM-SHA384'
JOIN_PREFIX = 'smap2p1:'

T_JSON, T_CHUNK, T_ACK = 1, 2, 3
_HEADER = struct.Struct('<IB')
CHUNK_HEADER = struct.Struct('<IQI32s')   # file index, offset, length, sha256
ACK = struct.Struct('<IQB')               # file index, offset, status
MAX_JSON = 32 * 1024 * 1024
MAX_CHUNK = 8 * 1024 * 1024


class FrameError(ValueError):
    pass


def new_key() -> bytes:
    return os.urandom(32)


def derive(key: bytes, label: str) -> bytes:
    return hmac.new(key, b'sma-p2p/' + label.encode(), hashlib.sha256).digest()


def net_id(key: bytes) -> str:
    return hashlib.sha256(b'sma-p2p/net/' + key).hexdigest()[:32]


def join_code(key: bytes) -> str:
    return JOIN_PREFIX + base64.urlsafe_b64encode(key).decode().rstrip('=')


def parse_join_code(code: str) -> bytes:
    code = (code or '').strip()
    if not code.startswith(JOIN_PREFIX):
        raise ValueError('加入码格式不对')
    raw = code[len(JOIN_PREFIX):]
    try:
        key = base64.urlsafe_b64decode(raw + '=' * (-len(raw) % 4))
    except (ValueError, TypeError) as exc:
        raise ValueError('加入码格式不对') from exc
    if len(key) != 32:
        raise ValueError('加入码长度不对')
    return key


def sign(key: bytes, data: dict) -> str:
    body = json.dumps(data, sort_keys=True, separators=(',', ':')).encode()
    return hmac.new(derive(key, 'signal'), body, hashlib.sha256).hexdigest()


def verify(key: bytes, data: dict, mac: str) -> bool:
    return isinstance(mac, str) and hmac.compare_digest(sign(key, data), mac)


def _base(server: bool) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER if server else ssl.PROTOCOL_TLS_CLIENT)
    if not server:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    # Python exposes PSK callbacks for TLS 1.2 only.
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.maximum_version = ssl.TLSVersion.TLSv1_2
    context.set_ciphers(CIPHERS)
    return context


def server_context(key: bytes) -> ssl.SSLContext:
    psk = derive(key, 'psk')
    context = _base(True)
    context.set_psk_server_callback(lambda identity: psk if identity else b'', identity_hint='sma-p2p')
    return context


def client_context(key: bytes, peer_id: str) -> ssl.SSLContext:
    psk = derive(key, 'psk')
    context = _base(False)
    context.set_psk_client_callback(lambda hint: (peer_id, psk))
    return context


def pack_json(obj: dict) -> bytes:
    body = json.dumps(obj, ensure_ascii=False, separators=(',', ':')).encode()
    return _HEADER.pack(len(body), T_JSON) + body


def pack_ack(file_index: int, offset: int, status: int) -> bytes:
    body = ACK.pack(file_index, offset, status)
    return _HEADER.pack(len(body), T_ACK) + body


def chunk_header(file_index: int, offset: int, data_len: int, digest: bytes) -> bytes:
    return _HEADER.pack(CHUNK_HEADER.size + data_len, T_CHUNK) + CHUNK_HEADER.pack(file_index, offset, data_len, digest)


class FrameReceiver(asyncio.BufferedProtocol):
    """Zero-copy frame reader for bulk data links.

    StreamReader appends into one bytearray and slices from its front; with 4 MiB frames that
    re-copies the buffer on every growth. Here TLS decrypts straight into a buffer sized for the
    frame being read. Frames queue up to `depth`; beyond that the transport pauses reading.
    """

    def __init__(self, depth: int = 4):
        self.frames: asyncio.Queue = asyncio.Queue()
        self.depth = depth
        self.transport = None
        self._paused = False
        self._header = bytearray(_HEADER.size)
        self._target = self._header
        self._filled = 0
        self._kind = 0

    @classmethod
    def attach(cls, writer: asyncio.StreamWriter, reader: asyncio.StreamReader, depth: int = 4) -> 'FrameReceiver':
        """Take over a connection that a StreamReader has been reading, keeping buffered bytes."""
        receiver = cls(depth)
        transport = writer.transport
        receiver.transport = transport
        leftover = bytes(reader._buffer)
        reader._buffer.clear()
        transport.set_protocol(receiver)
        if leftover:
            receiver._feed(leftover)
        if reader._eof:
            receiver.frames.put_nowait(None)
        try:
            transport.resume_reading()
        except (AttributeError, RuntimeError):
            pass
        return receiver

    def connection_made(self, transport) -> None:
        self.transport = transport

    def get_buffer(self, sizehint: int):
        return memoryview(self._target)[self._filled:]

    def buffer_updated(self, nbytes: int) -> None:
        self._filled += nbytes
        if self._filled == len(self._target):
            self._advance()

    def _feed(self, data: bytes) -> None:
        view = memoryview(data)
        while view:
            room = len(self._target) - self._filled
            take = min(room, len(view))
            self._target[self._filled:self._filled + take] = view[:take]
            view = view[take:]
            self.buffer_updated(take)

    def _advance(self) -> None:
        if self._target is self._header:
            length, kind = _HEADER.unpack(self._header)
            limit = MAX_JSON if kind == T_JSON else MAX_CHUNK + CHUNK_HEADER.size if kind == T_CHUNK else ACK.size
            if kind not in (T_JSON, T_CHUNK, T_ACK) or length > limit:
                self.frames.put_nowait(FrameError('bad frame'))
                self.transport.close()
                return
            self._kind = kind
            self._target, self._filled = bytearray(length), 0
            if length == 0:
                self._advance()
            return
        self.frames.put_nowait((self._kind, self._target))
        self._target, self._filled = self._header, 0
        if self.frames.qsize() >= self.depth and not self._paused:
            self._paused = True
            self.transport.pause_reading()

    async def read(self) -> tuple[int, bytearray]:
        item = await self.frames.get()
        if self._paused and self.frames.qsize() < self.depth:
            self._paused = False
            self.transport.resume_reading()
        if item is None:
            raise asyncio.IncompleteReadError(b'', None)
        if isinstance(item, Exception):
            raise item
        return item

    def eof_received(self):
        self.frames.put_nowait(None)
        return False

    def connection_lost(self, exc) -> None:
        self.frames.put_nowait(None)


async def read_frame(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    head = await reader.readexactly(_HEADER.size)
    length, kind = _HEADER.unpack(head)
    limit = MAX_JSON if kind == T_JSON else MAX_CHUNK + CHUNK_HEADER.size if kind == T_CHUNK else ACK.size
    if kind not in (T_JSON, T_CHUNK, T_ACK) or length > limit:
        raise FrameError('bad frame')
    return kind, await reader.readexactly(length)


async def read_json(reader: asyncio.StreamReader) -> dict:
    kind, payload = await read_frame(reader)
    if kind != T_JSON:
        raise FrameError('json frame expected')
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise FrameError('json object expected')
    return value
