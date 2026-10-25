"""Only owned catalogs and copied engines; no real library ledger execution."""
import ast
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import threading
import os
import subprocess
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from supermonoagent.mylib_evolution import EvolutionService, EvolutionError, _VALIDATOR, _run_process


ENGINE_SOURCE = Path('D:/aicoding/mylib/skills/meta-rsi')
FILES = ('scripts/metarsi.py', 'config/defaults.json', 'config/ideation.json')


def _service(*args, **kwargs):
    kwargs.setdefault("min_free_bytes", 0)
    return EvolutionService(*args, **kwargs)


class Catalog:
    def __init__(self, files=None):
        self.revision = 'a' * 40
        self.files = files or {'demo': '---\nname: demo\ndescription: owned sample\n---\n# Demo\n\nUse only owned test files.\n',
            'verified': '# Existing\n\n## Verification\n\nRun the owned checks.\n'}
        self.reads = []
    def list_skills(self, cursor=None, limit=50):
        return {'revision': self.revision, 'items': [{'id': key} for key in self.files], 'next_cursor': None}
    def read_skill(self, skill_id, revision=None):
        self.reads.append((skill_id, revision))
        text = self.files[skill_id]
        return {'revision': revision, 'skill_id': skill_id, 'path': 'skills/' + skill_id + '/SKILL.md', 'text': text,
                'sha256': hashlib.sha256(text.encode()).hexdigest(), 'truncated': False}


def copy_engine(destination):
    if not ENGINE_SOURCE.exists(): pytest.skip('Read-only source engine fixture unavailable')
    original = {}
    for name in FILES:
        source = ENGINE_SOURCE / name
        data = source.read_bytes()
        original[name] = hashlib.sha256(data).hexdigest()
        target = destination / name;target.parent.mkdir(parents=True, exist_ok=True);target.write_bytes(data)
    return original


def events(service, ledger=None):
    connection = sqlite3.connect(service.project / '.metarsi/metarsi.db')
    try:
        rows = connection.execute('SELECT ledger, data FROM events ORDER BY seq').fetchall()
        return [(kind, json.loads(data)) for kind, data in rows if ledger is None or kind == ledger]
    finally: connection.close()


def test_real_copied_engine_review_has_formal_artifact_without_activation(tmp_path):
    engine = tmp_path / 'source-engine';before = copy_engine(engine)
    catalog = Catalog();original = dict(catalog.files)
    service = _service(tmp_path/'state', engine, catalog)
    try:
        result = service.run_once()
        assert result['scanned'] == 2 and result['suggestions'] == 1 and result['ledger_verified']
        assert result['suggestion_count'] == result['suggestions']
        item = service.list_suggestions()['items'][0]
        assert item['validation']['passed'] is True
        assert item['validation']['scope'] == 'format_and_source_preservation_only'
        assert item['validation']['semantic_efficacy'] == 'unverified'
        assert item['admission'] == 'not_requested' and item['proposal']['applied'] is False
        artifact = service.project / item['validation']['artifact']
        data = artifact.read_bytes();check = json.loads(data)
        assert hashlib.sha256(data).hexdigest() == item['validation']['artifact_sha256']
        assert all(check['checks'].values())
        assert check['validator_sha256'] == hashlib.sha256(_VALIDATOR.encode()).hexdigest()
        assert check['engine_sha256'] == service.status()['engine']['sha256']
        assert check['source']['revision'] == catalog.revision
        trials = events(service, 'trials')
        assert len(trials) == 1 and trials[0][1]['kind'] == 'formal' and trials[0][1]['tier'] == 'formal'
        assert trials[0][1]['by'] == 'mylib-review-service'
        assert not events(service, 'admissions') and not events(service, 'shadows')
        assert catalog.files == original
        assert all(hashlib.sha256((engine/name).read_bytes()).hexdigest() == digest for name,digest in before.items())
        assert all(hashlib.sha256((ENGINE_SOURCE/name).read_bytes()).hexdigest() == digest for name,digest in before.items())
        assert not (engine/'.metarsi').exists() and not (ENGINE_SOURCE/'.metarsi').exists()
        assert service.status()['model_configured'] is False
    finally: service.close()


