"""History grid context is opt-in, bounded and explicitly uncertain after resize."""
import asyncio
import json
import threading
from unittest.mock import patch

import pytest
from websockets.asyncio.client import connect

from supermonoagent import protocol
from supermonoagent.history_context import ReplayRenditionState, pack_history_context, unpack_history_context
from supermonoagent.ansi_state import SgrState
from supermonoagent.hub import Hub, Session
from supermonoagent.protocol import (OP_HELLO, OP_HELLO_OK, OP_HISTORY_CONTEXT, OP_OUTPUT,
                                    OP_SNAPSHOT, OP_SUBSCRIBE, ProtocolError,
                                    pack_feed, pack_json, unpack)
from supermonoagent.pump import OutputPump
from test_client_integration import isolated_host, receive_op


def metadata(kind='snapshot', **changes):
    return {'v': 1, 'kind': kind, 'cols': 100, 'rows': 28, 'epoch': 0,
            'partial': False, 'reason': '', **changes}


def test_opt_in_mode_tracking_keeps_legacy_rendition_bytes_identical():
    legacy, extended = SgrState(), ReplayRenditionState(emit_modes=False)
    stream = b'\x1b[?1049h\x1b[?7l\x1b[2;20r\x1b[?6h\x1b[31;1m\x1b[?2004h'
    for value in stream:
        legacy.feed(bytes([value]));extended.feed(bytes([value]))
    assert extended.prefix() == legacy.prefix()
    replay = extended.copy();replay.emit_modes = True
    assert b'\x1b[?1049h' in replay.prefix()
    assert b'\x1b[?7l' in replay.prefix()
    assert b'\x1b[2;20r' in replay.prefix()
    assert b'\x1b[?6h' in replay.prefix()
    replay.feed(b'\x1b[!p')
    assert replay.alternate and replay.wraparound and not replay.origin and replay.region is None
    replay.feed(b'\x1bc')
    assert not replay.alternate


def test_cropped_epoch_retains_fullscreen_and_wrap_modes_across_frame_budget():
    with patch('supermonoagent.pump.RING_BYTES', 192):
        pump = OutputPump();pump.mark_grid(80, 24)
        pump.remember(b'\x1b[?1049h\x1b[?7l\x1b[31m' + b'x' * 800)
        legacy = pump.snapshot()
        assert b'\x1b[?1049h' not in legacy and b'\x1b[?7l' not in legacy
        data, _, context = pump.history_snapshot_position()
        assert b'\x1b[?1049h' in data and b'\x1b[?7l' in data
        with patch('supermonoagent.protocol.MAX_FRAME', 240):
            framed = pack_history_context('session', context, data)
        _, context, replay = unpack_history_context(unpack(framed)[1])
        assert context['partial']
        assert b'\x1b[?1049h' in replay and b'\x1b[?7l' in replay


def test_resize_clears_epoch_scroll_margins_but_retains_alternate_buffer_mode():
    pump = OutputPump();pump.mark_grid(80, 54)
    pump.remember(b'\x1b[?1049h\x1b[5;50r\x1b[?6h')
    pump.begin_resize();pump.mark_grid(80, 31)
    pump.remember(b'current frame')
    replay, _, context = pump.history_snapshot_position()
    assert context['partial'] and b'\x1b[?1049h' in replay
    assert b'\x1b[5;50r' not in replay


class Loop:
    def __init__(self):
        self.jobs = []

    def call_soon_threadsafe(self, callback, *args):
        callback(*args)

    def call_later(self, delay, callback, *args):
        self.jobs.append((callback, args))

    def fire(self):
        callback, args = self.jobs.pop(0)
        callback(*args)


class Pty:
    def __init__(self):
        self.resize_calls = []
        self.written = []

    def resize(self, cols, rows):
        self.resize_calls.append((cols, rows))

    def write(self, data):
        self.written.append(data)


