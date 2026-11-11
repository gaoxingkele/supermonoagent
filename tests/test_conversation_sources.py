import asyncio
import json
import os
import sqlite3
import stat
import subprocess
import threading

import pytest

from supermonoagent.conversation_contract import JournalError
from supermonoagent.conversation_store import JournalStore
from supermonoagent.conversation_sources import (SourceRegistry, CodexPassiveReader,
    normalize_codex_turn, normalize_claude_record, classify_claude_result)


def binding(provider='codex'):
    return dict(binding_id='binding', provider=provider, provider_session_id='root', provider_version='0.167.0')


def test_codex_final_requires_phase_and_same_completed_turn():
    items = [dict(id='comment', type='agentMessage', phase='commentary', text='progress'),
             dict(id='answer', type='agentMessage', phase='final_answer', text='业务失败\n```py\nx=1\n```'),
             dict(id='thought', type='reasoning', text='private'),
             dict(id='unknown', type='agentMessage', text='not final')]
    events = normalize_codex_turn(binding(), dict(id='turn', status='completed'), items)
    assert [(e['kind'], text) for e, text in events if e['kind'] == 'assistant_final'] == [
        ('assistant_final', items[1]['text'])]
    assert not any(e['kind'] == 'assistant_final' for e, _ in normalize_codex_turn(
        binding(), dict(id='turn', status='interrupted'), items))
    assert all(text != 'private' for _, text in events)


def test_source_ids_preserve_repeated_prompts_and_unknown_provenance():
    items = [dict(type='userMessage', id='one', content=[dict(type='text', text='again')], origin='human'),
             dict(type='userMessage', id='two', content=[dict(type='text', text='again')], origin='human'),
             dict(type='userMessage', id='unknown', content=[dict(type='text', text='scheduled')])]
    rows = normalize_codex_turn(binding(), dict(id='turn', status='completed'), items)
    prompts = [(e, t) for e, t in rows if e['kind'] == 'user_input']
    assert len({e['source_record_id'] for e, _ in prompts}) == 3
    assert prompts[2][0]['actor'] == 'unknown'
    assert prompts[2][0]['state'] == 'observed'


def test_claude_tool_user_meta_sidechain_and_stop_are_not_human_or_final():
    base = dict(type='user', sessionId='root', uuid='a', message=dict(content=[dict(type='tool_result', content='tool')]))
    assert normalize_claude_record(binding('claude-code'), base) == []
    base['message']['content'] = 'schedule'
    base['isMeta'] = True
    assert normalize_claude_record(binding('claude-code'), base)[0][0]['actor'] == 'unknown'
    base['isSidechain'] = True
    assert normalize_claude_record(binding('claude-code'), base) == []
    stop = dict(hook_event_name='Stop', session_id='root', prompt_id='p', last_assistant_message='candidate')
    rows = normalize_claude_record(binding('claude-code'), stop)
    assert rows[0][0]['kind'] == 'assistant_candidate'
    assert rows[0][0]['state'] == 'candidate'
    assert normalize_claude_record(binding('claude-code'), {**stop, 'hook_event_name':'SubagentStop'}) == []
    assert not classify_claude_result(dict(type='result', subtype='success', is_error=False, num_turns=0))


def make_source(home):
    home.mkdir()
    source = home / 'root.jsonl'
    source.write_text(json.dumps(dict(type='session_meta', payload=dict(id='root', source='cli')))+'\n', encoding='utf8')
    with sqlite3.connect(home / 'state_5.sqlite') as db:
        db.execute('CREATE TABLE threads(id TEXT PRIMARY KEY, rollout_path TEXT, cwd TEXT, cli_version TEXT, source TEXT, created_at INTEGER)')
        db.executemany('INSERT INTO threads VALUES(?,?,?,?,?,?)', [
            ('root', str(source), 'project', '0.167.0', '"cli"', 1),
            ('child', str(source), 'project', '0.167.0', '{"subagent":{"parent_thread_id":"root"}}', 2)])
    with sqlite3.connect(home / 'thread_history_1.sqlite') as db:
        for table in ['thread_turns','thread_items','thread_history_projection_state','thread_realtime_items']:
            db.execute(f'CREATE TABLE {table}(thread_id TEXT, turn_id TEXT)')
    return source