def test_same_snapshot_and_signal_are_idempotent_across_restart(tmp_path):
    engine = tmp_path/'engine';copy_engine(engine)
    catalog = Catalog()
    service = _service(tmp_path/'state', engine, catalog)
    try:
        first = service.record_signal('request', '--project=outside', 'Owned metadata, not CLI options.')
        assert not first['duplicate']
        second = service.record_signal('request', '--project=outside', 'Owned metadata, not CLI options.')
        assert second['duplicate'] and second['signal_id'] == first['signal_id']
        assert len(events(service, 'signals')) == 1
        service.run_once();before = events(service)
        assert service.run_once()['suggestions'] == 0
        assert events(service) == before
    finally: service.close()
    restarted = _service(tmp_path/'state', engine, catalog)
    try:
        assert restarted.run_once()['suggestions'] == 0
        assert restarted.record_signal('request', '--project=outside', 'Owned metadata, not CLI options.')['duplicate']
        assert restarted.list_suggestions()['count'] == 1
    finally: restarted.close()


@pytest.mark.parametrize('mutation', ['heading', 'checklist', 'source', 'semantic_limit'])
def test_independent_validator_rejects_mutants(tmp_path, mutation):
    source = b'# Owned source\n'
    candidate = source + b'\n## Verification\n\n- [ ] reproducible check artifact\n- [ ] semantic outcome falsifier\nhas not demonstrated improved task outcomes\n'
    (tmp_path/'source.md').write_bytes(source)
    (tmp_path/'source.json').write_text(json.dumps({'sha256': hashlib.sha256(source).hexdigest()}))
    (tmp_path/'candidate.md').write_bytes(candidate)
    command = [sys.executable, '-I', '-S', '-X', 'utf8', '-c', _VALIDATOR, str(tmp_path)]
    code, raw = _run_process(command, cwd=tmp_path)
    assert code == 0 and json.loads(raw)['passed'] is True
    if mutation == 'heading': candidate = candidate.replace(b'## Verification', b'Verification prose only')
    elif mutation == 'checklist': candidate = candidate.replace(b'- [ ]', b'- [x]')
    elif mutation == 'source': candidate = candidate.replace(b'Owned source', b'Altered source')
    else: candidate = candidate.replace(b'has not demonstrated improved task outcomes', b'improved outcomes')
    (tmp_path/'candidate.md').write_bytes(candidate)
    code, raw = _run_process(command, cwd=tmp_path)
    assert code == 2 and json.loads(raw)['passed'] is False


@pytest.mark.parametrize('value', [0, 101, True, 1.5, None])
def test_list_limit_is_bounded_without_creating_state(tmp_path, value):
    service = _service(tmp_path/'state', tmp_path/'engine', Catalog())
    try:
        with pytest.raises(EvolutionError, match='invalid_limit'): service.list_suggestions(value)
        assert not (tmp_path/'state').exists()
    finally: service.close()


@pytest.mark.parametrize('kwargs', [
    {'kind': 'decide'}, {'kind': ['request']}, {'name': ''}, {'name': 'x'*161},
    {'note': 'line\nbreak'}, {'source': '/other/project'},
    {'source': {'revision':'r','skill_id':'s','path':'../../secret','sha256':'a'*64}},
])
def test_signal_rejects_untrusted_shape_before_writing(tmp_path, kwargs):
    service = _service(tmp_path/'state', tmp_path/'engine', Catalog())
    try:
        with pytest.raises(EvolutionError, match='invalid_signal'):
            service.record_signal(**({'kind':'request','name':'owned','note':'owned'} | kwargs))
        assert not (tmp_path/'state').exists()
    finally: service.close()


def test_python310_syntax():
    source = Path(__import__('supermonoagent.mylib_evolution', fromlist=['x']).__file__).read_text(encoding='utf8')
    ast.parse(source, feature_version=(3,10))


def test_engine_and_constitution_pins_fail_closed(tmp_path):
    engine = tmp_path/'engine';copy_engine(engine)
    service = _service(tmp_path/'state', engine, Catalog())
    try:
        service.run_once()
        engine_file = engine/'scripts/metarsi.py'
        engine_file.write_bytes(engine_file.read_bytes() + b'\n# owned mutation\n')
        with pytest.raises(EvolutionError, match='engine_changed'): service.run_once()
        assert service.status()['engine']['status'] == 'changed'
        assert service.list_suggestions()['count'] == 1
    finally: service.close()