def hub_fixture(context=True):
    hub = Hub()
    window = hub.open_window(owner='owner')
    session = Session('aabbccdd', window.id, 'test', '.', Pty())
    session.pump.mark_grid(session.cols, session.rows)
    window.sessions[session.id] = session
    window.focused = session.id
    hub.sessions[session.id] = session
    client = hub.attach(asyncio.Queue(32), token='owner')
    client.multiplex = True
    client.history_context = context
    client.subscriptions.add(session.id)
    client.window_id, client.session_id = window.id, session.id
    return hub, client, session


def test_binary_envelope_is_one_frame_and_keeps_raw_bytes():
    raw = b'\x1b[31m' + '中文'.encode() + b'\0binary'
    frame = pack_history_context('aabbccdd', metadata(), raw)
    op, payload = unpack(frame)
    assert op == OP_HISTORY_CONTEXT
    assert unpack_history_context(payload) == ('aabbccdd', metadata(), raw)
    assert len(frame) <= protocol.MAX_FRAME + 4


def test_activity_subscription_can_skip_replay_without_losing_subsequent_live_bytes():
    hub, client, session = hub_fixture()
    client.subscriptions.clear()
    while not client.queue.empty():
        client.queue.get_nowait()
    session.pump.remember(b'historical bytes must not be replayed')
    hub.set_subscriptions(client, [session.id], replay=False)
    assert client.queue.empty()
    assert client.feed_offsets[session.id] == session.pump.position()
    session.pump.remember(b'fresh')
    session.pump.flushed_position = session.pump.position()
    hub._flushed(session.id, b'historical bytes must not be replayedfresh', False)
    op, payload = unpack(client.queue.get_nowait())
    assert op == OP_HISTORY_CONTEXT
    assert unpack_history_context(payload)[2] == b'fresh'
    assert session.resize_owner is None and not session.pty.written


def test_history_catalog_update_age_is_monotonic_and_opt_in():
    hub, client, session = hub_fixture()
    assert session.pump.output_age_ms() is None
    with patch('supermonoagent.pump.time.monotonic_ns', side_effect=[10_000_000_000, 12_500_000_000]):
        session.pump.remember(b'output')
        assert session.pump.output_age_ms() == 2500
    old = hub.summary('owner')['windows'][0]['sessions'][0]
    modern = hub.summary('owner', history_context=True)['windows'][0]['sessions'][0]
    assert 'output_age_ms' not in old
    assert isinstance(modern['output_age_ms'], int)


@pytest.mark.parametrize('changes', [
    {'v': True}, {'v': 2}, {'kind': 'other'}, {'cols': True}, {'cols': 501},
    {'rows': 0}, {'rows': 201}, {'epoch': -1}, {'epoch': 2**53},
    {'partial': 1}, {'partial': True}, {'reason': 'mismatch'},
    {'partial': True, 'reason': 'unsafe\nreason'}, {'extra': 1},
])
def test_metadata_rejects_invalid_types_and_bounds(changes):
    with pytest.raises(ProtocolError):
        pack_history_context('sid', metadata(**changes), b'raw')


@pytest.mark.parametrize('payload', [b'', b'sid\0', b'sid\0\xff\xffx', b'sid\0\x02\0{}', 'not bytes'])
def test_decoder_rejects_malformed_headers(payload):
    with pytest.raises(ProtocolError):
        unpack_history_context(payload)


def test_snapshot_budget_preserves_utf8_and_sgr_without_base64_expansion():
    raw = b'\x1b[31m' + '中文'.encode() * 1000 + b'Z'
    with patch.object(protocol, 'MAX_FRAME', 512):
        frame = pack_history_context('aabbccdd', metadata(), raw)
        assert len(frame) <= 516
        _, context, tail = unpack_history_context(unpack(frame)[1])
        assert context['partial'] and context['reason'] == 'history_ring_truncated'
        assert tail.decode('utf-8').endswith('Z')
        assert tail.startswith(b'\x1b[0;31m')
        with pytest.raises(ProtocolError):
            pack_history_context('aabbccdd', metadata('output'), raw)


