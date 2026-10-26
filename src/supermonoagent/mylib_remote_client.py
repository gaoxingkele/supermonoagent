"""Restricted read-only MCP Streamable HTTP client, not a general MCP client.

Only stateless JSON/SSE responses for protocol 2025-11-25 are supported. No
server-initiated requests, redirects, proxies, session cookies, or model calls.
The deadline covers socket connection, headers and body across initialization
and the tool call. OS DNS resolution may outlive this budget.
"""
from __future__ import annotations

import http.client
import hashlib
import io
import json
import math
import re
import socket
import ssl
import threading
import time
from urllib.parse import urlsplit

ENDPOINT = 'https://117.50.48.22/mylib/mcp'
PROTOCOL = '2025-11-25'
MAX_REQUEST = 128 * 1024
MAX_RESPONSE = 1024 * 1024


class MylibRemoteError(ValueError):
    """Contains only a fixed safe error code, never response or credentials."""


def _json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError()
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))


def _contains(value, secret):
    if isinstance(value, str):
        return secret in value
    if isinstance(value, dict):
        return any(_contains(k, secret) or _contains(v, secret) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return any(_contains(item, secret) for item in value)
    return False


class _Reader(io.RawIOBase):
    def __init__(self, sock, check):
        self.sock, self.check = sock, check

    def readable(self):
        return True

    def readinto(self, buf):
        # Windows shutdown does not reliably interrupt select() inside a timed
        # socket read. Poll the real socket without SocketIO (which becomes
        # permanently unusable after a timeout) to bound cancellation latency.
        while True:
            self.sock.settimeout(min(.1, self.check()))
            try:
                value = self.sock.recv_into(buf)
            except socket.timeout:
                self.check()
                continue
            self.check()
            return value


class _Socket:
    def __init__(self, sock, check):
        self.sock, self.check = sock, check

    def makefile(self, mode):
        return io.BufferedReader(_Reader(self.sock, self.check))


class _Connection:
    def close(self):
        # HTTPConnection closes a Connection: close socket immediately after
        # headers. Defer that close until our response reader has finished, so
        # shutdown remains possible while a makefile holds the Windows handle.
        if self.sock is getattr(self, '_response_socket', None):
            self.sock = None
        super().close()


class _HTTPConnection(_Connection, http.client.HTTPConnection):
    pass


class _HTTPSConnection(_Connection, http.client.HTTPSConnection):
    pass


class MylibRemoteClient:
    def __init__(self, token_provider, *, timeout=15):
        if not callable(token_provider) or type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 90:
            raise MylibRemoteError('invalid_configuration')
        self._provider, self._timeout = token_provider, timeout
        self._endpoint = ENDPOINT
        self._closed = threading.Event()
        self._lock = threading.Lock()
        self._connections_lock = threading.Lock()
        self._connections = set()
        self._counter = 0
        self._initialized_identity = None

    @classmethod
    def _for_test(cls, token_provider, endpoint, *, timeout=15):
        """Explicit owned-loopback test seam; cannot select remote endpoints."""
        try:
            p = urlsplit(endpoint)
            if (p.scheme != 'http' or p.hostname not in ('127.0.0.1', '::1', 'localhost')
                    or p.username is not None or p.password is not None or p.query or p.fragment
                    or p.path != '/mylib/mcp' or not p.port
                    or any(c.isspace() or ord(c) < 33 for c in endpoint) or '\\' in endpoint):
                raise ValueError()
        except (TypeError, ValueError):
            raise MylibRemoteError('invalid_test_endpoint') from None
        client = cls(token_provider, timeout=timeout)
        client._endpoint = endpoint
        return client

    def close(self):
        self._closed.set()
        with self._connections_lock:
            for connection in tuple(self._connections):
                self._interrupt(connection)

    @staticmethod
    def _interrupt(connection):
        sock = connection.sock or getattr(connection, '_response_socket', None)
        try:
            if sock:
                sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        connection.close()
        if sock:
            sock.close()

    def _check(self, deadline, cancel):
        if self._closed.is_set():
            raise MylibRemoteError('closed')
        if cancel is not None and cancel.is_set():
            raise MylibRemoteError('cancelled')
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise MylibRemoteError('timeout')
        return remaining

    def _post(self, message, token, deadline, cancel, *, initialized=False, notification=False):
        raw = json.dumps(message, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
        if len(raw) > MAX_REQUEST:
            raise MylibRemoteError('request_too_large')
        if _contains(message, token):
            raise MylibRemoteError('credential_in_content')
        parts = urlsplit(self._endpoint)
        options = {'timeout': self._check(deadline, cancel)}
        if parts.scheme == 'https':
            connection = _HTTPSConnection(parts.hostname, parts.port, context=ssl.create_default_context(), **options)
        else:
            connection = _HTTPConnection(parts.hostname, parts.port, **options)
        check = lambda: self._check(deadline, cancel)
        def response_factory(sock, *args, **kwargs):
            # HTTPConnection detaches its socket for Connection: close. Keep a
            # reference so close/cancel can interrupt a stalled detached body.
            connection._response_socket = sock
            return http.client.HTTPResponse(_Socket(sock, check), *args, **kwargs)
        connection.response_class = response_factory
        headers = {'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream',
                   'Authorization': 'Bearer ' + token}
        if initialized:
            headers['MCP-Protocol-Version'] = PROTOCOL
        stop = threading.Event()
        def monitor():
            while not stop.wait(.025):
                try:
                    check()
                except MylibRemoteError:
                    self._interrupt(connection)
                    return
        with self._connections_lock:
            self._connections.add(connection)
        watcher = threading.Thread(target=monitor, daemon=True)
        watcher.start()
        try:
            check()
            connection.request('POST', parts.path, body=raw, headers=headers)
            with connection.getresponse() as response:
                if notification:
                    if response.status != 202:
                        raise MylibRemoteError('invalid_notification_response')
                    return None
                if response.status != 200:
                    raise MylibRemoteError('http_error')
                kind = response.getheader('Content-Type', '').split(';', 1)[0].strip().lower()
                if kind not in ('application/json', 'text/event-stream'):
                    raise MylibRemoteError('unsupported_response')
                size = response.getheader('Content-Length')
                if size is not None and (not size.isascii() or not size.isdecimal() or int(size) > MAX_RESPONSE):
                    raise MylibRemoteError('response_too_large')
                body = bytearray()
                while True:
                    check()
                    chunk = response.read1(min(16384, MAX_RESPONSE + 1 - len(body)))
                    check()
                    if not chunk:
                        break
                    body.extend(chunk)
                    if len(body) > MAX_RESPONSE:
                        raise MylibRemoteError('response_too_large')
                return self._decode(bytes(body), kind, message['id'])
        except MylibRemoteError:
            raise
        except Exception:
            check()
            raise MylibRemoteError('transport_failed') from None
        finally:
            stop.set()
            self._interrupt(connection)
            watcher.join(timeout=.1)
            with self._connections_lock:
                self._connections.discard(connection)

    @staticmethod
    def _decode(body, kind, request_id):
        try:
            text = body.decode('utf-8-sig')
            messages = []
            if kind == 'application/json':
                messages = [_json(text)]
            else:
                data = []
                for line in text.replace('\r\n', '\n').replace('\r', '\n').split('\n') + ['']:
                    if not line:
                        if data:
                            messages.append(_json('\n'.join(data)))
                            data = []
                    elif line.startswith('data:'):
                        data.append(line[5:].removeprefix(' '))
                    elif line.startswith(':') or line.startswith(('event:', 'id:', 'retry:')):
                        continue
                    else:
                        raise ValueError()
            if len(messages) != 1:
                raise ValueError()
            reply = messages[0]
            if (not isinstance(reply, dict) or reply.get('jsonrpc') != '2.0' or 'method' in reply
                    or type(reply.get('id')) is not int or reply['id'] != request_id
                    or ('result' in reply) == ('error' in reply)):
                raise ValueError()
            if 'error' in reply:
                raise MylibRemoteError('rpc_error')
            if not isinstance(reply['result'], dict):
                raise ValueError()
            return reply['result']
        except MylibRemoteError:
            raise
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise MylibRemoteError('invalid_response') from None

    def _call(self, name, arguments, cancel_event):
        if name not in {'catalog_status', 'list_skills', 'search_skills', 'read_skill',
                        'read_resource', 'review_status', 'list_suggestions'}:
            raise MylibRemoteError('unsupported_tool')
        deadline = time.monotonic() + self._timeout
        while not self._lock.acquire(timeout=min(.05, self._check(deadline, cancel_event))):
            pass
        try:
            self._check(deadline, cancel_event)
            try:
                token = self._provider()
            except Exception:
                raise MylibRemoteError('credential_unavailable') from None
            if not isinstance(token, str) or not 1 <= len(token) <= 8192 or any(not 33 <= ord(c) <= 126 for c in token):
                raise MylibRemoteError('invalid_credential')
            def rpc(method, params, **kwargs):
                self._counter += 1
                return self._post({'jsonrpc': '2.0', 'id': self._counter, 'method': method, 'params': params},
                                  token, deadline, cancel_event, **kwargs)
            identity = hashlib.sha256(token.encode('ascii')).digest()
            if identity != self._initialized_identity:
                self._initialized_identity = None
                result = rpc('initialize', {'protocolVersion': PROTOCOL, 'capabilities': {},
                             'clientInfo': {'name': 'supermono-mylib-readonly', 'version': '1'}})
                if result.get('protocolVersion') != PROTOCOL or not isinstance(result.get('capabilities'), dict):
                    raise MylibRemoteError('unsupported_protocol')
                self._post({'jsonrpc': '2.0', 'method': 'notifications/initialized'}, token, deadline,
                           cancel_event, initialized=True, notification=True)
                self._initialized_identity = identity
            result = rpc('tools/call', {'name': name, 'arguments': arguments}, initialized=True)
            if result.get('isError') not in (None, False):
                raise MylibRemoteError('tool_error')
            structured = result.get('structuredContent')
            if structured is not None:
                result = structured
            else:
                content = result.get('content')
                if (not isinstance(content, list) or len(content) != 1
                        or not isinstance(content[0], dict) or content[0].get('type') != 'text'):
                    raise MylibRemoteError('invalid_tool_result')
                try:
                    result = _json(content[0]['text'])
                except Exception:
                    raise MylibRemoteError('invalid_tool_result') from None
            if isinstance(result, dict) and set(result) == {'result'}:
                result = result['result']
            if not isinstance(result, dict):
                raise MylibRemoteError('invalid_tool_result')
            if _contains(result, token):
                raise MylibRemoteError('credential_in_response')
            return result
        except MylibRemoteError:
            raise
        except Exception:
            raise MylibRemoteError('invalid_response') from None
        finally:
            self._lock.release()

    @staticmethod
    def _limit(value, maximum):
        if type(value) is not int or not 1 <= value <= maximum:
            raise MylibRemoteError('invalid_limit')
        return value

    @staticmethod
    def _identity(skill_id, revision):
        if not isinstance(skill_id, str) or not re.fullmatch('[a-f0-9]{64}', skill_id):
            raise MylibRemoteError('invalid_skill')
        if revision is not None and (not isinstance(revision, str) or not re.fullmatch('(?:[a-f0-9]{40}|[a-f0-9]{64})', revision)):
            raise MylibRemoteError('invalid_revision')
        return {'skill_id': skill_id, **({'revision': revision} if revision is not None else {})}

    def catalog_status(self, *, cancel_event=None):
        return self._call('catalog_status', {}, cancel_event)

    def list_skills(self, cursor=None, limit=100, *, cancel_event=None):
        if cursor is not None and (not isinstance(cursor, str) or not re.fullmatch('[A-Za-z0-9_=-]{1,512}', cursor)):
            raise MylibRemoteError('invalid_cursor')
        return self._call('list_skills', {'limit': self._limit(limit, 100), **({'cursor': cursor} if cursor is not None else {})}, cancel_event)

    def search_skills(self, query, limit=10, *, cancel_event=None):
        if not isinstance(query, str) or not query.strip() or len(query) > 512 or not query.isprintable():
            raise MylibRemoteError('invalid_query')
        return self._call('search_skills', {'query': query, 'limit': self._limit(limit, 50)}, cancel_event)

    def read_skill(self, skill_id, revision=None, *, cancel_event=None):
        return self._call('read_skill', self._identity(skill_id, revision), cancel_event)

    def read_resource(self, skill_id, path, revision=None, *, cancel_event=None):
        args = self._identity(skill_id, revision)
        if (not isinstance(path, str) or not 1 <= len(path) <= 1024 or '\\' in path or ':' in path
                or path.startswith('/') or not path.isprintable()
                or any(part in ('', '.', '..') for part in path.split('/'))):
            raise MylibRemoteError('invalid_path')
        args['path'] = path
        return self._call('read_resource', args, cancel_event)

    def review_status(self, *, cancel_event=None):
        return self._call('review_status', {}, cancel_event)

    def list_suggestions(self, limit=20, *, cancel_event=None):
        return self._call('list_suggestions', {'limit': self._limit(limit, 100)}, cancel_event)