def test_constitution_is_not_silently_reinitialized(tmp_path):
    engine = tmp_path/'engine';copy_engine(engine)
    service = _service(tmp_path/'state', engine, Catalog())
    try:
        service.run_once()
        constitution = service.project/'.metarsi/constitution.json'
        constitution.write_text('{}', encoding='utf8')
        with pytest.raises(EvolutionError, match='state_invalid'): service.run_once()
        assert constitution.read_text() == '{}'
    finally: service.close()


def test_concurrent_calls_fail_busy_without_duplicate_work(tmp_path, monkeypatch):
    engine = tmp_path/'engine';copy_engine(engine)
    first = _service(tmp_path/'state', engine, Catalog())
    second = _service(tmp_path/'state', engine, Catalog())
    entered, release = threading.Event(), threading.Event()
    original = first._review
    def blocked():
        entered.set();assert release.wait(10)
        return original()
    monkeypatch.setattr(first, '_review', blocked)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(first.run_once)
            assert entered.wait(3)
            with pytest.raises(EvolutionError, match='review_busy'): second.record_signal('request','owned','owned')
            release.set();assert future.result(timeout=30)['suggestions'] == 1
        assert first.list_suggestions()['count'] == 1
    finally:
        release.set();first.close();second.close()


def test_timeout_is_sanitized_and_owns_only_new_process(tmp_path):
    with pytest.raises(EvolutionError, match='review_failed') as error:
        _run_process([sys.executable,'-c','import time;time.sleep(30)'],cwd=tmp_path,timeout=.1)
    assert str(error.value) == 'review_failed'


def test_source_truncation_does_not_claim_full_file_validation(tmp_path):
    engine = tmp_path/'engine';copy_engine(engine)
    library = Catalog()
    original = library.read_skill
    def truncated(*args,**kwargs):
        result = original(*args,**kwargs);result['truncated'] = True;return result
    library.read_skill = truncated
    service = _service(tmp_path/'state', engine, library)
    try:
        result = service.run_once()
        assert result['suggestions'] == 0 and result['skipped_nonexact_or_truncated'] == 2
        assert service.list_suggestions()['items'] == []
        assert not events(service, 'trials')
    finally: service.close()


def test_distinct_revision_has_explicit_provenance(tmp_path):
    engine = tmp_path/'engine';copy_engine(engine)
    library = Catalog();service = _service(tmp_path/'state',engine,library)
    try:
        service.run_once();library.revision = 'b'*40
        service.run_once()
        items = service.list_suggestions()['items']
        assert {item['source']['revision'] for item in items} == {'a'*40,'b'*40}
        for item in items:
            data = json.loads((service.project/item['validation']['artifact']).read_text())
            assert data['source'] == item['source']
    finally: service.close()