def test_epoch_snapshot_does_not_reinterpret_earlier_grids_and_legacy_is_unchanged():
    pump = OutputPump()
    pump.mark_grid(188, 54)
    pump.remember(b'\x1b[31mFIRST-GRID')
    legacy = pump.snapshot()
    pump.begin_resize()
    pump.mark_grid(113, 31)
    assert pump.snapshot() == legacy
    pump.remember(b'SECOND-GRID')
    data, end, context = pump.history_snapshot_position()
    assert b'FIRST-GRID' not in data and data.endswith(b'SECOND-GRID')
    assert b'31m' in data
    assert context == metadata(cols=113, rows=31, epoch=1, partial=True, reason='resize_boundary')
    assert end == len(b'\x1b[31mFIRST-GRIDSECOND-GRID')
    pump.mark_grid(188, 54)
    pump.remember(b'THIRD-GRID')
    data, _, context = pump.history_snapshot_position()
    assert context['epoch'] == 2 and context['partial']
    assert data.endswith(b'THIRD-GRID') and b'SECOND-GRID' not in data
    assert b'FIRST-GRIDSECOND-GRIDTHIRD-GRID' in pump.snapshot()


def test_preexisting_unlabelled_bytes_are_not_guessed_into_the_current_grid():
    pump = OutputPump()
    pump.remember(b'old unknown grid')
    before = pump.snapshot()
    pump.mark_grid(113, 31)
    data, _, context = pump.history_snapshot_position()
    assert context['partial'] and context['reason'] == 'legacy_history_without_grid'
    assert b'old unknown grid' not in data
    assert pump.snapshot() == before


def test_both_rings_are_bounded_and_live_stream_does_not_become_partial_just_from_crop():
    pump = OutputPump()
    pump.mark_grid(100, 28)
    with patch('supermonoagent.pump.RING_BYTES', 64):
        pump.remember(b'\x1b[32m' + '中文'.encode() * 100)
        assert len(pump._ring) <= 64 and len(pump._epoch_ring) <= 64
        data, _, context = pump.history_snapshot_position()
        assert len(data) <= 64 and context['partial']
        data.decode('utf-8')
        assert pump.history_context()['partial'] is False


def test_pending_batches_crossing_resize_never_claim_complete():
    pump, loop, flushed = OutputPump(), Loop(), []
    pump.mark_grid(188, 54)
    pump.add(b'old frame', loop, lambda data, overflow: flushed.append(data))
    pump.begin_resize()
    pump.mark_grid(113, 31)
    pump.add(b'new frame', loop, lambda data, overflow: flushed.append(data))
    loop.fire()
    assert flushed == [b'old framenew frame']
    assert pump.flushed_history_context() == metadata('output', cols=113, rows=31,
                                                    epoch=1, partial=True, reason='mixed_epoch_output')


def test_legacy_and_opt_in_clients_get_distinct_atomic_snapshot_and_live_frames():
    hub, modern, session = hub_fixture()
    legacy = hub.attach(asyncio.Queue(32), token='owner')
    legacy.multiplex = True
    legacy.subscriptions.add(session.id)
    session.pump.remember(b'old\r\n')
    assert hub._session_snapshot(legacy, session) == pack_feed(OP_SNAPSHOT, session.id, session.pump.snapshot())
    new = hub._session_snapshot(modern, session)
    assert unpack(new)[0] == OP_HISTORY_CONTEXT
    assert unpack_history_context(unpack(new)[1])[2] == b'old\r\n'
    assert 'history_epoch' not in hub.controls(legacy)[session.id]
    assert hub.controls(modern)[session.id]['history_epoch'] == 0
    loop = Loop()
    session.pump.add(b'live', loop, lambda data, overflow: hub._flushed(session.id, data, overflow))
    loop.fire()
    assert modern.queue.qsize() == legacy.queue.qsize() == 1
    assert unpack(legacy.queue.get_nowait()) == (OP_OUTPUT, session.id.encode() + b'\0live')
    sid, context, data = unpack_history_context(unpack(modern.queue.get_nowait())[1])
    assert (sid, context['kind'], data) == (session.id, 'output', b'live')


