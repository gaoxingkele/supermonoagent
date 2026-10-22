"""Coalesce PTY bytes so a busy shell wakes the event loop once per window."""

from __future__ import annotations

import threading

RING_BYTES = 256 * 1024
PENDING_BYTES = 128 * 1024
COALESCE_S = 0.02


class OutputPump:
    def __init__(self, coalesce_s: float = COALESCE_S) -> None:
        self.coalesce_s = coalesce_s
        self._lock = threading.Lock()
        self._ring = bytearray()
        self._pending = bytearray()
        self._armed = False
        self._overflow = False
        self._position = 0
        self._pending_end = 0
        self.flushed_position = 0

    def snapshot(self) -> bytes:
        with self._lock:
            return bytes(self._ring)

    def snapshot_position(self) -> tuple[bytes, int]:
        with self._lock:
            return bytes(self._ring), self._position

    def remember(self, data: bytes) -> None:
        if not data:
            return
        with self._lock:
            self._ring.extend(data)
            self._position += len(data)
            extra = len(self._ring) - RING_BYTES
            if extra > 0:
                del self._ring[:extra]

    def stage(self, data: bytes, loop, on_flush) -> None:
        if not data:
            return
        arm = False
        with self._lock:
            self._pending.extend(data)
            self._pending_end = self._position
            if len(self._pending) > PENDING_BYTES:
                self._pending.clear()
                self._overflow = True
            if not self._armed:
                self._armed = True
                arm = True
        if arm:
            loop.call_soon_threadsafe(self._schedule, loop, on_flush)

    def add(self, data: bytes, loop, on_flush) -> None:
        self.remember(data)
        self.stage(data, loop, on_flush)

    def _schedule(self, loop, on_flush) -> None:
        loop.call_later(self.coalesce_s, self._fire, on_flush)

    def _fire(self, on_flush) -> None:
        with self._lock:
            overflow = self._overflow
            data = bytes(self._pending)
            snap = bytes(self._ring) if overflow else b""
            self._pending.clear()
            self._overflow = False
            self._armed = False
            self.flushed_position = self._pending_end
        if overflow:
            on_flush(snap, True)
        elif data:
            on_flush(data, False)
