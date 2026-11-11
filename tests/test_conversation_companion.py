import asyncio

import pytest

from supermonoagent.conversation_companion import LegacyJournalCompanion
from supermonoagent.conversation_contract import JournalError
from supermonoagent.conversation_store import JournalStore
from supermonoagent.history_context import pack_history_context
from supermonoagent.protocol import OP_HELLO_OK, OP_SESSIONS, pack_json


def context(kind, end, stream='a'*24, epoch=1, reason=''):
    return dict(v=2,kind=kind,end_position=end,stream_id=stream,epoch=epoch,
        cols=80,rows=24,partial=bool(reason),reason=reason)


def catalog(**extra):
    return dict(capabilities=dict(history_context=1,history_resume=1,history_chunks=1),
        windows=[dict(id='w',title='same',sessions=[dict(id='s',shell='pwsh')])],**extra)


def setup(tmp_path):
    token = tmp_path/'token'
    token.write_text('private-token')
    store = JournalStore(tmp_path/'journals')
    companion = LegacyJournalCompanion('http://127.0.0.1:6789',token,store,
        owner_scope='owner',room='room',state_dir=tmp_path/'companion')
    companion.start()
    companion.receive(pack_json(OP_HELLO_OK,catalog()))
    return store, companion, token


def test_bootstrap_partial_then_exact_live_dedup_and_epoch_preserves_journal(tmp_path):
    store, companion, _ = setup(tmp_path)
    companion.receive(pack_history_context('s',context('snapshot',10),b'old'))
    raw = '中文🙂'.encode()+b'\x1b[31m\x00private-token'
    end = 10+len(raw)
    companion.receive(pack_history_context('s',context('output',end),raw))
    companion.receive(pack_history_context('s',context('resume',end),raw))
    companion.receive(pack_history_context('s',context('output',end+1,epoch=2),b'x'))
    journals = store.list_journals('owner')['journals']
    assert len(journals)==1 and journals[0]['coverage']=='partial'
    events = store.read_page('owner',journals[0]['journal_id'])['events']
    assert b''.join(store.content_bytes(journals[0]['journal_id'],e['content_id']) for e in events if e['kind']=='raw_output')==raw+b'x'
    companion.close()
    assert store.status('owner',journals[0]['journal_id'])['terminal_state']=='running'
    store.close()


def test_restart_uses_cursor_gaps_and_no_false_end_on_catalog_loss(tmp_path):
    store, companion, token = setup(tmp_path)
    companion.receive(pack_history_context('s',context('snapshot',3),b'old'))
    journal = store.list_journals('owner')['journals'][0]['journal_id']
    companion.close()
    newer = LegacyJournalCompanion('http://127.0.0.1:6789',token,store,
        owner_scope='owner',room='room',state_dir=tmp_path/'companion')
    newer.start()
    newer.receive(pack_json(OP_HELLO_OK,catalog()))
    assert newer.subscription()['history_after']['s']['position']==3
    newer.receive(pack_history_context('s',context('output',10),b'new'))
    newer.receive(pack_json(OP_SESSIONS,dict(capabilities={},windows=[])))
    assert store.status('owner',journal)['terminal_state']=='running'
    assert any(e['kind']=='capture_gap' for e in store.read_page('owner',journal)['events'])
    newer.close()
    store.close()


def test_instance_lock_native_handoff_and_no_remote_default(tmp_path):
    store, companion, token = setup(tmp_path)
    duplicate = LegacyJournalCompanion('http://127.0.0.1:6789',token,store,
        owner_scope='owner',state_dir=tmp_path/'companion')
    with pytest.raises(JournalError): duplicate.start()
    companion.receive(pack_json(OP_HELLO_OK,dict(capabilities=dict(conversation_journal=1),windows=[])))
    assert companion.status()['native_capture'] is True
    assert companion.subscription() is None
    assert not store.list_journals('owner')['journals']
    with pytest.raises(ValueError): LegacyJournalCompanion('https://remote.example',token,store,owner_scope='owner')
    companion.close()
    store.close()


def test_fake_running_backend_observed_without_any_terminal_commands(tmp_path):
    store, companion, _ = setup(tmp_path)
    sent = []
    class Socket:
        async def send(self,value): sent.append(value)
        def __aiter__(self): return self
        async def __anext__(self):
            if not hasattr(self,'seen'):
                self.seen=True
                return pack_json(OP_HELLO_OK,catalog())
            stop.set()
            raise StopAsyncIteration
    class Connection:
        async def __aenter__(self): return Socket()
        async def __aexit__(self,*args): pass
    stop=asyncio.Event()
    asyncio.run(companion.run(stop,connector=lambda *args,**kw:Connection()))
    from supermonoagent.protocol import unpack,OP_HELLO,OP_SUBSCRIBE
    assert [unpack(frame)[0] for frame in sent]==[OP_HELLO,OP_SUBSCRIBE]
    store.close()


def test_crash_after_raw_commit_recovers_authoritative_cursor(tmp_path):
    store,companion,token=setup(tmp_path)
    companion.receive(pack_history_context('s',context('snapshot',3),b'old'))
    jid=store.list_journals('owner')['journals'][0]['journal_id']
    # Simulate commit succeeding and cursor-file replace never happening.
    store.append_raw(jid,b'exact',source_record_id='legacy:'+'a'*24+':3:8',
        metadata=dict(start_position=3,end_position=8))
    companion.close()
    resumed=LegacyJournalCompanion('http://127.0.0.1:6789',token,store,
        owner_scope='owner',room='room',state_dir=tmp_path/'companion')
    resumed.start()
    resumed.receive(pack_json(OP_HELLO_OK,catalog()))
    assert resumed.subscription()['history_after']['s']['position']==8
    resumed.receive(pack_history_context('s',context('resume',8,epoch=2),b'exact'))
    events=store.read_page('owner',jid)['events']
    assert len([event for event in events if event['kind']=='raw_output'])==1
    resumed.close()
    store.close()


def test_new_stream_generation_gets_distinct_journal_without_sealing_old(tmp_path):
    store,companion,_=setup(tmp_path)
    companion.receive(pack_history_context('s',context('snapshot',3),b'old'))
    first=store.list_journals('owner')['journals'][0]['journal_id']
    companion.receive(pack_history_context('s',context('snapshot',3,stream='b'*24),b'new'))
    journals=store.list_journals('owner')['journals']
    assert len(journals)==2
    assert store.status('owner',first)['terminal_state']=='running'
    companion.close()
    store.close()


def test_actual_daemon_hello_negotiates_native_capture_handoff(tmp_path):
    from test_client_integration import isolated_host
    async def run():
        async with isolated_host() as (daemon,direct,_relay):
            assert daemon.journals is not None
            window=daemon.hub.open_window(owner='integration-owner')
            target=daemon.hub.open_cli(window.id,'cmd')
            token=tmp_path/'owner-token'
            token.write_text('integration-owner')
            store=JournalStore(tmp_path/'companion-journals')
            companion=LegacyJournalCompanion(direct,token,store,owner_scope='owner',state_dir=tmp_path/'companion')
            stop=asyncio.Event()
            task=asyncio.create_task(companion.run(stop))
            try:
                async with asyncio.timeout(5):
                    while not companion.status()['native_capture']: await asyncio.sleep(.01)
                clients=[client for client in daemon.hub.clients.values() if client.history_chunks]
                assert len(clients)==1 and clients[0].conversation_journal is True
                assert not clients[0].subscriptions
                assert not store.list_journals('owner')['journals']
                assert not target.pty.written
            finally:
                stop.set()
                await task
                store.close()
    asyncio.run(run())