def test_snapshot_feed_offset_deduplicates_pending_live_bytes_and_revocation_blocks_new_frames():
    hub, client, session = hub_fixture()
    loop = Loop()
    session.pump.add(b'already in snapshot', loop, lambda data, overflow: hub._flushed(session.id, data, overflow))
    hub.refresh_subscriptions(client, [session.id])
    assert client.queue.qsize() == 1
    loop.fire()
    assert client.queue.qsize() == 1
    client.queue.get_nowait()
    client.authorize = lambda: False
    hub.refresh_subscriptions(client, [session.id])
    session.pump.add(b'private after revoke', loop, lambda data, overflow: hub._flushed(session.id, data, overflow))
    loop.fire()
    assert client.queue.empty()


def test_resize_in_progress_and_detach_cannot_claim_complete_or_reacquire_lease():
    hub, client, session = hub_fixture()
    entered, released = threading.Event(), threading.Event()

    def resizing(cols, rows):
        entered.set()
        assert released.wait(3)

    session.pty.resize = resizing
    result = []
    thread = threading.Thread(target=lambda: result.append(hub.resize_session(client, session.id, 113, 31)))
    thread.start()
    assert entered.wait(3)
    try:
        assert session.pump.history_context()['partial']
        session.pump.remember(b'during resize')
        assert session.pump.history_snapshot_position()[2]['partial']
        hub.detach(client.key)
    finally:
        released.set()
        thread.join(3)
    assert result == [False]
    assert (session.cols, session.rows) == (113, 31)
    assert session.resize_owner is None
    assert session.pump.history_context()['partial']


def test_real_websocket_negotiation_direct_and_native_relay_and_footer():
    asyncio.run(_real_websocket_checks())


async def _real_websocket_checks():
    async with isolated_host() as (daemon, direct_url, relay_url):
        window = daemon.hub.open_window(owner=daemon.token)
        session = daemon.hub.open_cli(window.id, 'cmd')
        session.pump.remember(b'\x1b[31mbounded replay')
        for relay in (False, True):
            headers = {} if relay else {'Authorization': 'Bearer integration-owner'}
            async with connect(relay_url if relay else direct_url, additional_headers=headers) as ws:
                if relay:
                    await ws.send(json.dumps({'op': 'phone', 'code': 'local-test', 'token': 'integration-owner', 'reliable': True}))
                    assert json.loads(await ws.recv())['op'] == 'ready'
                await ws.send(pack_json(OP_HELLO, {'token': 'integration-owner', 'multiplex': True,
                                                  'reliable': True, 'history_context': 1}))
                hello = json.loads(await receive_op(ws, OP_HELLO_OK))
                assert hello['capabilities']['history_context'] == 1
                await ws.send(pack_json(OP_SUBSCRIBE, {'sessions': [session.id]}))
                sid, context, data = unpack_history_context(await receive_op(ws, OP_HISTORY_CONTEXT))
                assert sid == session.id and context['kind'] == 'snapshot'
                assert b'bounded replay' in data
                session.pty.inbox.put(b'fresh')
                _, context, data = unpack_history_context(await receive_op(ws, OP_HISTORY_CONTEXT))
                assert context['kind'] == 'output' and data == b'fresh'

        queue = asyncio.Queue(32)
        client = daemon.hub.attach(queue, token=daemon.token)
        client.multiplex = True
        client.subscriptions.add(session.id)
        for offered in (True, '1', 2):
            await daemon._op(client, OP_HELLO, json.dumps({'multiplex': True, 'history_context': offered}).encode())
            assert client.history_context is False
        await daemon._op(client, OP_HELLO, b'{"multiplex":false,"history_context":1}')
        assert client.history_context is False
        await daemon._op(client, OP_HELLO, b'{"multiplex":true,"history_context":1}')
        assert client.history_context is True
        calls = []
        session.pty.footer_patch = lambda cols, rows: calls.append((cols, rows)) or b'footer'
        snapshot = daemon.hub._session_snapshot(client, session)
        supplement = await daemon.snapshot_supplement(client, snapshot)
        assert unpack(supplement) == (OP_OUTPUT, session.id.encode() + b'\0footer')
        assert calls == [(session.cols, session.rows)]
        session.pump.begin_resize()
        session.pump.mark_grid(113, 31)
        session.pump.mark_grid(session.cols, session.rows)
        assert await daemon.snapshot_supplement(client, snapshot) is None
        revoked_snapshot = daemon.hub._session_snapshot(client, session)
        daemon.hub.detach(client.key)
        assert await daemon.snapshot_supplement(client, revoked_snapshot) is None


