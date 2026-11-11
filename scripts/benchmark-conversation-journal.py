"""Real ConPTY + Hub journal benchmark using only a private synthetic child.

No user CLI, credentials, provider calls, sockets or installed profiles are used.
The canonical comparison is against WinPty.read bytes because ConPTY transforms
process stdout. Queue limits and the actual Hub._read/OutputPump are unchanged.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import deque
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import statistics
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from supermonoagent.conversation_manager import ConversationManager
from supermonoagent.hub import Client, Hub, Session
from supermonoagent.protocol import OP_OUTPUT, OP_SNAPSHOT, unpack
from supermonoagent.pty import WinPty

CHILD = r'''
import os,sys,time,msvcrt
from pathlib import Path
folder=Path(sys.argv[1]);total=int(sys.argv[2])
msvcrt.setmode(sys.stdout.fileno(),os.O_BINARY)
(folder/'ready').write_text('ready',encoding='ascii')
while not (folder/'go').exists():time.sleep(.002)
line=bytes([27])+b'[32mSMA-PRIVATE-PERF '+bytes(range(33,127))+bytes([27])+b'[0m'+bytes([13,10])
block=line*32
left=total
while left:
    data=block[:min(left,len(block))]
    written=0
    while written<len(data):written+=os.write(sys.stdout.fileno(),data[written:])
    left-=len(data)
# Let ConPTY expose the last screen write before terminating its process tree.
time.sleep(.1)
'''


def percentile(values, ratio=.95):
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * ratio) - 1)] if ordered else None


class ObservedPty:
    def __init__(self, real):
        self.real = real
        self.digest = hashlib.sha256()
        self.total = 0
        self.records = []
        self.pending = deque()
        self.first_at = None
        self.last_at = None

    def read(self, size=4096):
        data = self.real.read(size)
        if data:
            now = time.perf_counter()
            if self.first_at is None:
                self.first_at = now
            start = self.total
            self.total += len(data)
            self.digest.update(data)
            self.records.append((start, self.total, hashlib.sha256(data).hexdigest()))
            self.pending.append((self.total, now))
            self.last_at = now
        return data

    def close(self):
        self.real.close()


class ObservedHub(Hub):
    def __init__(self):
        super().__init__()
        self.delivery_ms = []
        self.flush_ms = []
        self.flush_count = 0
        self.overflow_flushes = 0
        self.measured_session = None

    def _flushed(self, sid, data, overflow):
        tick = time.perf_counter()
        super()._flushed(sid, data, overflow)
        done = time.perf_counter()
        session = self.measured_session
        if session is not None:
            through = session.pump.flushed_position
            while session.pty.pending and session.pty.pending[0][0] <= through:
                _position, read_at = session.pty.pending.popleft()
                self.delivery_ms.append((done - read_at) * 1000)
        self.flush_ms.append((done - tick) * 1000)
        self.flush_count += 1
        self.overflow_flushes += bool(overflow)


async def run(enabled, viewers, total_bytes, timeout):
    with tempfile.TemporaryDirectory(prefix='sma-private-real-pty-') as private:
        work = Path(private)
        child_path = work / 'synthetic_child.py'
        child_path.write_text(CHILD, encoding='utf-8')
        manager = ConversationManager(work / 'journals', default_token='synthetic-owner', drain_seconds=0) if enabled else None
        hub = ObservedHub()
        hub.default_owner = 'synthetic-owner'
        hub.persist = False
        hub.journals = manager
        hub.attach_loop(asyncio.get_running_loop())
        window = hub.open_window(owner='synthetic-owner', title='Synthetic benchmark')
        real = WinPty([sys.executable, '-u', str(child_path), str(work), str(total_bytes)], str(work))
        observed = ObservedPty(real)
        session = Session('synthetic-session', window.id, 'synthetic-python', str(work), observed)
        session.pump.mark_grid(session.cols, session.rows)
        window.sessions[session.id] = session
        window.focused = session.id
        hub.sessions[session.id] = session
        hub.measured_session = session
        if manager is not None:
            scope = manager.owner_scope('synthetic-owner')
            session.journal_id = manager.open_session(session, owner_scope=scope, room='synthetic-room', title=window.title)
        # A detached query client invokes the production snapshot encoder. It
        # is never attached/subscribed and cannot acquire input or resize rights.
        snapshot_probe = Client('synthetic-snapshot-probe', asyncio.Queue(), token='synthetic-owner',
            multiplex=True, history_context=True, history_resume=True, history_chunks=True)
        client = None
        consumed = {'frames': 0, 'output_frames': 0, 'snapshot_frames': 0, 'payload_bytes': 0}
        if viewers:
            client = hub.attach(asyncio.Queue(256), token='synthetic-owner')
            client.multiplex = True
            client.reliable = True
            client.window_id, client.session_id = window.id, session.id
            hub.set_subscriptions(client, [session.id], replay=False)
        reader_done = threading.Event()
        def read():
            try:
                hub._read(session)
            finally:
                reader_done.set()
        reader = threading.Thread(target=read, name='synthetic-real-pty-reader', daemon=True)
        reader.start()
        snapshots = []
        sampling = True
        async def observe_viewer():
            while True:
                frame = await client.queue.get()
                if frame is None:
                    return
                op, payload = unpack(frame)
                consumed['frames'] += 1
                consumed['output_frames'] += op == OP_OUTPUT
                consumed['snapshot_frames'] += op == OP_SNAPSHOT
                consumed['payload_bytes'] += len(payload)
        async def sample():
            target = time.perf_counter() + .05
            while sampling:
                await asyncio.sleep(max(0, target - time.perf_counter()))
                if not sampling:
                    break
                started = time.perf_counter()
                hub._session_snapshot_frames(snapshot_probe, session)
                finished = time.perf_counter()
                snapshots.append((started, max(0, finished-target)*1000,
                                  (finished-started)*1000, max(0, started-target)*1000))
                target = max(target + .05, finished)
        viewer_job = asyncio.create_task(observe_viewer()) if client else None
        sample_job = None
        started, reader_elapsed = None, None
        stored_digest, stored_bytes, stored_records, source_ids_match = hashlib.sha256(), 0, 0, True
        try:
            async with asyncio.timeout(30):
                while not (work / 'ready').exists():
                    if reader_done.is_set():
                        raise RuntimeError('synthetic_child_ended_before_ready')
                    await asyncio.sleep(.005)
            sample_job = asyncio.create_task(sample())
            started = time.perf_counter()
            (work / 'go').write_text('go', encoding='ascii')
            async with asyncio.timeout(timeout):
                while not reader_done.is_set():
                    await asyncio.sleep(.01)
            reader_elapsed = max(.000001, observed.last_at - started)
            # Allow actual final coalescing callback to finish after EOF.
            await asyncio.sleep(.1)
            sampling = False
            await sample_job
            before_drain = manager.capture.status() if manager else None
            drain_started = time.perf_counter()
            flushed = await asyncio.to_thread(manager.capture.flush, 60) if manager else True
            drain_seconds = time.perf_counter() - drain_started
            if manager:
                cursor = None
                while True:
                    page = await asyncio.to_thread(manager.store.read_page, scope, session.journal_id,
                                                   cursor=cursor, limit=100)
                    for event in page['events']:
                        if event['kind'] != 'raw_output':
                            continue
                        body = manager.store.content_bytes(session.journal_id, event['content_id'])
                        stored_digest.update(body)
                        stored_bytes += len(body)
                        if stored_records >= len(observed.records):
                            source_ids_match = False
                        else:
                            first, last, digest = observed.records[stored_records]
                            source_ids_match = source_ids_match and event['source_record_id'] == (
                                f'pty:{session.process_generation}:{first}:{last}') and hashlib.sha256(body).hexdigest() == digest
                        stored_records += 1
                    cursor = page['next_cursor']
                    if cursor is None:
                        break
            status = manager.capture.status() if manager else None
            # pywinpty can wait several seconds at EOF. Exclude those idle
            # samples so tail waiting cannot dilute active-output snapshot p95.
            active_snapshots = [item for item in snapshots if started <= item[0] <= observed.last_at + .025]
            snapshot_ms = [item[1] for item in active_snapshots]
            snapshot_call_ms = [item[2] for item in active_snapshots]
            scheduler_ms = [item[3] for item in active_snapshots]
            fidelity = not enabled or (flushed and stored_bytes == observed.total and stored_records == len(observed.records)
                                      and stored_digest.hexdigest() == observed.digest.hexdigest() and source_ids_match
                                      and not status['error'] and status['dropped_bytes'] == 0)
            return dict(enabled=enabled, viewers=viewers, process_payload_bytes=total_bytes,
                boundary_bytes=observed.total, boundary_sha256=observed.digest.hexdigest(),
                boundary_records=len(observed.records), active_reader_seconds=reader_elapsed,
                boundary_reader_mib_s=observed.total / 1024**2 / reader_elapsed,
                snapshot_samples=len(snapshot_ms), snapshot_request_p95_ms=percentile(snapshot_ms),
                snapshot_call_p95_ms=percentile(snapshot_call_ms), scheduler_p95_ms=percentile(scheduler_ms),
                delivery_p95_ms=percentile(hub.delivery_ms), measured_delivery_records=len(hub.delivery_ms),
                flush_p95_ms=percentile(hub.flush_ms), flush_count=hub.flush_count,
                overflow_flushes=hub.overflow_flushes, observer=consumed,
                viewer_failure=client.failure if client else None,
                stored_bytes=stored_bytes if enabled else None, stored_sha256=stored_digest.hexdigest() if enabled else None,
                stored_records=stored_records if enabled else None, source_ids_match=source_ids_match if enabled else None,
                before_drain=before_drain, status=status, drain_seconds=drain_seconds, fidelity=fidelity)
        finally:
            sampling = False
            if sample_job:
                sample_job.cancel()
                await asyncio.gather(sample_job, return_exceptions=True)
            if viewer_job:
                viewer_job.cancel()
                await asyncio.gather(viewer_job, return_exceptions=True)
            observed.close()
            await asyncio.to_thread(reader.join, 10)
            if manager:
                await manager.aclose()


async def benchmark(args):
    result = dict(schema=1, benchmark='actual-conpty-hub-read-outputpump', platform=platform.platform(),
        python=platform.python_version(), producer='private deterministic printable ASCII plus ANSI SGR and CRLF',
        process_payload_bytes=args.mib * 1024**2, configured_source_rate='unpaced natural ConPTY',
        pairs_per_viewer_variant=args.pairs, snapshot_interval_ms=50,
        default_capture_queue_items=1024, default_capture_queue_bytes=16*1024**2,
        default_output_ring_bytes=2*1024**2,
        scope='Actual Hub._read, OutputPump delivery and Hub._session_snapshot_frames chunk encoding; detached snapshot query plus optional in-process passive viewer, no socket or renderer',
        fidelity_boundary='Exact bytes returned by WinPty.read; producer bytes may be transformed by ConPTY',
        timing_boundary='Gate release to last nonempty WinPty.read; storage drain reported separately',
        snapshot_sample_boundary='Gate release through last nonempty read plus25ms; idleEOFwait excluded', variants=[], passed=False)
    for viewers in args.viewers:
        pairs = []
        for index in range(args.pairs):
            order = [False, True] if index % 2 == 0 else [True, False]
            runs = {}
            for enabled in order:
                print(json.dumps(dict(progress='running', viewers=viewers, pair=index+1, enabled=enabled)), flush=True)
                runs[enabled] = await run(enabled, viewers, args.mib * 1024**2, args.timeout)
            baseline, enabled = runs[False], runs[True]
            pair = dict(baseline=baseline, enabled=enabled,
                throughput_degradation_percent=(1-enabled['boundary_reader_mib_s']/baseline['boundary_reader_mib_s'])*100,
                extra_snapshot_p95_ms=enabled['snapshot_request_p95_ms']-baseline['snapshot_request_p95_ms'],
                extra_delivery_p95_ms=enabled['delivery_p95_ms']-baseline['delivery_p95_ms'])
            pairs.append(pair)
            print(json.dumps(dict(progress='pair_complete', viewers=viewers, pair=index+1,
                degradation_percent=pair['throughput_degradation_percent'], fidelity=enabled['fidelity'])), flush=True)
        degradation = statistics.median(pair['throughput_degradation_percent'] for pair in pairs)
        snapshot = statistics.median(pair['extra_snapshot_p95_ms'] for pair in pairs)
        delivery = statistics.median(pair['extra_delivery_p95_ms'] for pair in pairs)
        fidelity = all(pair['enabled']['fidelity'] for pair in pairs)
        variant = dict(viewers=viewers, pairs=pairs, all_fidelity=fidelity,
            median_throughput_degradation_percent=degradation, median_extra_snapshot_p95_ms=snapshot,
            median_extra_delivery_p95_ms=delivery,
            passed=fidelity and degradation <= 10 and snapshot <= 20 and delivery <= 20
                   and all(not pair['enabled']['viewer_failure'] for pair in pairs))
        result['variants'].append(variant)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2)+'\n', encoding='utf-8')
    result['passed'] = all(variant['passed'] for variant in result['variants'])
    args.output.write_text(json.dumps(result, indent=2)+'\n', encoding='utf-8')
    print(json.dumps({key:result[key] for key in ['benchmark','passed']}), flush=True)
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / '.omx/artifacts/conversation-journal-performance/hub-real-pty.json')
    parser.add_argument('--pairs', type=int, default=3)
    parser.add_argument('--viewers', type=int, nargs='+', choices=[0,1], default=[0,1])
    parser.add_argument('--mib', type=int, default=10)
    parser.add_argument('--timeout', type=float, default=300)
    args=parser.parse_args()
    if not 1<=args.pairs<=10 or not 1<=args.mib<=100 or not 1<=args.timeout<=600:
        parser.error('bounded pairs, MiB and timeout required')
    result=asyncio.run(benchmark(args))
    return 0 if result['passed'] else 1


if __name__=='__main__':
    raise SystemExit(main())
