"""Coalesce PTY bytes so a busy shell wakes the event loop once per window."""

from __future__ import annotations

import threading
import time
import secrets

from .ansi_state import bounded_snapshot
from .history_context import ReplayRenditionState

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
        self.stream_id = secrets.token_hex(12)
        self._last_output_ns = None
        self._pending_end = 0
        self.flushed_position = 0
        self._head_style = ReplayRenditionState(emit_modes=False)
        self._cropped = False
        self._history_grid = None
        self._history_epoch = 0
        self._history_reason = ''
        self._epoch_ring = bytearray()
        self._epoch_head_style = ReplayRenditionState()
        self._epoch_cropped = False
        self._pending_epoch = None
        self._pending_mixed_epoch = False
        self._flushed_history_context = None

    def begin_resize(self) -> None:
        """Quarantine reads during the OS call without holding its output pipe."""
        with self._lock:
            self._history_reason = 'resize_boundary'

    def mark_grid(self, cols: int, rows: int) -> None:
        """Start a bounded grid epoch, never claiming a ConPTY byte fence.

        ResizePseudoConsole may finish an old-size frame after returning. A
        resized session therefore remains explicitly partial for its lifetime.
        The independent legacy ring is neither cleared nor rewritten.
        """
        if type(cols) is not int or type(rows) is not int or not (2 <= cols <= 500 and 1 <= rows <= 200):
            raise ValueError('Invalid history grid')
        with self._lock:
            if self._history_grid == (cols, rows):
                return
            inherited = (self._epoch_head_style if self._history_grid is not None else self._head_style).copy()
            inherited.feed(bytes(self._epoch_ring if self._history_grid is not None else self._ring))
            inherited.emit_modes = True
            if self._history_grid is not None:
                inherited.region = None
            if self._history_grid is not None:
                self._history_epoch += 1
                self._history_reason = 'resize_boundary'
            elif self._ring:
                self._history_reason = 'legacy_history_without_grid'
            self._history_grid = (cols, rows)
            self._epoch_ring.clear()
            self._epoch_head_style = inherited
            self._epoch_cropped = bool(self._ring)

    def _history_context_locked(self, kind):
        cols, rows = self._history_grid or (100, 28)
        reason = self._history_reason
        if self._history_grid is None:
            reason = 'uninitialized_grid'
        elif not reason and self._epoch_cropped and kind == 'snapshot':
            reason = 'history_ring_truncated'
        return {'v': 1, 'kind': kind, 'cols': cols, 'rows': rows,
                'epoch': self._history_epoch, 'partial': bool(reason), 'reason': reason}

    def history_context(self):
        with self._lock:
            return self._history_context_locked('output')

    def history_snapshot_position(self):
        with self._lock:
            data = bounded_snapshot(bytes(self._epoch_ring), RING_BYTES,
                                    self._epoch_head_style, cropped=self._epoch_cropped)
            return data, self._position, self._history_context_locked('snapshot')

    def history_resume_position(self, position, epoch, stream_id):
        """Return only an exact retained interval; gaps require a fresh snapshot."""
        with self._lock:
            if (type(position) is int and type(epoch) is int
                    and stream_id == self.stream_id
                    and epoch == self._history_epoch and self._history_grid is not None
                    and self._position - len(self._epoch_ring) <= position <= self._position):
                offset = position - (self._position - len(self._epoch_ring))
                data = bytes(self._epoch_ring[offset:])
                context = self._history_context_locked('resume')
            else:
                data = bounded_snapshot(bytes(self._epoch_ring), RING_BYTES,
                                        self._epoch_head_style, cropped=self._epoch_cropped)
                context = self._history_context_locked('snapshot')
            context.update(v=2, end_position=self._position, stream_id=self.stream_id)
            return data, self._position, context

    def flushed_history_context(self):
        with self._lock:
            return dict(self._flushed_history_context or self._history_context_locked('output'))

    def _snapshot_locked(self) -> bytes:
        return bounded_snapshot(bytes(self._ring), RING_BYTES, self._head_style, cropped=self._cropped)

    def snapshot(self) -> bytes:
        with self._lock:
            return self._snapshot_locked()

    def snapshot_position(self) -> tuple[bytes, int]:
        with self._lock:
            return self._snapshot_locked(), self._position

    def position(self) -> int:
        with self._lock:
            return self._position

    def output_age_ms(self) -> int | None:
        with self._lock:
            return None if self._last_output_ns is None else max(0, (time.monotonic_ns() - self._last_output_ns) // 1_000_000)

    def remember(self, data: bytes) -> None:
        if not data:
            return
        with self._lock:
            self._ring.extend(data)
            self._last_output_ns = time.monotonic_ns()
            self._position += len(data)
            extra = len(self._ring) - RING_BYTES
            if extra > 0:
                self._head_style.feed(bytes(self._ring[:extra]))
                del self._ring[:extra]
                self._cropped = True
            if self._history_grid is not None:
                self._epoch_ring.extend(data)
                extra_epoch = len(self._epoch_ring) - RING_BYTES
                if extra_epoch > 0:
                    self._epoch_head_style.feed(bytes(self._epoch_ring[:extra_epoch]))
                    del self._epoch_ring[:extra_epoch]
                    # Its mode state includes the resize margin reset, unlike
                    # the independent byte-compatible legacy rendition head.
                    self._epoch_cropped = True

    def stage(self, data: bytes, loop, on_flush) -> None:
        if not data:
            return
        arm = False
        with self._lock:
            if not self._pending:
                self._pending_epoch = self._history_epoch
                self._pending_mixed_epoch = False
            elif self._pending_epoch != self._history_epoch:
                self._pending_mixed_epoch = True
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
            snap = self._snapshot_locked() if overflow else b""
            self._pending.clear()
            self._overflow = False
            self._armed = False
            self.flushed_position = self._pending_end
            context = self._history_context_locked('output')
            if self._pending_mixed_epoch or self._pending_epoch != self._history_epoch:
                context.update(partial=True, reason='mixed_epoch_output')
            self._flushed_history_context = context
            self._pending_epoch = None
            self._pending_mixed_epoch = False
        if overflow:
            on_flush(snap, True)
        elif data:
            on_flush(data, False)