def test_resume_metadata_keeps_v1_schema_strict():
    value = metadata('resume', v=2, end_position=7, stream_id='a' * 24)
    assert unpack_history_context(unpack(pack_history_context('sid', value, b'\x80\x1b['))[1]) == ('sid', value, b'\x80\x1b[')
    for invalid in (metadata(end_position=0), metadata('resume'), metadata(v=2),
                    {**value, 'end_position': True}, {**value, 'end_position': -1},
                    {**value, 'end_position': 9007199254740992},
                    {**value, 'stream_id': 'a' * 23}, {**value, 'stream_id': 'Z' * 24},
                    {key: item for key, item in value.items() if key != 'stream_id'}):
        with pytest.raises(ProtocolError):
            pack_history_context('sid', invalid, b'')


@pytest.mark.parametrize('position', range(12))
def test_resume_returns_exact_bytes_even_inside_utf8_and_csi(position):
    pump = OutputPump();pump.mark_grid(80, 24)
    raw = '中'.encode() + b'\x1b[31mtext'
    pump.remember(raw)
    data, end, context = pump.history_resume_position(position, 0, pump.stream_id)
    assert data == raw[position:] and end == len(raw)
    assert context == metadata('resume', v=2, cols=80, rows=24, end_position=len(raw), stream_id=pump.stream_id)


def test_resume_empty_barrier_and_epoch_or_retention_gap_fallback():
    pump = OutputPump();pump.mark_grid(80, 24)
    pump.remember(b'old')
    assert pump.history_resume_position(3, 0, pump.stream_id)[0:2] == (b'', 3)
    assert pump.history_resume_position(3, 0, pump.stream_id)[2]['kind'] == 'resume'
    with patch('supermonoagent.pump.RING_BYTES', 8):
        pump.remember(b'1234567890')
    data, end, context = pump.history_resume_position(0, 0, pump.stream_id)
    assert data.endswith(b'34567890') and end == 13
    assert context['kind'] == 'snapshot' and context['reason'] == 'history_ring_truncated'
    assert pump.history_resume_position(14, 0, pump.stream_id)[2]['kind'] == 'snapshot'
    pump.mark_grid(90, 25);pump.remember(b'new')
    data, end, context = pump.history_resume_position(13, 0, pump.stream_id)
    assert data.endswith(b'new') and end == 16
    assert context['kind'] == 'snapshot' and context['reason'] == 'resize_boundary'


def test_reused_session_id_cannot_resume_another_producer_with_matching_position_and_epoch():
    hub, client, session = hub_fixture()
    client.history_resume = True;client.subscriptions.clear()
    session.pump.remember(b'old')
    previous_stream = session.pump.stream_id
    session.pump = OutputPump();session.pump.mark_grid(session.cols, session.rows)
    session.pump.remember(b'new')
    assert session.pump.stream_id != previous_stream
    hub.set_subscriptions(client, [session.id], history_after={session.id: {
        'position': 3, 'epoch': 0, 'stream_id': previous_stream}})
    _, context, data = unpack_history_context(unpack(client.queue.get_nowait())[1])
    assert context['kind'] == 'snapshot' and data == b'new'
    assert context['end_position'] == 3 and context['epoch'] == 0
    assert context['stream_id'] == session.pump.stream_id