def test_registry_explicit_root_generation_and_exclusive_binding(tmp_path):
    home = tmp_path / 'codex'
    make_source(home)
    store = JournalStore(tmp_path / 'journals')
    one = store.create('owner', room='room',window_id='w',session_id='s',source_epoch='epoch',process_generation='pid-1')['journal_id']
    two = store.create('owner', room='room',window_id='w',session_id='s2',source_epoch='epoch',process_generation='pid-2')['journal_id']
    registry = SourceRegistry(store, tmp_path / 'registry', codex_home=home)
    catalog = registry.list_sources('codex', project_root='PROJECT\\')
    assert len(catalog) == 1
    assert 'rollout_path' not in json.dumps(catalog)
    ref = catalog[0]['source_ref']
    with pytest.raises(JournalError): registry.bind('owner', one, ref, terminal_generation='reused-pid')
    bound = registry.bind('owner', one, ref, terminal_generation='pid-1')
    assert bound['provider'] == 'codex' and 'capability' not in bound
    with pytest.raises(JournalError): registry.bind('owner', two, ref, terminal_generation='pid-2')
    with pytest.raises(JournalError): registry.receive_hook(bound['binding_id'], 'bad', {'thread-id':'root'})
    registry.close()
    assert store.status('owner', one)['terminal_state'] == 'running'
    store.close()


def test_passive_rpc_is_paginated_private_and_exact_version(tmp_path):
    home = tmp_path / 'codex'
    original = make_source(home).read_bytes()
    calls = []
    class RPC:
        def __init__(self, binary, private):
            from supermonoagent.file_companion_manager import _private
            self.private = private
            _private(private,True)
            for copied in private.rglob('*'):
                _private(copied,copied.is_dir())
            assert not (private / 'auth.json').exists()
            db = sqlite3.connect(private / 'state_5.sqlite')
            try:
                assert len(db.execute('select * from threads').fetchall()) == 1
            finally:
                db.close()
        def rpc(self, method, params):
            calls.append(method)
            if method == 'initialize': return {}
            if method == 'thread/read': return dict(thread=dict(id='root'))
            if method == 'thread/turns/list':
                return dict(data=[dict(id='t', status='completed')], nextCursor=None)
            if method == 'thread/items/list':
                if params.get('cursor'): return dict(data=[dict(item=dict(id='f',type='agentMessage',phase='final_answer',text='final'))])
                return dict(data=[dict(item=dict(id='x',type='reasoning'))],nextCursor='next')
            raise AssertionError(method)
        def close(self): pass
    reader = CodexPassiveReader(home, tmp_path / 'private', rpc_factory=RPC,
        binary_resolver=lambda version: tmp_path / 'fake.exe', version_probe=lambda path:'0.167.0')
    rows = reader.read('root', expected_version='0.167.0')
    assert rows[0][1][-1]['text'] == 'final'
    assert calls.count('thread/items/list') == 2
    assert (home / 'root.jsonl').read_bytes() == original
    assert list((tmp_path / 'private').glob('passive-read-private-*')) == []
    with pytest.raises(JournalError): reader.read('root', expected_version='0.166.0')


def test_notify_is_hint_only_and_generation_is_authenticated(tmp_path):
    home=tmp_path/'codex'
    make_source(home)
    store=JournalStore(tmp_path/'journals')
    jid=store.create('owner',room='room',window_id='w',session_id='s',source_epoch='epoch',process_generation='pid')['journal_id']
    registry=SourceRegistry(store,tmp_path/'registry',codex_home=home)
    bound=registry.bind('owner',jid,registry.list_sources('codex')[0]['source_ref'],terminal_generation='pid')
    config=registry.prepare_hook_config(bound['binding_id'],'http://127.0.0.1:9876/hook')
    cap=json.loads(config.read_text())['capability']
    payload={'type':'agent-turn-complete','thread-id':'root','turn-id':'t',
        'input-messages':['old','old'],'last-assistant-message':'not canonical','_terminal_generation':'pid'}
    assert registry.receive_hook(bound['binding_id'],cap,payload)==dict(accepted=True,needs_reconcile=True,recorded=0)
    assert all(e['kind'] not in {'user_input','assistant_final'} for e in store.read_page('owner',jid)['events'])
    with pytest.raises(JournalError): registry.receive_hook(bound['binding_id'],cap,{**payload,'thread-id':'child'})
    with pytest.raises(JournalError): registry.receive_hook(bound['binding_id'],cap,{**payload,'_terminal_generation':'reused'})
    assert cap not in json.dumps(registry.status(jid))
    registry.close()
    store.close()