def test_real_catalog_snapshot_is_only_read_and_its_revision_is_attributable(tmp_path):
    from supermonoagent.mylib_catalog import CatalogStore, build_snapshot, publish_snapshot
    repo = tmp_path/'repo';repo.mkdir()
    empty = tmp_path/'empty-hooks';empty.mkdir()
    skill = repo/'skills/owned/SKILL.md';skill.parent.mkdir(parents=True)
    skill.write_text('---\nname: owned\ndescription: isolated fixture\n---\n# Owned skill\n\nNo scripts should execute.\n', encoding='utf8')
    script = skill.parent/'never-run.py'
    script.write_text('raise RuntimeError("Catalog scripts must not execute")\n', encoding='utf8')
    excluded = repo/'.metarsi/never-review/SKILL.md';excluded.parent.mkdir(parents=True)
    excluded.write_text('# Owned excluded runtime instruction\n',encoding='utf8')
    env = {key:value for key,value in os.environ.items() if not key.upper().startswith('GIT_')}
    env.update(GIT_CONFIG_NOSYSTEM='1',GIT_CONFIG_GLOBAL=os.devnull,GIT_CONFIG_SYSTEM=os.devnull,GIT_TERMINAL_PROMPT='0')
    def git(*args):
        return subprocess.run(['git','-c','core.hooksPath='+str(empty),'-c','commit.gpgsign=false',
            '-c','user.name=Owned Fixture','-c','user.email=owned@example.invalid',*args],
            cwd=repo,env=env,capture_output=True,check=True,timeout=10)
    git('init','--template='+str(empty));git('add','.');git('commit','-m','Owned catalog fixture')
    candidate = tmp_path/'candidate'
    build_snapshot(repo,candidate)
    store = CatalogStore(tmp_path/'catalog')
    publish_snapshot(store,candidate)
    cards = store.list_skills()['items']
    excluded_id = next(card['id'] for card in cards if card.get('available') is False)
    reads, original_read = [], store.read_skill
    def observed_read(*args,**kwargs):
        reads.append(args[0]);return original_read(*args,**kwargs)
    store.read_skill = observed_read
    before = {path.relative_to(store.root).as_posix():(hashlib.sha256(path.read_bytes()).hexdigest(),path.stat().st_mtime_ns,path.stat().st_ctime_ns)
        for path in store.root.rglob('*') if path.is_file()}
    engine = tmp_path/'engine';copy_engine(engine)
    service = _service(tmp_path/'state',engine,store)
    try:
        result = service.run_once()
        assert result['suggestions'] == 1 and result['revision'] == store.status()['revision']
        assert result['scanned'] == 2 and result['skipped_unavailable'] == 1 and result['exact_sources_checked'] == 1
        assert excluded_id not in reads
        suggestion = service.list_suggestions()['items'][0]
        original = store.read_skill(suggestion['skill_id'],revision=result['revision'])
        assert suggestion['source']['sha256'] == original['sha256']
        assert suggestion['source']['path'] == original['path']
        after = {path.relative_to(store.root).as_posix():(hashlib.sha256(path.read_bytes()).hexdigest(),path.stat().st_mtime_ns,path.stat().st_ctime_ns)
            for path in store.root.rglob('*') if path.is_file()}
        assert before == after
        public = json.dumps({'status':service.status(),'suggestions':service.list_suggestions()})
        assert str(tmp_path).replace('\\','\\\\') not in public
    finally: service.close()


def test_catalog_state_overlap_is_rejected_before_any_write(tmp_path):
    library = Catalog();library.root = tmp_path/'catalog'
    with pytest.raises(EvolutionError,match='state_invalid'):
        _service(library.root/'state',tmp_path/'engine',library)
    assert not tmp_path.joinpath('catalog').exists()


@pytest.mark.parametrize('target', ['validation.json','candidate.md','source.md','source.json'])
def test_changed_validation_artifact_is_not_served_as_passed(tmp_path, target):
    engine = tmp_path/'engine';copy_engine(engine)
    service = _service(tmp_path/'state',engine,Catalog())
    try:
        service.run_once()
        item = service.list_suggestions()['items'][0]
        path = service.project/'reviews'/item['suggestion_id']/target
        path.write_bytes(path.read_bytes()+b'\nowned mutation\n')
        with pytest.raises(EvolutionError,match='state_invalid'): service.list_suggestions()
        assert service.status()['available'] is False
    finally: service.close()


def test_a_single_run_limits_candidates_and_admits_nothing(tmp_path):
    engine = tmp_path/'engine';copy_engine(engine)
    service = _service(tmp_path/'state',engine,Catalog({str(i):'# Owned\n' for i in range(5)}))
    try:
        result = service.run_once()
        assert result['suggestions'] == 3 and result['pending_candidates'] == 2 and not result['complete']
        assert not events(service,'signals') and not events(service,'admissions')
        assert service.status()['model_configured'] is False
    finally: service.close()


def test_only_owned_child_processes_are_cleaned_on_timeout(tmp_path):
    import time
    marker = tmp_path/'owned-child-pid'
    code = 'import subprocess,sys,time,pathlib;p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(30)"]);pathlib.Path(sys.argv[1]).write_text(str(p.pid));time.sleep(30)'
    with pytest.raises(EvolutionError,match='review_failed'):
        _run_process([sys.executable,'-c',code,str(marker)],cwd=tmp_path,timeout=.5)
    assert marker.exists()
    pid = int(marker.read_text())
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD];kernel.OpenProcess.restype=wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD];kernel.CloseHandle.argtypes=[wintypes.HANDLE]
        handle=kernel.OpenProcess(0x100000,False,pid)
        if handle:
            try: assert kernel.WaitForSingleObject(handle,3000)==0
            finally: kernel.CloseHandle(handle)
    else:
        deadline=time.monotonic()+3
        while time.monotonic()<deadline:
            try: state=Path('/proc')/str(pid)/'stat';value=state.read_text().rsplit(')',1)[1].split()[0]
            except FileNotFoundError: break
            if value=='Z': break
            time.sleep(.05)
        else: pytest.fail('Owned child survived process-group cleanup')