def test_resume_one_shot_pending_queue_and_manual_refresh_and_live_deduplication():
    hub, client, session = hub_fixture()
    client.history_resume = True
    client.subscriptions.clear()
    client.queue = asyncio.Queue(2)
    client.queue.put_nowait(b'occupied')
    loop = Loop()
    session.pump.add(b'old\xe4\xb8\xad\x1b[31m', loop, lambda data, overflow: hub._flushed(session.id, data, overflow))
    hub.set_subscriptions(client, [session.id], history_after={session.id: {'position': 4, 'epoch': 0, 'stream_id': session.pump.stream_id}})
    assert set(client.pending_replays) == {session.id}
    session.pump.add(b'new', loop, lambda data, overflow: hub._flushed(session.id, data, overflow))
    client.queue.get_nowait();hub.pump_replays(client, draining=True)
    _, context, data = unpack_history_context(unpack(client.queue.get_nowait())[1])
    assert context['kind'] == 'resume' and data == b'\xb8\xad\x1b[31mnew'
    assert context['end_position'] == session.pump.position()
    assert not client.history_after
    loop.fire()
    assert client.queue.empty()
    session.pump.add(b'live', loop, lambda data, overflow: hub._flushed(session.id, data, overflow))
    loop.fire()
    _, context, data = unpack_history_context(unpack(client.queue.get_nowait())[1])
    assert context['kind'] == 'output' and data == b'live' and context['end_position'] == session.pump.position()
    hub.refresh_subscriptions(client, [session.id])
    assert unpack_history_context(unpack(client.queue.get_nowait())[1])[1]['kind'] == 'snapshot'


def test_unauthorized_resume_cursor_never_queues_or_retains_private_bytes():
    hub, client, session = hub_fixture()
    client.history_resume = True
    client.token = 'intruder';client.subscriptions.clear()
    session.pump.remember(b'private')
    hub.set_subscriptions(client, [session.id], history_after={session.id: {'position': 0, 'epoch': 0, 'stream_id': session.pump.stream_id}})
    assert client.queue.empty() and not client.subscriptions and not client.history_after


def test_resume_exceeding_frame_budget_falls_back_to_snapshot():
    hub, client, session = hub_fixture()
    client.history_resume = True;client.subscriptions.clear()
    session.pump.remember(b'x' * 1000)
    with patch('supermonoagent.protocol.MAX_FRAME', 240):
        hub.set_subscriptions(client, [session.id], history_after={session.id: {'position': 0, 'epoch': 0, 'stream_id': session.pump.stream_id}})
    _, context, data = unpack_history_context(unpack(client.queue.get_nowait())[1])
    assert context['kind'] == 'snapshot' and context['partial'] and context['end_position'] == 1000
    assert len(data) < 1000


def test_queued_resume_refresh_replaces_cursor_and_activity_only_ignores_cursor():
    hub, client, session = hub_fixture()
    client.history_resume = True;client.subscriptions.clear()
    client.queue = asyncio.Queue(2);client.queue.put_nowait(b'occupied')
    session.pump.remember(b'old')
    cursor = {session.id: {'position': 3, 'epoch': 0, 'stream_id': session.pump.stream_id}}
    hub.set_subscriptions(client, [session.id], history_after=cursor)
    hub.refresh_subscriptions(client, [session.id])
    assert not client.history_after
    client.queue.get_nowait();hub.pump_replays(client, draining=True)
    assert unpack_history_context(unpack(client.queue.get_nowait())[1])[1]['kind'] == 'snapshot'
    hub.set_subscriptions(client, [])
    hub.set_subscriptions(client, [session.id], replay=False, history_after=cursor)
    assert client.queue.empty() and not client.history_after
    loop = Loop()
    session.pump.add(b'fresh', loop, lambda data, overflow: hub._flushed(session.id, data, overflow))
    loop.fire()
    _, context, data = unpack_history_context(unpack(client.queue.get_nowait())[1])
    assert context['v'] == 2 and context['kind'] == 'output' and data == b'fresh'


def test_real_resume_direct_native_relay_negotiation_validation_and_footer():
    asyncio.run(_real_resume_checks())