def test_frozen_known_final_can_arrive_after_eof_and_seal(tmp_path,monkeypatch):
    home=tmp_path/'codex'
    make_source(home)
    store=JournalStore(tmp_path/'journals',clock=lambda:1000)
    jid=store.create('owner',room='room',window_id='w',session_id='s',source_epoch='epoch',process_generation='pid')['journal_id']
    registry=SourceRegistry(store,tmp_path/'registry',codex_home=home,clock=lambda:1000)
    bound=registry.bind('owner',jid,registry.list_sources('codex')[0]['source_ref'],terminal_generation='pid')
    stage=['inProgress']
    class Reader:
        def __init__(self,*args,**kw): pass
        def read(self,*args,**kw):
            return [(dict(id='pending',status=stage[0]),[dict(id='known-item',type='agentMessage',phase='final_answer',text='late final')])]
    monkeypatch.setattr('supermonoagent.conversation_sources.CodexPassiveReader',Reader)
    assert asyncio.run(registry.reconcile(bound['binding_id']))['recorded']==0
    registry.terminal_end(jid)
    store.terminal_end(jid,pending=True)
    store.seal(jid,incomplete=True)
    stage[0]='completed'
    assert asyncio.run(registry.reconcile(bound['binding_id']))['recorded']==2
    assert registry.status(jid)['pending_turns']==0
    page=store.read_page('owner',jid,projection='conversation')
    assert page['events'][0]['kind']=='assistant_final'
    assert store.content_bytes(jid,page['events'][0]['content_id'])==b'late final'
    assert asyncio.run(registry.reconcile(bound['binding_id']))['recorded']==0
    registry.close()
    store.close()


def test_new_unfrozen_final_after_eof_does_not_clear_pending(tmp_path,monkeypatch):
    home=tmp_path/'codex'
    make_source(home)
    store=JournalStore(tmp_path/'journals',clock=lambda:1000)
    jid=store.create('owner',room='room',window_id='w',session_id='s',source_epoch='epoch',process_generation='pid')['journal_id']
    registry=SourceRegistry(store,tmp_path/'registry',codex_home=home,clock=lambda:1000)
    bound=registry.bind('owner',jid,registry.list_sources('codex')[0]['source_ref'],terminal_generation='pid')
    stage=['inProgress','known-item']
    class Reader:
        def __init__(self,*args,**kw): pass
        def read(self,*args,**kw):
            return [(dict(id='pending',status=stage[0]),[dict(id=stage[1],type='agentMessage',phase='final_answer',text='final')])]
    monkeypatch.setattr('supermonoagent.conversation_sources.CodexPassiveReader',Reader)
    asyncio.run(registry.reconcile(bound['binding_id']))
    registry.terminal_end(jid)
    store.terminal_end(jid,pending=True)
    store.seal(jid,incomplete=True)
    stage[:]=['completed','new-item']
    result=asyncio.run(registry.reconcile(bound['binding_id']))
    assert result['recorded']==0 and result['reason']=='invalid_repair'
    assert registry.status(jid)['pending_turns']==1
    assert not store.read_page('owner',jid,projection='conversation')['events']
    registry.close()
    store.close()


