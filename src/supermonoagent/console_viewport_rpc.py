"""Explicit owner-only console preview; no automatic stream or VT injection.

The caller's session selects a cached native identity, never a browser PID. The
existing daemon footer-reader job set is the single four-reader budget. Raw
uint16 cells are transport data, not strings to write into a terminal.
Captured text never grants authority to send input or treat a preview as VT state.
"""
from __future__ import annotations

import asyncio
from array import array
import base64
from dataclasses import dataclass, field
import json
import re
import sys
import threading
import time

from . import console_snapshot
from .protocol import MAX_FRAME, OP_ASSISTANT_CONTEXT, pack_json

ACTION = 'console.viewport'
READ_TIMEOUT = 2.5
MAX_REQUEST = 2048
_RAW_KEYS = {'kind', 'version', 'pid', 'parent', 'created', 'buffer_size', 'viewport',
             'width', 'height', 'cursor_buffer', 'cursor', 'cursor_visible', 'cursor_in_view',
             'cursor_size', 'default_attributes', 'palette', 'code_units', 'attributes', 'stable_samples'}
_WIRE_KEYS = (_RAW_KEYS - {'code_units', 'attributes'}) | {
    'code_units_le', 'attributes_le', 'encoding', 'stream_checkpoint', 'captured_at'}


class ViewportRPCError(ValueError):
    """Fixed safe error code only."""


def _windows_supported():
    return sys.platform == 'win32'


def _words(rows):
    value = array('H', (unit for row in rows for unit in row))
    if value.itemsize != 2:
        raise ViewportRPCError('unsupported')
    if sys.byteorder != 'little':
        value.byteswap()
    return base64.b64encode(value.tobytes()).decode('ascii')


def encode_viewport(snapshot):
    if not console_snapshot.validate_console_viewport(snapshot):
        raise ViewportRPCError('invalid_payload')
    # Copy only small metadata; no mutable raw matrices escape this boundary.
    metadata = json.loads(json.dumps({key: value for key, value in snapshot.items()
                                      if key not in {'code_units', 'attributes'}}, allow_nan=False))
    # Windows FILETIME exceeds JavaScript's exact integer range. Identity stays
    # an integer in the native reader; preview metadata travels as decimal text.
    metadata['created'] = str(snapshot['created'])
    result = {**metadata, 'kind': 'console_viewport_preview', 'stream_checkpoint': False,
              'captured_at': int(time.time()*1000), 'encoding': 'base64-u16le',
              'code_units_le': _words(snapshot['code_units']),
              'attributes_le': _words(snapshot['attributes'])}
    if len(json.dumps(result, separators=(',', ':')).encode()) > MAX_FRAME - 2048:
        raise ViewportRPCError('too_large')
    return result


