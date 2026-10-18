"""Length-prefixed frames. The relay forwards them without decoding."""

from __future__ import annotations

import json
import struct

OP_HELLO = 1
OP_HELLO_OK = 2
OP_OUTPUT = 3
OP_INPUT = 4
OP_RESIZE = 5
OP_OPEN = 6
OP_SESSIONS = 7
OP_SNAPSHOT = 8
OP_CLOSE = 9
OP_PING = 10
OP_PONG = 11
OP_WINDOW = 12
OP_SELECT = 13
OP_FOCUS = 14
OP_GROUP = 15

_HEADER = struct.Struct("<I")
MAX_FRAME = 256 * 1024


class ProtocolError(ValueError):
    pass


def pack(op: int, payload: bytes = b"") -> bytes:
    body = bytes((op,)) + payload
    if len(body) > MAX_FRAME:
        raise ProtocolError(f"frame is {len(body)} bytes")
    return _HEADER.pack(len(body)) + body


def fit_payload(payload: bytes) -> bytes:
    """Keep the newest bytes that still fit in one frame after the opcode."""
    limit = MAX_FRAME - 1
    if len(payload) <= limit:
        return payload
    payload = payload[-limit:]
    while payload and payload[0] & 0xC0 == 0x80:
        payload = payload[1:]
    return payload


def pack_feed(op: int, session_id: str, data: bytes) -> bytes:
    """Terminal bytes for one open session. The id lets a relay client keep every window."""
    prefix = session_id.encode("utf-8") + b"\0"
    room = MAX_FRAME - 1 - len(prefix)
    if room < 1:
        raise ProtocolError("session id too long")
    if len(data) > room:
        data = data[-room:]
        while data and data[0] & 0xC0 == 0x80:
            data = data[1:]
    return pack(op, prefix + data)


def pack_json(op: int, obj: dict) -> bytes:
    return pack(op, json.dumps(obj, separators=(",", ":")).encode("utf-8"))


def unpack(frame: bytes) -> tuple[int, bytes]:
    if len(frame) < 5:
        raise ProtocolError("short frame")
    (length,) = _HEADER.unpack_from(frame, 0)
    if length < 1 or length > MAX_FRAME or len(frame) != 4 + length:
        raise ProtocolError("bad frame length")
    return frame[4], frame[5:]


def unpack_json(payload: bytes) -> dict:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("json payload") from exc
    if not isinstance(value, dict):
        raise ProtocolError("json object required")
    return value