def test_claude_split_utf8_new_records_and_no_historical_import(tmp_path):
    home=tmp_path/'claude'
    project=home/'projects'/'project'
    project.mkdir(parents=True)
    path=project/'root.jsonl'
    initial=dict(type='user',sessionId='root',uuid='old',cwd='project',version='2.1.310',origin='human',message=dict(content='old'))
    path.write_text(json.dumps(initial)+'\n',encoding='utf8')
    store=JournalStore(tmp_path/'journals')
    jid=store.create('owner',room='room',window_id='w',session_id='s',source_epoch='epoch',process_generation='pid')['journal_id']
    registry=SourceRegistry(store,tmp_path/'registry',claude_home=home)
    bound=registry.bind('owner',jid,registry.list_sources('claude-code',project_root='project')[0]['source_ref'],terminal_generation='pid')
    message={**initial,'uuid':'new','message':dict(content='中文🙂')}
    encoded=(json.dumps(message,ensure_ascii=False)+'\n').encode()
    cut=encoded.index('中'.encode())+1
    with path.open('ab') as stream: stream.write(encoded[:cut])
    assert asyncio.run(registry.reconcile(bound['binding_id']))['recorded']==0
    with path.open('ab') as stream: stream.write(encoded[cut:])
    assert asyncio.run(registry.reconcile(bound['binding_id']))['recorded']==1
    events=store.read_page('owner',jid,projection='conversation')['events']
    assert len(events)==1 and store.content_bytes(jid,events[0]['content_id']).decode()=='中文🙂'
    assert asyncio.run(registry.reconcile(bound['binding_id']))['recorded']==0
    registry.close()
    store.close()


def test_private_reconciliation_roster_survives_restart_is_bounded_and_expires(tmp_path):
    home=tmp_path/'codex'
    make_source(home)
    now=[1000]
    store=JournalStore(tmp_path/'journals',clock=lambda:now[0])
    jid=store.create('owner',room='room',window_id='w',session_id='s',source_epoch='epoch',process_generation='pid')['journal_id']
    registry=SourceRegistry(store,tmp_path/'registry',codex_home=home,clock=lambda:now[0])
    bound=registry.bind('owner',jid,registry.list_sources('codex')[0]['source_ref'],terminal_generation='pid')
    template=registry._bindings[bound['binding_id']]
    # Synthetic persisted roster: no provider runtime or transcript involved.
    registry._bindings={f'b-{i}':{**template,'binding_id':f'b-{i}','active':False,
        'pending':{'known-turn':['known-source']},'repair_until':2000} for i in range(300)}
    registry._save()
    registry.close()
    restarted=SourceRegistry(store,tmp_path/'registry',codex_home=home,clock=lambda:now[0])
    first=restarted.reconciliation_targets()
    second=restarted.reconciliation_targets()
    assert len(first)==len(second)==256
    assert len({row['binding_id'] for row in first+second})==300
    assert all(set(row)=={'binding_id','journal_id','active','pending_turns','repair_until'} for row in first)
    assert not any(row['active'] for row in first)
    now[0]=2001
    assert restarted.reconciliation_targets()==[]
    restarted.close()
    store.close()


def test_cancelled_reconcile_joins_reader_before_owner_closes_storage(tmp_path,monkeypatch):
    home=tmp_path/'codex'
    make_source(home)
    store=JournalStore(tmp_path/'journals')
    jid=store.create('owner',room='room',window_id='w',session_id='s',source_epoch='epoch',process_generation='pid')['journal_id']
    registry=SourceRegistry(store,tmp_path/'registry',codex_home=home)
    bound=registry.bind('owner',jid,registry.list_sources('codex')[0]['source_ref'],terminal_generation='pid')
    entered=threading.Event()
    release=threading.Event()
    finished=threading.Event()
    class Reader:
        def __init__(self,*args,**kw): pass
        def read(self,*args,**kw):
            entered.set()
            assert release.wait(5)
            finished.set()
            return [(dict(id='t',status='completed'),[dict(id='answer',type='agentMessage',phase='final_answer',text='must not append')])]
    monkeypatch.setattr('supermonoagent.conversation_sources.CodexPassiveReader',Reader)
    async def run():
        task=asyncio.create_task(registry.reconcile(bound['binding_id']))
        while not entered.is_set(): await asyncio.sleep(.001)
        task.cancel()
        await asyncio.sleep(.03)
        assert not task.done()
        assert bound['binding_id'] in registry._inflight
        release.set()
        with pytest.raises(asyncio.CancelledError): await task
        assert finished.is_set()
        assert not registry._inflight
        assert not store.read_page('owner',jid,projection='conversation')['events']
        registry.close()
        store.close()
    asyncio.run(run())


