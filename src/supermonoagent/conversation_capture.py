"""Bounded off-reader journal writer. An accepted enqueue is never a saved ACK."""
from __future__ import annotations

from collections import deque
import threading
import time

from .conversation_contract import JournalError, checked_integer

_RAW_COALESCE_SECONDS = 0.003


class JournalCapture:
    def __init__(self, store, *, queue_bytes=16 * 1024 * 1024, queue_items=1024):
        checked_integer(queue_bytes, 1)
        checked_integer(queue_items, 1, 100000)
        self.store = store
        self._max_bytes, self._max_items = queue_bytes, queue_items
        self._condition = threading.Condition()
        self._queue = deque()
        self._bytes = 0
        self._inflight = 0
        self._closing = False
        self._drain_requested = False
        self._closed = False
        self._journals = set()
        self._gaps = {}
        self._errors = {}
        self._committed = 0
        self._failed = 0
        self._dropped = 0
        self._raw_batches = 0
        self._max_raw_batch_items = 0
        self._peak_pending_items = 0
        self._peak_pending_bytes = 0
        self._worker = threading.Thread(target=self._run, name='conversation-journal', daemon=True)
        self._worker.start()

    def _submit(self, journal_id, action, args, kwargs, size=0):
        with self._condition:
            if self._closing:
                return False
            self._journals.add(journal_id)
            if self._bytes + size > self._max_bytes or len(self._queue) + self._inflight >= self._max_items:
                self._errors[journal_id] = 'capture_gap'
                self._gaps[journal_id] = self._gaps.get(journal_id, 0) + size
                self._dropped += size
                self._condition.notify_all()
                return False
            self._bytes += size
            self._queue.append((journal_id, action, args, kwargs, size))
            self._peak_pending_items = max(self._peak_pending_items, len(self._queue) + self._inflight)
            self._peak_pending_bytes = max(self._peak_pending_bytes, self._bytes)
            self._condition.notify_all()
            return True

    def record_output(self, journal_id, data: bytes, *, source_record_id=None, observed_at=None, metadata=None):
        if not isinstance(data, bytes):
            raise JournalError()
        return self._submit(journal_id, 'append_raw', (data,),
            dict(source_record_id=source_record_id, observed_at=observed_at, metadata=metadata), len(data))

    def append_event(self, journal_id, event, content=b''):
        if not isinstance(content, (str, bytes)) or not isinstance(event, dict):
            raise JournalError()
        size = len(content.encode('utf-8')) if isinstance(content, str) else len(content)
        # The caller cannot mutate a queued semantic envelope after validation.
        from .conversation_contract import canonical_json
        import json
        return self._submit(journal_id, 'append_event', (json.loads(canonical_json(event)), content), {}, size)

    def terminal_end(self, journal_id, *, outcome='exited', pending=False):
        return self._submit(journal_id, 'terminal_end', (), dict(outcome=outcome, pending=pending))

    def suspend(self, journal_id, *, reason='recorder_stopped'):
        return self._submit(journal_id, 'suspend', (), dict(reason=reason))

    def seal(self, journal_id, *, incomplete=False):
        return self._submit(journal_id, 'seal', (), dict(incomplete=incomplete))

    def _run(self):
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._queue or self._gaps or self._closing)
                # A visible gap must reach the journal before a queued seal.
                if self._gaps:
                    journal, lost = self._gaps.popitem()
                    action, args, kwargs, size = 'append_event', (
                        dict(kind='capture_gap', actor='system', origin='recorder', state='incomplete',
                             metadata={'dropped_bytes': lost}), b''), {}, 0
                    self._inflight = 1
                elif self._queue:
                    journal, action, args, kwargs, size = self._queue.popleft()
                    records = None
                    self._inflight = 1
                    if action == 'append_raw' and size <= 256 * 1024:
                        records = [dict(data=args[0], **kwargs)]
                        deadline = time.monotonic() + _RAW_COALESCE_SECONDS
                        # Drain contiguous chunks only. Never pass a semantic,
                        # lifecycle or another journal's operation in the queue.
                        while len(records) < 64 and size < 256 * 1024:
                            if self._queue:
                                next_journal, next_action, next_args, next_kwargs, next_size = self._queue[0]
                                if (next_journal != journal or next_action != 'append_raw'
                                        or size + next_size > 256 * 1024):
                                    break
                                self._queue.popleft()
                                records.append(dict(data=next_args[0], **next_kwargs))
                                size += next_size
                                self._inflight = len(records)
                                continue
                            remaining = deadline - time.monotonic()
                            if (remaining <= 0 or self._closing or self._drain_requested or self._gaps):
                                break
                            # Release the enqueue lock, keep one absolute bound,
                            # and wake immediately for any ordering barrier.
                            self._condition.wait(remaining)
                        self._inflight = len(records)
                elif self._closing:
                    break
            try:
                count = self._inflight
                if action == 'append_raw' and records is not None and len(records) > 1:
                    self.store.append_raw_many(journal, records)
                    with self._condition:
                        self._raw_batches += 1
                        self._max_raw_batch_items = max(self._max_raw_batch_items, len(records))
                else:
                    getattr(self.store, action)(journal, *args, **kwargs)
                with self._condition:
                    if not (action == 'append_event' and args[0].get('kind') == 'capture_gap'):
                        self._committed += count
            except Exception:
                # Never include exception text: it can contain paths or bodies.
                with self._condition:
                    self._errors[journal] = 'storage_error'
                    self._failed += self._inflight
                    if action == 'append_raw':
                        self._gaps[journal] = self._gaps.get(journal, 0) + size
                        self._dropped += size
            finally:
                with self._condition:
                    self._inflight = 0
                    self._bytes -= size
                    if not self._queue and not self._gaps:
                        self._drain_requested = False
                    self._condition.notify_all()
        for journal in self._journals:
            try:
                self.store.suspend(journal)
            except Exception:
                with self._condition:
                    self._errors[journal] = 'storage_error'
        with self._condition:
            self._closed = True
            self._condition.notify_all()

    def flush(self, timeout=2.0):
        if type(timeout) not in (int, float) or not 0 <= timeout <= 60:
            raise JournalError()
        deadline = time.monotonic() + timeout
        with self._condition:
            if self._queue or self._inflight or self._gaps:
                self._drain_requested = True
                self._condition.notify_all()
            while self._queue or self._inflight or self._gaps:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    def status(self, journal_id=None):
        with self._condition:
            error = self._errors.get(journal_id) if journal_id else (
                'storage_error' if 'storage_error' in self._errors.values() else
                'capture_gap' if self._errors else None)
            return dict(pending_items=len(self._queue) + self._inflight + len(self._gaps),
                pending_bytes=self._bytes, committed_items=self._committed,
                failed_items=self._failed, dropped_bytes=self._dropped,
                raw_batch_commits=self._raw_batches, max_raw_batch_items=self._max_raw_batch_items,
                peak_pending_items=self._peak_pending_items, peak_pending_bytes=self._peak_pending_bytes,
                error=error, closed=self._closed)

    def close(self, timeout=2.0):
        if type(timeout) not in (int, float) or not 0 <= timeout <= 60:
            raise JournalError()
        with self._condition:
            self._closing = True
            self._condition.notify_all()
        self._worker.join(timeout)
        return not self._worker.is_alive()