def decode_viewport(value):
    """Decode words while retaining preview-only markers; never return a VT baseline."""
    try:
        if (not isinstance(value, dict) or set(value) != _WIRE_KEYS or value['encoding'] != 'base64-u16le'
                or value['kind'] != 'console_viewport_preview' or value['stream_checkpoint'] is not False
                or type(value['captured_at']) is not int or not 0 < value['captured_at'] <= 0x1FFFFFFFFFFFFF):
            raise ValueError()
        created = value['created']
        if (not isinstance(created, str) or not re.fullmatch(r'[1-9][0-9]{0,19}', created)
                or int(created) > 0xFFFFFFFFFFFFFFFF):
            raise ValueError()
        width, height = value['width'], value['height']
        if (type(width) is not int or type(height) is not int or not 1 <= width <= console_snapshot.MAX_COLS
                or not 1 <= height <= console_snapshot.MAX_ROWS
                or width*height > console_snapshot.MAX_VIEWPORT_CELLS):
            raise ValueError()
        expected = width * height * 2
        matrices = {}
        for wire, raw_name in [('code_units_le', 'code_units'), ('attributes_le', 'attributes')]:
            text = value[wire]
            if not isinstance(text, str) or len(text) != 4 * ((expected+2)//3):
                raise ValueError()
            data = base64.b64decode(text, validate=True)
            if len(data) != expected or base64.b64encode(data).decode('ascii') != text:
                raise ValueError()
            words = array('H')
            if words.itemsize != 2:
                raise ValueError()
            words.frombytes(data)
            if sys.byteorder != 'little':
                words.byteswap()
            matrices[raw_name] = [list(words[offset:offset+width]) for offset in range(0, width*height, width)]
        result = {key: item for key, item in value.items() if key in _RAW_KEYS}
        result['kind'] = 'console_viewport'
        result['created'] = int(created)
        result.update(matrices)
        if not console_snapshot.validate_console_viewport(result):
            raise ValueError()
        return {**result, 'created': created, 'kind': 'console_viewport_preview', 'stream_checkpoint': False,
                'captured_at': value['captured_at']}
    except (ValueError, TypeError, KeyError, OverflowError, UnicodeError, RecursionError):
        raise ViewportRPCError('invalid_payload') from None


def _request(payload):
    def unique(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError()
            result[key] = item
        return result
    try:
        if not isinstance(payload, bytes) or len(payload) > MAX_REQUEST:
            raise ValueError()
        value = json.loads(payload, object_pairs_hook=unique)
        if not isinstance(value, dict) or set(value) != {'request', 'action', 'session'} or value['action'] != ACTION:
            raise ValueError()
        for key, limit in [('request', 64), ('session', 256)]:
            if not isinstance(value[key], str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,'+str(limit)+'}', value[key]):
                raise ValueError()
        return value
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        raise ViewportRPCError('invalid_request') from None


@dataclass(frozen=True)
class _Target:
    session: object
    pty: object
    pid: int
    identity: tuple
    dimensions: tuple
    token: str = field(repr=False)
    channel: str
    multiplex: bool
    window: object


def _permitted(daemon, client, session):
    try:
        return (not daemon._footer_closed and daemon.client_authorized(client)
                and not client.delegated and client.token == daemon.token
                and (client.authorize is None or client.authorize())
                and session is not None and daemon.hub.sessions.get(session.id) is session
                and daemon.hub._channel_ok(client, session)
                and (session.id in client.subscriptions if client.multiplex else daemon.hub.view(client) is session))
    except Exception:
        return False


def _current(daemon, client, target):
    session = target.session
    return (_permitted(daemon, client, session) and session.pty is target.pty
            and (session.cols, session.rows) == target.dimensions
            and client.token == target.token and client.channel == target.channel
            and client.multiplex == target.multiplex
            and daemon.hub.windows.get(session.window_id) is target.window
            and getattr(target.pty, '_footer_pid', None) == target.pid
            and getattr(target.pty, '_footer_identity', None) == target.identity)


def _capture(daemon, client, target, cancelled, request_task):
    # Admission can precede actual execution in the shared executor. A closed,
    # revoked or cancelled request must not start a late OS console read.
    # Task.cancel() records its request before the event loop delivers
    # CancelledError and sets our Event. Observe that read-only flag too, so a
    # queued worker cannot slip through during that delivery window. This does
    # not interrupt an OS read that already started; its reader slot stays held.
    if cancelled.is_set() or request_task.cancelling():
        # The owner has already selected cancellation/timeout, so no payload is
        # consumed. Finish normally rather than log a detached shield exception.
        return None
    if not _current(daemon, client, target):
        raise ViewportRPCError('target_changed')
    snapshot = console_snapshot.read_console_viewport(target.pid, target.identity[1], target.identity[0], timeout=2)
    if snapshot is None:
        raise ViewportRPCError('unavailable')
    if snapshot.get('pid') != target.pid or (snapshot.get('parent'), snapshot.get('created')) != target.identity:
        raise ViewportRPCError('target_changed')
    return encode_viewport(snapshot)


async def handle_viewport_request(daemon, client, payload):
    """Return ordinary OP22 frames; only this explicit call starts a read."""
    request = sid = ''
    def reply(code=None, viewport=None):
        body = {'request': request, 'action': ACTION, 'session': sid, 'ok': code is None}
        if code is not None:
            body['error_code'] = code
        else:
            body['viewport'] = viewport
        return [pack_json(OP_ASSISTANT_CONTEXT, body)], None
    try:
        body = _request(payload)
        request, sid = body['request'], body['session']
    except ViewportRPCError:
        return reply('invalid_request')
    session = daemon.hub.sessions.get(sid)
    if not _permitted(daemon, client, session):
        return reply('unauthorized')
    pty = session.pty
    pid, identity = getattr(pty, '_footer_pid', None), getattr(pty, '_footer_identity', None)
    if (not _windows_supported() or type(pid) is not int or not 0 < pid <= 0xFFFFFFFF
            or not isinstance(identity, tuple) or len(identity) != 2
            or any(type(item) is not int or item <= 0 for item in identity)
            or identity[0] > 0xFFFFFFFF or identity[1] > 0xFFFFFFFFFFFFFFFF):
        return reply('unsupported')
    target = _Target(session, pty, pid, identity, (session.cols, session.rows),
                     client.token, client.channel, client.multiplex, daemon.hub.windows.get(session.window_id))
    # Shared with footer recovery, including scheduler-reserved starting slots.
    # No await may occur between admission and registering the actual job.
    if len(daemon._footer_jobs) + len(daemon._footer_starting) >= 4:
        return reply('busy')
    cancelled = threading.Event()
    job = asyncio.create_task(asyncio.to_thread(_capture, daemon, client, target, cancelled,
                                               asyncio.current_task()))
    daemon._footer_jobs.add(job)
    def finished(task):
        daemon._footer_jobs.discard(task)
        if not task.cancelled():
            task.exception()
        daemon._kick_footer_recovery()
    job.add_done_callback(finished)
    error_code = None
    try:
        viewport = await asyncio.wait_for(asyncio.shield(job), READ_TIMEOUT)
    except asyncio.CancelledError:
        cancelled.set()
        raise
    except asyncio.TimeoutError:
        cancelled.set()
        error_code = 'timeout'
    except ViewportRPCError as error:
        code = str(error)
        if code not in {'unsupported', 'invalid_payload', 'too_large', 'target_changed', 'unavailable'}:
            code = 'unavailable'
        error_code = code
    except Exception:
        error_code = 'unavailable'
    try:
        if not daemon.client_authorized(client):
            return [], 4001
    except Exception:
        return reply('unauthorized')
    if not _current(daemon, client, target):
        return reply('target_changed')
    if error_code is not None:
        return reply(error_code)
    if viewport is None:
        return reply('target_changed')
    return reply(viewport=viewport)