def test_manager_eof_joins_existing_reader_before_freezing_and_sealing(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from supermonoagent.conversation_manager import ConversationManager
    home=tmp_path/'codex'
    make_source(home)
    entered,release=threading.Event(),threading.Event()
    reads=[]
    class Reader:
        def __init__(self,*args,**kw): pass
        def read(self,*args,**kw):
            reads.append('read')
            entered.set()
            assert release.wait(5)
            return [(dict(id='turn-at-eof',status='completed'),[dict(id='answer-at-eof',type='agentMessage',phase='final_answer',text='retained final')])]
    monkeypatch.setattr('supermonoagent.conversation_sources.CodexPassiveReader',Reader)
    async def run():
        manager=ConversationManager(tmp_path/'manager',default_token='owner-token',drain_seconds=0)
        manager.sources.close()
        manager.sources=SourceRegistry(manager.store,tmp_path/'registry',codex_home=home)
        target=SimpleNamespace(id='session',window_id='window',shell='codex',cwd='project',process_generation='pid')
        owner=manager.owner_scope('owner-token')
        jid=manager.open_session(target,owner_scope=owner,room='room')
        bound=manager.sources.bind(owner,jid,manager.sources.list_sources('codex')[0]['source_ref'],terminal_generation='pid')
        initial=asyncio.create_task(manager.sources.reconcile(bound['binding_id']))
        while not entered.is_set(): await asyncio.sleep(.001)
        finishing=asyncio.create_task(manager._finish(jid,'exited'))
        await asyncio.sleep(.04)
        assert not finishing.done()
        assert manager.store.status(owner,jid)['terminal_state']=='running'
        with pytest.raises(JournalError,match='reconcile_in_progress'):
            manager.sources.terminal_end(jid)
        release.set()
        await initial
        await finishing
        assert reads==['read']
        assert manager.store.status(owner,jid)['capture_state']=='sealed'
        events=manager.store.read_page(owner,jid,projection='conversation')['events']
        assert len(events)==1 and events[0]['kind']=='assistant_final'
        assert manager.store.content_bytes(jid,events[0]['content_id'])==b'retained final'
        await manager.aclose()
    asyncio.run(run())


def test_binding_rechecks_live_generation_after_provider_disk_reads(tmp_path,monkeypatch):
    home=tmp_path/'codex'
    make_source(home)
    store=JournalStore(tmp_path/'journals')
    jid=store.create('owner',room='room',window_id='w',session_id='s',source_epoch='epoch',process_generation='pid')['journal_id']
    registry=SourceRegistry(store,tmp_path/'registry',codex_home=home)
    ref=registry.list_sources('codex')[0]['source_ref']
    current=[True]
    original=registry.list_sources
    def ended_during_catalog(*args,**kwargs):
        result=original(*args,**kwargs)
        current[0]=False
        return result
    monkeypatch.setattr(registry,'list_sources',ended_during_catalog)
    with pytest.raises(JournalError,match='terminal_generation_mismatch'):
        registry.bind('owner',jid,ref,terminal_generation='pid',is_current=lambda:current[0])
    assert not registry.status(jid)['bound']
    assert not any(event['kind']=='binding' for event in store.read_page('owner',jid)['events'])
    registry.close()
    store.close()


def test_source_root_is_private_before_binding_capability_can_be_written(tmp_path,monkeypatch):
    from supermonoagent import conversation_sources as module
    store=JournalStore(tmp_path/'journals')
    calls=[]
    def refuse(path,directory=False):
        calls.append((path,directory,list(path.iterdir()) if directory else path.read_bytes()))
        raise ValueError('synthetic private details')
    monkeypatch.setattr(module,'_secure_storage',refuse)
    root=tmp_path/'source-private'
    with pytest.raises(JournalError,match='private_storage_unavailable'):
        SourceRegistry(store,root)
    assert calls==[(root,True,[])]
    assert not (root/'bindings.json').exists()
    store.close()


@pytest.mark.skipif(os.name!='nt',reason='Windows ACL assertion')
def test_existing_explicit_everyone_grants_removed_only_from_owned_source_state(tmp_path):
    from supermonoagent.file_companion_manager import _private,ManagerError
    home=tmp_path/'codex'
    provider_rollout=make_source(home)
    store=JournalStore(tmp_path/'journals')
    jid=store.create('owner',room='room',window_id='w',session_id='s',source_epoch='epoch',process_generation='pid')['journal_id']
    root=tmp_path/'registry'
    registry=SourceRegistry(store,root,codex_home=home)
    bound=registry.bind('owner',jid,registry.list_sources('codex')[0]['source_ref'],terminal_generation='pid')
    hook=registry.prepare_hook_config(bound['binding_id'],'http://127.0.0.1:9876/hook')
    registry.close()
    orphan=root/'snapshots'/'passive-read-private-orphan'
    (orphan/'sessions').mkdir(parents=True)
    copied=orphan/'sessions'/'copied.jsonl'
    copied.write_bytes(b'synthetic-private-copy')
    temporary=root/'bindings-crashed.tmp'
    temporary.write_bytes(b'synthetic-private-capability')
    unrelated=tmp_path/'unrelated.txt'
    unrelated.write_text('untouched')
    owned=[root,root/'bindings.json',root/'hooks',hook,root/'snapshots',orphan,orphan/'sessions',copied,temporary]
    for path in [*owned,provider_rollout,unrelated]:
        subprocess.run(['icacls',str(path),'/grant','*S-1-1-0:(RX)'],check=True,
            capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW)
        with pytest.raises(ManagerError): _private(path,path.is_dir())
    reopened=SourceRegistry(store,root,codex_home=home)
    for path in owned: _private(path,path.is_dir())
    # Original provider evidence and unrelated files are never permission-mutated.
    for path in [provider_rollout,unrelated]:
        with pytest.raises(ManagerError): _private(path)
    assert copied.read_bytes()==b'synthetic-private-copy'
    reopened.close()
    store.close()


@pytest.mark.skipif(os.name=='nt',reason='POSIX mode assertion')
def test_source_private_modes_repair_existing_owned_files(tmp_path):
    from supermonoagent.conversation_sources import private_directory,private_file
    root=tmp_path/'private'
    root.mkdir(mode=0o777)
    root.chmod(0o777)
    path=root/'bindings.json'
    path.write_text('{}')
    path.chmod(0o666)
    private_directory(root)
    private_file(path)
    assert stat.S_IMODE(root.stat().st_mode)==0o700
    assert stat.S_IMODE(path.stat().st_mode)==0o600


@pytest.mark.parametrize('provider', ['codex', 'claude-code'])
def test_source_catalog_parse_failure_is_explicit_and_private(tmp_path, provider):
    home = tmp_path / 'provider-private-path'
    home.mkdir()
    if provider == 'codex':
        (home / 'state_5.sqlite').write_bytes(b'private malformed database')
    else:
        project = home / 'projects' / 'project'
        project.mkdir(parents=True)
        (project / 'root.jsonl').write_text('private malformed JSON\n', encoding='utf8')
    with JournalStore(tmp_path / 'journals') as store:
        registry = SourceRegistry(store, tmp_path / 'registry', codex_home=home, claude_home=home)
        try:
            with pytest.raises(JournalError) as error:
                registry.list_sources(provider)
            assert str(error.value) == 'source_catalog_unavailable'
        finally:
            registry.close()


@pytest.mark.parametrize('provider', ['codex', 'claude-code'])
def test_source_catalog_valid_empty_is_not_unavailable(tmp_path, provider):
    home = tmp_path / 'provider'
    if provider == 'codex':
        make_source(home)
    else:
        home.mkdir()
    with JournalStore(tmp_path / 'journals') as store:
        registry = SourceRegistry(store, tmp_path / 'registry', codex_home=home, claude_home=home)
        try:
            assert registry.list_sources(provider, project_root='no-matching-project') == []
        finally:
            registry.close()