def test_reported_skill_is_reviewed_first_without_treating_signal_as_proof(tmp_path):
    engine = tmp_path/'engine';copy_engine(engine)
    catalog = Catalog({str(i):'# Owned\n' for i in range(5)})
    service = _service(tmp_path/'state',engine,catalog)
    service.max_suggestions_per_run = 1
    try:
        service.record_signal('request','Review owned skill','Reported hypothesis only.',
            {'revision':catalog.revision,'skill_id':'4','path':'skills/4/SKILL.md',
             'sha256':hashlib.sha256(catalog.files['4'].encode()).hexdigest()})
        service.run_once()
        item = service.list_suggestions()['items'][0]
        assert item['skill_id'] == '4'
        assert item['validation']['semantic_efficacy'] == 'unverified'
        assert len(events(service,'signals')) == 1 and not events(service,'admissions')
    finally: service.close()


@pytest.mark.parametrize('args', [['decide','--approve'],['propose'],['idea','update','--status=adopted'],['shadow','primary']])
def test_admission_activation_and_protected_commands_are_not_permitted(tmp_path,args):
    service=_service(tmp_path/'state',tmp_path/'engine',Catalog())
    try:
        with pytest.raises(EvolutionError,match='review_failed'):service._engine(args)
        assert not (tmp_path/'state').exists()
    finally:service.close()


def state_files(root):
    return {path.relative_to(root).as_posix():(hashlib.sha256(path.read_bytes()).hexdigest(),
        path.stat().st_mtime_ns,path.stat().st_ctime_ns)
        for path in root.rglob('*') if path.is_file()}


def test_full_quota_blocks_new_writes_but_duplicate_signal_is_readonly(tmp_path):
    engine=tmp_path/'engine';copy_engine(engine)
    service=_service(tmp_path/'state',engine,Catalog())
    try:
        first=service.record_signal('request','owned','existing metadata')
        before=state_files(service.root);chain=events(service)
        service.max_state_bytes=service._space()['used_bytes']
        with pytest.raises(EvolutionError,match='quota_exceeded'):
            service.record_signal('request','new','new metadata')
        with pytest.raises(EvolutionError,match='quota_exceeded'):service.run_once()
        duplicate=service.record_signal('request','owned','existing metadata')
        assert duplicate['duplicate'] and duplicate['signal_id']==first['signal_id']
        assert state_files(service.root)==before and events(service)==chain
        assert service.status()['storage']['review_writable'] is False
    finally:service.close()


def test_low_free_space_and_insufficient_headroom_change_no_existing_files(tmp_path,monkeypatch):
    from supermonoagent import mylib_evolution as module
    engine=tmp_path/'engine';copy_engine(engine)
    service=_service(tmp_path/'state',engine,Catalog())
    try:
        service.record_signal('request','owned','existing')
        before=state_files(service.root)
        required=service._entry_growth(review=True)
        service.max_state_bytes=service._space()['used_bytes']+required-1
        with pytest.raises(EvolutionError,match='quota_exceeded'):service.run_once()
        assert state_files(service.root)==before
        service.max_state_bytes=256*1024**2;service.min_free_bytes=1
        usage=module.shutil.disk_usage(service.root)
        monkeypatch.setattr(module.shutil,'disk_usage',lambda _:type(usage)(usage.total,usage.used,0))
        with pytest.raises(EvolutionError,match='insufficient_space'):
            service.record_signal('request','new','new metadata')
        assert state_files(service.root)==before
    finally:service.close()


def test_parallel_quota_rejections_do_not_append_or_mutate(tmp_path):
    engine=tmp_path/'engine';copy_engine(engine)
    service=_service(tmp_path/'state',engine,Catalog())
    try:
        service.record_signal('request','owned','existing')
        service.max_state_bytes=service._space()['used_bytes']
        before=state_files(service.root)
        def attempt(number):
            try:service.record_signal('opportunity','unique-'+str(number),'bounded metadata')
            except EvolutionError as error:return str(error)
        with ThreadPoolExecutor(max_workers=4) as pool:
            assert list(pool.map(attempt,range(4)))==['quota_exceeded']*4
        assert state_files(service.root)==before and len(events(service,'signals'))==1
    finally:service.close()


