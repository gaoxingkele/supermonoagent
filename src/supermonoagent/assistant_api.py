"""Bounded text-only OpenAI-compatible requests, with explicit credentials only."""
from __future__ import annotations

import json
import io
import math
import ssl
import time
from functools import partial
from http.client import HTTPException, HTTPResponse
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPHandler, HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener


MAX_REQUEST_BYTES = 128 * 1024
MAX_RESPONSE_BYTES = 256 * 1024
MAX_TEXT_CHARS = 16384


class AssistantAPIError(ValueError):
    """Safe to display: never includes response bodies, URLs, or credentials."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _endpoint(value):
    if (not isinstance(value, str) or not value or len(value) > 16384
            or not value.isprintable() or any(char.isspace() for char in value)
            or any(char in value for char in "\\?#")):
        raise AssistantAPIError("Invalid model endpoint")
    try:
        parts = urlsplit(value)
        if (not parts.hostname or parts.username is not None or parts.password is not None
                or parts.scheme not in {"https", "http"}
                or parts.port is not None and not 1 <= parts.port <= 65535
                or parts.scheme == "http" and parts.hostname not in {"localhost", "127.0.0.1", "::1"}):
            raise ValueError
    except ValueError:
        raise AssistantAPIError("Invalid model endpoint") from None
    value = value.rstrip("/")
    return value if value.endswith("/chat/completions") else value + "/chat/completions"


def _remaining(deadline, cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise AssistantAPIError("Model request cancelled")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise AssistantAPIError("Model request failed or timed out")
    return remaining


class _DeadlineReader(io.RawIOBase):
    """Apply the deadline to every socket read, including response headers."""

    def __init__(self, sock, deadline, cancel_event):
        super().__init__()
        self._sock = sock
        self._raw = sock.makefile("rb", buffering=0)
        self._deadline = deadline
        self._cancel_event = cancel_event

    def readable(self):
        return True

    def readinto(self, buffer):
        self._sock.settimeout(_remaining(self._deadline, self._cancel_event))
        result = self._raw.readinto(buffer)
        _remaining(self._deadline, self._cancel_event)
        return result

    def close(self):
        try:
            self._raw.close()
        finally:
            super().close()


class _ResponseSocket:
    def __init__(self, sock, deadline, cancel_event):
        self.sock, self.deadline, self.cancel_event = sock, deadline, cancel_event

    def makefile(self, mode):
        return io.BufferedReader(_DeadlineReader(self.sock, self.deadline, self.cancel_event))


class _DeadlineResponse(HTTPResponse):
    def __init__(self, sock, *args, deadline, cancel_event, **kwargs):
        super().__init__(_ResponseSocket(sock, deadline, cancel_event), *args, **kwargs)


class _DeadlineHandler:
    def __init__(self, deadline, cancel_event, **kwargs):
        self.deadline, self.cancel_event = deadline, cancel_event
        super().__init__(**kwargs)

    def do_open(self, http_class, req, **kwargs):
        def connection(host, **options):
            result = http_class(host, **options)
            result.response_class = partial(_DeadlineResponse, deadline=self.deadline,
                                            cancel_event=self.cancel_event)
            return result
        return super().do_open(connection, req, **kwargs)


class _HTTPDeadlineHandler(_DeadlineHandler, HTTPHandler):
    pass


class _HTTPSDeadlineHandler(_DeadlineHandler, HTTPSHandler):
    pass


def query(endpoint, model, messages, api_key="", timeout=30, *, cancel_event=None) -> str:
    """Query a text model. No environment credentials, proxies, tools, or redirects.

    ``timeout`` is the open/body budget in seconds, bounded to 90 seconds.
    System DNS resolution and multi-address connection setup may exceed this
    budget: urllib cannot cancel the OS resolver or its connection retries.
    Response headers/body use the remaining deadline, including against slow
    trickles. Cancellation is checked before open and between socket reads.
    API keys are held by the caller, never persisted. A key repeated in supplied
    model/context text is rejected before any request is sent.
    """
    url = _endpoint(endpoint)
    if (not isinstance(model, str) or not model.strip() or len(model) > 128
            or not model.isprintable()):
        raise AssistantAPIError("Invalid model name")
    if (not isinstance(api_key, str) or len(api_key) > 8192
            or any(not 33 <= ord(char) <= 126 for char in api_key)):
        raise AssistantAPIError("Invalid model credential")
    if (type(timeout) not in {int, float} or not 0 < timeout <= 90
            or not math.isfinite(timeout)):
        raise AssistantAPIError("Invalid model timeout")
    if not isinstance(messages, (list, tuple)) or not 1 <= len(messages) <= 14:
        raise AssistantAPIError("Invalid model messages")
    copied = []
    for message in messages:
        if (not isinstance(message, dict) or set(message) != {"role", "content"}
                or message.get("role") not in ("system", "user", "assistant")
                or not isinstance(message.get("content"), str)
                or len(message["content"]) > 32768):
            raise AssistantAPIError("Invalid model messages")
        copied.append(dict(message))
    payload = {"model": model, "messages": copied, "stream": False, "store": False,
               "max_completion_tokens": 1024, "tools": [], "tool_choice": "none"}
    try:
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (ValueError, UnicodeError):
        raise AssistantAPIError("Invalid model messages") from None
    if len(raw) > MAX_REQUEST_BYTES:
        raise AssistantAPIError("Model request exceeds size limit")
    if api_key and (api_key in model or api_key in endpoint
                    or any(api_key in message["content"] for message in copied)):
        raise AssistantAPIError("Model credential must not appear in request content")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    request = Request(url, data=raw, headers=headers, method="POST")
    deadline = time.monotonic() + timeout
    opener = build_opener(ProxyHandler({}), _HTTPDeadlineHandler(deadline, cancel_event),
                          _HTTPSDeadlineHandler(deadline, cancel_event,
                                                context=ssl.create_default_context()), _NoRedirect())
    try:
        with opener.open(request, timeout=_remaining(deadline, cancel_event)) as response:
            result = bytearray()
            while len(result) <= MAX_RESPONSE_BYTES:
                _remaining(deadline, cancel_event)
                # The transport also applies this deadline to response headers
                # and every underlying body read, before buffering can wait.
                chunk = response.read1(min(16384, MAX_RESPONSE_BYTES + 1 - len(result)))
                _remaining(deadline, cancel_event)
                if not chunk:
                    break
                result.extend(chunk)
            result = bytes(result)
    except AssistantAPIError:
        raise
    except HTTPError as error:
        status = error.code
        error.close()
        raise AssistantAPIError(f"Model request failed (HTTP {status})") from None
    except (URLError, OSError, ValueError, HTTPException):
        raise AssistantAPIError("Model request failed or timed out") from None
    if len(result) > MAX_RESPONSE_BYTES:
        raise AssistantAPIError("Model response exceeds size limit")
    try:
        data = json.loads(result)
        choices = data["choices"]
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ValueError
        choice = choices[0]
        message = choice["message"]
        if (not isinstance(message, dict) or message.get("role") != "assistant"
                or message.get("tool_calls") or message.get("function_call")
                or message.get("refusal") or choice.get("finish_reason") in {"tool_calls", "function_call"}):
            raise ValueError
        content = message["content"]
        if not isinstance(content, str) or not content.strip() or len(content) > MAX_TEXT_CHARS:
            raise ValueError
        content.encode("utf-8")
        # A provider must never turn the credential into displayed model output.
        if api_key and api_key in content:
            raise ValueError
        return content
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        raise AssistantAPIError("Invalid or unsupported model response") from None