async def _real_resume_checks():
    async with isolated_host() as (daemon, direct_url, relay_url):
        window = daemon.hub.open_window(owner=daemon.token)
        session = daemon.hub.open_cli(window.id, 'cmd')
        session.pump.remember(b'old\xe4\xb8\xad\x1b[31m')
        for relay in (False, True):
            headers = {} if relay else {'Authorization': 'Bearer integration-owner'}
            async with connect(relay_url if relay else direct_url, additional_headers=headers) as ws:
                if relay:
                    await ws.send(json.dumps({'op': 'phone', 'code': 'local-test', 'token': 'integration-owner', 'reliable': True}))
                    assert json.loads(await ws.recv())['op'] == 'ready'
                await ws.send(pack_json(OP_HELLO, {'token': 'integration-owner', 'multiplex': True,
                                                  'history_context': 1, 'history_resume': 1}))
                hello = json.loads(await receive_op(ws, OP_HELLO_OK))
                assert hello['capabilities']['history_context'] == hello['capabilities']['history_resume'] == 1
                await ws.send(pack_json(OP_SUBSCRIBE, {'sessions': [session.id],
                                                       'history_after': {session.id: {'position': 4, 'epoch': 0, 'stream_id': session.pump.stream_id}}}))
                _, context, data = unpack_history_context(await receive_op(ws, OP_HISTORY_CONTEXT))
                assert context['v'] == 2 and context['kind'] == 'resume' and data.startswith(b'\xb8\xad\x1b[31m')
                assert context['stream_id'] == session.pump.stream_id
                cursor = {'position': context['end_position'], 'epoch': context['epoch'], 'stream_id': context['stream_id']}
                await ws.send(pack_json(OP_SUBSCRIBE, {'sessions': []}))
                await ws.send(pack_json(OP_SUBSCRIBE, {'sessions': [session.id], 'history_after': {session.id: cursor}}))
                _, barrier, data = unpack_history_context(await receive_op(ws, OP_HISTORY_CONTEXT))
                assert barrier['kind'] == 'resume' and barrier['stream_id'] == cursor['stream_id'] and data == b''
                session.pty.inbox.put(b'live')
                _, context, data = unpack_history_context(await receive_op(ws, OP_HISTORY_CONTEXT))
                assert context['kind'] == 'output' and data == b'live' and context['end_position'] == session.pump.position()

        client = daemon.hub.attach(asyncio.Queue(32), token=daemon.token)
        for body in ({'multiplex': True, 'history_resume': 1},
                     {'multiplex': False, 'history_context': 1, 'history_resume': 1},
                     {'multiplex': True, 'history_context': 1, 'history_resume': True}):
            await daemon._op(client, OP_HELLO, json.dumps(body).encode())
            assert not client.history_resume
        await daemon._op(client, OP_HELLO, b'{"multiplex":true,"history_context":1,"history_resume":1}')
        for invalid in ([], {'sid': {'position': True, 'epoch': 0}}, {'sid': {'position': 0}},
                        {'sid': {'position': 0, 'epoch': -1}},
                        {'sid': {'position': 0, 'epoch': 0, 'stream_id': 'g' * 24}},
                        {'sid': {'position': 0, 'epoch': 0, 'stream_id': 'a' * 25}},
                        {'\ud800': {'position': 0, 'epoch': 0}},
                        {str(i): {'position': 0, 'epoch': 0} for i in range(257)}):
            with pytest.raises(ProtocolError):
                await daemon._op(client, OP_SUBSCRIBE, json.dumps({'sessions': [session.id], 'history_after': invalid}).encode())
        await daemon._op(client, OP_SUBSCRIBE, json.dumps({'sessions': [session.id],
                         'history_after': {session.id: {'position': session.pump.position(), 'epoch': 0, 'stream_id': session.pump.stream_id}}}).encode())
        frame = client.queue.get_nowait()
        assert unpack_history_context(unpack(frame)[1])[2] == b''
        position = session.pump.position()
        session.pty.footer_patch = lambda cols, rows: b'footer'
        assert unpack(await daemon.snapshot_supplement(client, frame))[0] == OP_OUTPUT
        assert session.pump.position() == position
        session.pump.mark_grid(113, 31);session.pump.mark_grid(session.cols, session.rows)
        assert await daemon.snapshot_supplement(client, frame) is None
        client.history_after[session.id] = {'position': position, 'epoch': 2, 'stream_id': session.pump.stream_id}
        frame = daemon.hub._session_snapshot(client, session)
        daemon.hub.detach(client.key)
        assert await daemon.snapshot_supplement(client, frame) is None