def test_storage_budget_is_rechecked_while_owned_subprocess_runs(tmp_path):
    count=[]
    def no_space():
        count.append(True);raise EvolutionError('quota_exceeded')
    with pytest.raises(EvolutionError,match='quota_exceeded'):
        _run_process([sys.executable,'-c','import time;time.sleep(30)'],cwd=tmp_path,
            timeout=3,budget_check=no_space)
    assert count


def test_failed_pinned_context_exit_does_not_publish_success_summary(tmp_path):
    engine=tmp_path/'engine';copy_engine(engine)
    library=Catalog()
    @contextmanager
    def pinned():
        yield library
        raise ValueError('private manifest failure detail')
    library.pinned=pinned
    service=_service(tmp_path/'state',engine,library)
    try:
        with pytest.raises(EvolutionError,match='library_unavailable') as error:service.run_once()
        assert str(error.value)=='library_unavailable'
        assert service.status()['last_run'] is None
    finally:service.close()


def test_linux_child_loader_uses_only_selected_interpreter_prefix(tmp_path,monkeypatch):
    from supermonoagent import mylib_evolution as module
    runtime=tmp_path/'owned-runtime';libraries=runtime/'lib';libraries.mkdir(parents=True)
    monkeypatch.setattr(module,'sys',SimpleNamespace(platform='linux',base_prefix=str(runtime),executable=sys.executable))
    forbidden=['LD_LIBRARY_PATH','LD_PRELOAD','LD_AUDIT','LD_DEBUG','PYTHONPATH','PYTHONHOME','PYTHONSTARTUP','OPENAI_API_KEY']
    for name in forbidden:monkeypatch.setenv(name,'OWNED_CALLER_VALUE_MUST_NOT_INHERIT')
    environment=module._child_environment()
    assert environment['LD_LIBRARY_PATH']==str(libraries.resolve())
    assert not any(name in environment for name in forbidden if name!='LD_LIBRARY_PATH')
    command=[sys.executable,'-I','-S','-X','utf8','-c',
        'import os,json;print(json.dumps({k:v for k,v in os.environ.items() if k.startswith(("LD_","PYTHON")) or k=="OPENAI_API_KEY"}))']
    code,raw=module._run_process(command,cwd=tmp_path)
    assert code==0 and json.loads(raw)=={'LD_LIBRARY_PATH':str(libraries.resolve())}


@pytest.mark.parametrize('prefix',['relative/runtime','', 'bad\nroot'])
def test_invalid_runtime_prefix_never_falls_back_to_caller_loader(tmp_path,monkeypatch,prefix):
    from supermonoagent import mylib_evolution as module
    monkeypatch.setattr(module,'sys',SimpleNamespace(platform='linux',base_prefix=prefix,executable=sys.executable))
    monkeypatch.setenv('LD_LIBRARY_PATH',str(tmp_path))
    with pytest.raises(EvolutionError,match='engine_unavailable'):module._child_environment()


def test_missing_runtime_library_directory_is_not_replaced_by_review_cwd(tmp_path,monkeypatch):
    from supermonoagent import mylib_evolution as module
    runtime=tmp_path/'runtime';runtime.mkdir()
    monkeypatch.setattr(module,'sys',SimpleNamespace(platform='linux',base_prefix=str(runtime),executable=sys.executable))
    monkeypatch.setenv('LD_LIBRARY_PATH','.')
    with pytest.raises(EvolutionError,match='engine_unavailable'):module._child_environment()
    with pytest.raises(EvolutionError,match='review_failed'):
        module._run_process(['unselected-python','-c','pass'],cwd=tmp_path)


def test_non_linux_children_receive_no_loader_or_python_env(tmp_path,monkeypatch):
    from supermonoagent import mylib_evolution as module
    monkeypatch.setattr(module,'sys',SimpleNamespace(platform='win32',base_prefix=str(tmp_path),executable=sys.executable))
    for name in ['LD_LIBRARY_PATH','LD_PRELOAD','DYLD_LIBRARY_PATH','PYTHONPATH','PYTHONHOME']:
        monkeypatch.setenv(name,'OWNED_CALLER_VALUE')
    assert not any(name.startswith(('LD_','DYLD_','PYTHON')) for name in module._child_environment())
