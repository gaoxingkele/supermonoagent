"""Owned Git fixtures only: never fetch a real library or operate user PTYs."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from supermonoagent import mylib_sync as S

SOURCE = 'https://example.invalid/source.git'
MODULE = 'https://example.invalid/module.git'


def git(path, *args):
    env = S._environment(path)
    result = subprocess.run(['git', '-c', 'user.name=Owned test', '-c', 'user.email=owned@example.invalid',
                             '-c', 'protocol.file.allow=always', '-C', str(path), *args],
                            env=env, check=True, capture_output=True)
    return result.stdout.decode().strip()


def repository(path, text='first'):
    path.mkdir()
    git(path, 'init', '-b', 'main')
    (path/'SKILL.md').write_text(text, encoding='utf-8')
    git(path, 'add', 'SKILL.md')
    git(path, 'commit', '-m', 'Owned initial')
    return path


def change(repo, text):
    (repo/'SKILL.md').write_text(text, encoding='utf-8')
    git(repo, 'add', 'SKILL.md')
    git(repo, 'commit', '-m', 'Owned change')
    return git(repo, 'rev-parse', 'HEAD')


class LocalRunner:
    """Explicit test-only transport injection; production never permits file."""
    def __init__(self, sources):
        self.sources = sources
        self.cache_sources = {}
        self.calls = []
        self.fail = None

    def __call__(self, args, *, cwd, env, timeout, max_output, check=None):
        self.calls.append((list(args), Path(cwd), dict(env)))
        assert env['GIT_TERMINAL_PROMPT'] == '0'
        assert env['GIT_CONFIG_NOSYSTEM'] == '1'
        assert env['GIT_CONFIG_GLOBAL'] == os.devnull
        assert not any(key.upper() in {'GIT_DIR', 'GIT_WORK_TREE', 'GIT_CONFIG_COUNT', 'HTTPS_PROXY', 'HTTP_PROXY'} for key in env)
        assert 'protocol.file.allow=never' in args
        assert 'core.fsmonitor=false' in args
        assert 'http.sslVerify=true' in args
        assert 'http.followRedirects=false' in args
        if self.fail and self.fail in args:
            return S.GitResult(128, b'', b'private remote failure token=should-not-leak')
        actual = [value.replace('protocol.file.allow=never', 'protocol.file.allow=always') for value in args]
        if 'clone' in actual:
            original = actual[-2]
            actual[-2] = str(self.sources[original])
            result = S._run(actual, cwd=cwd, env=env, timeout=timeout, max_output=max_output, check=check)
            if result.returncode == 0:
                cache = Path(actual[-1])
                git(cache, 'remote', 'set-url', 'origin', original)
                self.cache_sources[original] = self.sources[original]
            return result
        if 'fetch' in actual:
            index = actual.index('origin')
            remote = git(Path(cwd), 'config', '--get', 'remote.origin.url')
            actual[index] = str(self.sources[remote])
        return S._run(actual, cwd=cwd, env=env, timeout=timeout, max_output=max_output, check=check)


class StubCatalog:
    """Only the catalog boundary is stubbed; actual Git operations still run."""
    def __init__(self):
        self.builds = []
        self.publishes = []
        self.invalid = False
        owner = self

        class Store:
            def __init__(self, root):
                self.root = Path(root)

            def status(self):
                path = self.root/'current.json'
                if not path.exists():
                    return {'available':False, 'source_revision':None, 'revision':None}
                value = json.loads(path.read_text())
                return {'available':True, 'revision':value['revision'], 'source_revision':value['revision']}
        self.CatalogStore = Store

    def build_snapshot(self, repo, destination, revision=None, *, submodule_repos=None, budget_check=None):
        self.builds.append((Path(repo), revision, dict(submodule_repos or {})))
        destination.mkdir()
        data = git(repo, 'show', revision+':SKILL.md')
        (destination/'SKILL.md').write_text(data)
        value = {'revision':revision, 'sha256':hashlib.sha256(data.encode()).hexdigest()}
        (destination/'manifest.json').write_text(json.dumps(value))
        return value

    def verify_snapshot(self, path, *, budget_check=None):
        if self.invalid:
            raise ValueError('private verify failure')
        value = json.loads((path/'manifest.json').read_text())
        assert hashlib.sha256((path/'SKILL.md').read_bytes()).hexdigest() == value['sha256']
        return value

    def publish_snapshot(self, store, candidate, *, expected_revision):
        current = self.CatalogStore(store).status()['revision']
        if current != expected_revision:
            raise ValueError('revision conflict')
        value = self.verify_snapshot(candidate)
        target = Path(store)/'releases'/value['revision']
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(candidate, target)
        (Path(store)/'current.json').write_text(json.dumps({'revision':value['revision']}))
        self.publishes.append(value['revision'])
        return {'revision':value['revision'], 'changed':True}


@pytest.fixture
def fixture(tmp_path):
    repo = repository(tmp_path/'source')
    config = {'schema':1, 'source_url':SOURCE, 'ref':'refs/heads/main',
              'cache_root':str(tmp_path/'cache'), 'store_root':str(tmp_path/'store'),
              'submodules':{}, 'min_free_bytes':0}
    return repo, config, LocalRunner({SOURCE:repo}), StubCatalog()


def test_first_sync_repeated_ref_and_fast_forward(fixture):
    repo, config, runner, api = fixture
    first = S.sync_once(config, runner=runner, catalog_api=api)
    revision = git(repo, 'rev-parse', 'HEAD')
    assert first['ok'] and first['changed'] and first['revision'] == revision
    assert first['source_revision'] == revision and first['submodules'] == 0
    second = S.sync_once(config, runner=runner, catalog_api=api)
    assert second['ok'] and not second['changed']
    assert len(api.builds) == 1
    new = change(repo, 'second')
    third = S.sync_once(config, runner=runner, catalog_api=api)
    assert third['changed'] and third['revision'] == new
    assert (Path(config['store_root'])/'releases'/revision/'SKILL.md').read_text() == 'first'
    assert (Path(config['store_root'])/'releases'/new/'SKILL.md').read_text() == 'second'
    assert not list((Path(config['cache_root'])/'candidates').iterdir())
    assert (repo/'SKILL.md').read_text() == 'second'
    assert not git(repo, 'status', '--short')


@pytest.mark.parametrize('limit,error', [('cache', 'cache_limit'), ('disk', 'insufficient_space'),
                                       ('deadline', 'sync_timeout')])
def test_builder_is_stopped_during_growth_before_publish(fixture, monkeypatch, limit, error):
    repo, config, runner, api = fixture
    initial = S.sync_once(config, runner=runner, catalog_api=api)['revision']
    change(repo, 'candidate to check during construction')
    cache = Path(config['cache_root'])
    if limit == 'cache':
        config['max_cache_bytes'] = S._size(cache, 1024**3) + 128*1024
    real_clock, real_disk = time.monotonic, S.shutil.disk_usage
    offset = [0]
    writes = []
    monkeypatch.setattr(S.time, 'monotonic', lambda: real_clock() + offset[0])
    if limit == 'disk':
        config['min_free_bytes'] = 1024
        monkeypatch.setattr(S.shutil, 'disk_usage',
                            lambda path: SimpleNamespace(free=0) if writes else real_disk(path))

    class Growing(StubCatalog):
        def build_snapshot(self, source, destination, revision=None, *, submodule_repos=None, budget_check=None):
            result = super().build_snapshot(source, destination, revision=revision, submodule_repos=submodule_repos)
            chunk = b'x'*(64*1024)
            with (destination/'large-owned-candidate').open('wb') as stream:
                for index in range(4):
                    if budget_check is not None:
                        budget_check(pending_bytes=len(chunk))
                    stream.write(chunk)
                    stream.flush()
                    writes.append(index)
                    offset[0] += config.get('total_timeout', 900) + 1 if limit == 'deadline' else 2
            return result
    growing = Growing()
    with pytest.raises(S.SyncError, match=error):
        S.sync_once(config, runner=runner, catalog_api=growing)
    assert len(writes) < 4, 'Budget failure must stop the writer, not be discovered only after all writes'
    assert growing.CatalogStore(config['store_root']).status()['revision'] == initial
    assert not growing.publishes
    assert not list((cache/'candidates').iterdir())


def test_budget_checks_are_constant_work_between_throttled_scans(tmp_path, monkeypatch):
    now = [0.0]
    calls = {'size':0, 'disk':0}

    def size(*_):
        calls['size'] += 1
        return 100

    def disk(*_):
        calls['disk'] += 1
        return SimpleNamespace(free=1024**3)
    monkeypatch.setattr(S.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(S, '_size', size)
    monkeypatch.setattr(S.shutil, 'disk_usage', disk)
    config = {'cache_root':tmp_path, 'max_cache_bytes':1024**3, 'min_free_bytes':1024}
    budget = S._BuildBudget(config, 100)
    for _ in range(15000):
        budget(pending_bytes=1)
    assert calls == {'size':1, 'disk':1}
    now[0] = 2
    budget()
    assert calls == {'size':2, 'disk':2}
    for _ in range(15000):
        budget()
    assert calls == {'size':2, 'disk':2}


def test_refresh_keeps_declared_but_unwritten_blob_bytes_reserved(tmp_path, monkeypatch):
    now = [0.0]
    free = [1000]
    monkeypatch.setattr(S.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(S, '_size', lambda *_: 100)
    monkeypatch.setattr(S.shutil, 'disk_usage', lambda *_: SimpleNamespace(free=free[0]))
    budget = S._BuildBudget({'cache_root':tmp_path, 'max_cache_bytes':10000, 'min_free_bytes':200}, 100)
    budget(pending_bytes=600)
    # The real disk shrinks before the reserved blob has all been written.
    # Refreshing from disk must not silently forget its pending 600 bytes.
    free[0], now[0] = 700, 2
    with pytest.raises(S.SyncError, match='insufficient_space'):
        budget()


def test_real_catalog_budget_reserves_large_blob_before_opening_it(fixture, monkeypatch):
    from supermonoagent import mylib_catalog as C
    repo, config, runner, _ = fixture
    change(repo, '---\nname: owned-review\ndescription: Inspect owned evidence.\n---\nStable.\n')
    initial = S.sync_once(config, runner=runner)['revision']
    pointer = Path(config['store_root'])/'current.json'
    stable_pointer = pointer.read_bytes()
    cache = Path(config['cache_root'])
    config['max_cache_bytes'] = S._size(cache, 1024**3) + 64*1024
    (repo/'large-owned.txt').write_bytes(b'x'*(128*1024))
    git(repo, 'add', 'large-owned.txt')
    git(repo, 'commit', '-m', 'Owned large blob budget fixture')
    opened = []
    original = Path.open

    def tracked_open(path, *args, **kwargs):
        mode = args[0] if args else kwargs.get('mode', 'r')
        if path.name == 'large-owned.txt' and 'candidates' in path.parts and ('w' in mode or 'x' in mode):
            opened.append(path)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', tracked_open)
    with pytest.raises(S.SyncError, match='cache_limit'):
        S.sync_once(config, runner=runner)
    assert not opened, 'The blob budget must be reserved before opening its destination'
    assert pointer.read_bytes() == stable_pointer
    assert C.CatalogStore(config['store_root']).status()['revision'] == initial
    assert not list((cache/'candidates').iterdir())


def test_actual_git_and_catalog_publish_integrity_and_history(fixture, tmp_path):
    from supermonoagent import mylib_catalog as C
    repo, config, runner, _ = fixture
    first_text = '---\nname: owned-review\ndescription: Inspect owned evidence.\n---\nFirst independent fixture.\n'
    first_revision = change(repo, first_text)
    module = repository(tmp_path/'actual-module',
                        '---\nname: owned-module\ndescription: Pinned module fixture.\n---\nPinned source.\n')
    pin = git(module, 'rev-parse', 'HEAD')
    change(module, '---\nname: owned-module\ndescription: Newer module.\n---\nNot the pinned content.\n')
    (repo/'.gitmodules').write_text('[submodule "module"]\npath = deps/module\nurl = '+MODULE+'\n')
    git(repo, 'add', '.gitmodules')
    git(repo, 'update-index', '--add', '--cacheinfo', '160000,'+pin+',deps/module')
    git(repo, 'commit', '-m', 'Owned actual catalog module')
    first_revision = git(repo, 'rev-parse', 'HEAD')
    config['submodules'] = {'deps/module':MODULE}
    runner.sources[MODULE] = module

    def committed_text(revision):
        return subprocess.check_output(['git', '-C', str(repo), 'show', revision+':SKILL.md'],
                                       env=S._environment(repo)).decode('utf-8')

    first = S.sync_once(config, runner=runner)
    store = C.CatalogStore(config['store_root'])
    assert first['ok'] and first['changed'] and first['revision'] == first_revision
    assert store.status()['source_revision'] == first_revision
    items = store.list_skills()['items']
    assert {item['name'] for item in items} == {'owned-review', 'owned-module'}
    review_id = next(item['id'] for item in items if item['name'] == 'owned-review')
    module_id = next(item['id'] for item in items if item['name'] == 'owned-module')
    assert 'Pinned source.' in store.read_skill(module_id)['text']
    repeated = S.sync_once(config, runner=runner)
    assert repeated['ok'] and repeated['changed'] is False

    second_text = first_text.replace('First independent fixture.', 'Second independently committed fixture.')
    second_revision = change(repo, second_text)
    newer = S.sync_once(config, runner=runner)
    assert newer['changed'] and store.status()['revision'] == second_revision
    assert store.status()['previous_revision'] == first_revision
    assert store.read_skill(review_id)['text'] == committed_text(second_revision)
    assert store.read_skill(review_id, revision=first_revision)['text'] == committed_text(first_revision)
    pointer = Path(config['store_root'])/'current.json'
    stable_pointer = pointer.read_bytes()

    change(repo, first_text.replace('First independent fixture.', 'Candidate that will be corrupted.'))

    def corrupt_after_export(source, destination, **kwargs):
        manifest = C.build_snapshot(source, destination, **kwargs)
        (destination/'files/SKILL.md').write_text('Corrupted candidate bytes without matching digest.')
        return manifest
    tampered = SimpleNamespace(CatalogStore=C.CatalogStore, build_snapshot=corrupt_after_export,
                               verify_snapshot=C.verify_snapshot, publish_snapshot=C.publish_snapshot)
    with pytest.raises(S.SyncError, match='sync_failed'):
        S.sync_once(config, runner=runner, catalog_api=tampered)
    assert pointer.read_bytes() == stable_pointer
    assert store.read_skill(review_id)['text'] == committed_text(second_revision)
    assert not list((Path(config['cache_root'])/'candidates').iterdir())

    git(repo, 'reset', '--hard', first_revision)
    with pytest.raises(S.SyncError, match='non_fast_forward'):
        S.sync_once(config, runner=runner)
    assert pointer.read_bytes() == stable_pointer
    assert store.read_skill(review_id)['text'] == committed_text(second_revision)


def test_forced_upstream_reset_never_changes_active(fixture):
    repo, config, runner, api = fixture
    initial = git(repo, 'rev-parse', 'HEAD')
    newest = change(repo, 'newer')
    S.sync_once(config, runner=runner, catalog_api=api)
    git(repo, 'reset', '--hard', initial)
    with pytest.raises(S.SyncError, match='non_fast_forward'):
        S.sync_once(config, runner=runner, catalog_api=api)
    assert api.CatalogStore(config['store_root']).status()['revision'] == newest
    report = json.loads((Path(config['cache_root'])/'last-sync.json').read_text())
    assert report['error'] == 'non_fast_forward' and not report['ok']
    assert 'token=' not in json.dumps(report)


def test_pinned_submodule_not_its_latest_head(fixture, tmp_path):
    repo, config, runner, api = fixture
    module = repository(tmp_path/'module', 'pinned module')
    pin = git(module, 'rev-parse', 'HEAD')
    change(module, 'new module head must not be exported')
    (repo/'.gitmodules').write_text('[submodule "module"]\npath = deps/module\nurl = '+MODULE+'\n')
    git(repo, 'add', '.gitmodules')
    git(repo, 'update-index', '--add', '--cacheinfo', '160000,'+pin+',deps/module')
    git(repo, 'commit', '-m', 'Owned pinned submodule')
    config['submodules'] = {'deps/module':MODULE}
    runner.sources[MODULE] = module
    result = S.sync_once(config, runner=runner, catalog_api=api)
    assert result['submodules'] == 1
    mapping = api.builds[-1][2]
    assert set(mapping) == {'deps/module'}
    assert git(mapping['deps/module'], 'show', pin+':SKILL.md') == 'pinned module'
    assert pin != git(mapping['deps/module'], 'rev-parse', 'HEAD')


def test_unlisted_submodule_url_is_never_fetched(fixture):
    repo, config, runner, api = fixture
    initial = S.sync_once(config, runner=runner, catalog_api=api)['revision']
    (repo/'.gitmodules').write_text('[submodule "bad"]\npath = deps/bad\nurl = https://attacker.invalid/bad.git\n')
    git(repo, 'add', '.gitmodules')
    git(repo, 'update-index', '--add', '--cacheinfo', '160000,'+initial+',deps/bad')
    git(repo, 'commit', '-m', 'Owned disallowed declaration')
    with pytest.raises(S.SyncError, match='submodule_source_changed'):
        S.sync_once(config, runner=runner, catalog_api=api)
    assert api.CatalogStore(config['store_root']).status()['revision'] == initial
    assert not any('https://attacker.invalid/bad.git' in args for args, _, _ in runner.calls)


def test_integrity_failure_keeps_old_active_and_cleans_only_candidate(fixture):
    repo, config, runner, api = fixture
    initial = S.sync_once(config, runner=runner, catalog_api=api)['revision']
    change(repo, 'new candidate')
    api.invalid = True
    with pytest.raises(S.SyncError, match='sync_failed'):
        S.sync_once(config, runner=runner, catalog_api=api)
    assert api.CatalogStore(config['store_root']).status()['revision'] == initial
    assert not list((Path(config['cache_root'])/'candidates').iterdir())
    assert (repo/'SKILL.md').read_text() == 'new candidate'


def test_git_failures_are_sanitized_and_retry_succeeds(fixture):
    _, config, runner, api = fixture
    runner.fail = 'clone'
    with pytest.raises(S.SyncError) as error:
        S.sync_once(config, runner=runner, catalog_api=api)
    assert str(error.value) == 'git_failed'
    assert not api.publishes
    assert not list((Path(config['cache_root'])/'repos').iterdir())
    runner.fail = None
    assert S.sync_once(config, runner=runner, catalog_api=api)['changed']


@pytest.mark.parametrize('change_value', [
    {'source_url':'http://example.invalid/source.git'},
    {'source_url':'https://user:password@example.invalid/source.git'},
    {'source_url':'file:///tmp/source.git'},
    {'source_url':'https://example.invalid/source.git?token=secret'},
    {'ref':'refs/heads/main:refs/heads/evil'},
    {'ref':'refs/heads/../evil'},
    {'submodules':{'../escape':MODULE}},
    {'submodules':{'module':'ssh://example.invalid/module.git'}},
    {'command_timeout':True},
    {'schema':True},
])
def test_invalid_operator_config_creates_nothing(fixture, change_value):
    _, config, runner, api = fixture
    config.update(change_value)
    with pytest.raises(S.SyncError, match='invalid_config'):
        S.sync_once(config, runner=runner, catalog_api=api)
    assert not Path(config['cache_root']).exists()
    assert not runner.calls


def test_cache_and_store_must_not_overlap(fixture):
    _, config, runner, api = fixture
    config['store_root'] = str(Path(config['cache_root'])/'nested')
    with pytest.raises(S.SyncError, match='invalid_config'):
        S.sync_once(config, runner=runner, catalog_api=api)
    assert not Path(config['cache_root']).exists()


def test_low_disk_and_existing_cache_limit_stop_before_fetch(fixture, monkeypatch):
    _, config, runner, api = fixture
    config['min_free_bytes'] = 100
    monkeypatch.setattr(S.shutil, 'disk_usage', lambda path: SimpleNamespace(free=0))
    with pytest.raises(S.SyncError, match='insufficient_space'):
        S.sync_once(config, runner=runner, catalog_api=api)
    assert not runner.calls
    config['min_free_bytes'] = 0
    config['max_cache_bytes'] = 1024
    (Path(config['cache_root'])/'owned-large').write_bytes(b'x'*1025)
    with pytest.raises(S.SyncError, match='cache_limit'):
        S.sync_once(config, runner=runner, catalog_api=api)
    assert not runner.calls


@pytest.mark.parametrize('key,value', [('url.https://attacker.invalid/.insteadOf', 'https://example.invalid/'),
                                      ('remote.origin.vcs', 'this-helper-must-not-run')])
def test_private_cache_rejects_url_rewrite_or_remote_helper_configuration(fixture, key, value):
    _, config, runner, api = fixture
    initial = S.sync_once(config, runner=runner, catalog_api=api)['revision']
    cache = Path(config['cache_root'])/'repos/source'
    git(cache, 'config', key, value)
    before = len(runner.calls)
    with pytest.raises(S.SyncError, match='invalid_cache'):
        S.sync_once(config, runner=runner, catalog_api=api)
    assert api.CatalogStore(config['store_root']).status()['revision'] == initial
    assert not any('fetch' in args for args, _, _ in runner.calls[before:])


def test_sync_lock_is_nonblocking(fixture):
    _, config, runner, api = fixture
    cache = Path(config['cache_root'])
    with S._lock(cache):
        with pytest.raises(S.SyncError, match='sync_busy'):
            S.sync_once(config, runner=runner, catalog_api=api)
    assert not runner.calls


def test_sync_lock_also_blocks_an_independent_process(tmp_path):
    cache = tmp_path/'private-cache'
    source = str(Path(__file__).resolve().parents[1]/'src')
    code = ('import sys;from pathlib import Path;sys.path.insert(0,'+repr(source)+');'
            'from supermonoagent.mylib_sync import _lock,SyncError\n'
            'try:\n with _lock(Path('+repr(str(cache))+')):\n  print("unexpected")\n'
            'except SyncError as error:\n print(str(error))\n')
    with S._lock(cache):
        result = subprocess.run([sys.executable, '-c', code], capture_output=True, timeout=5, check=True)
    assert result.stdout.strip() == b'sync_busy'


def test_audit_disk_failure_does_not_misreport_publication_or_mask_error(fixture, monkeypatch):
    _, config, runner, api = fixture

    def fail(*_):
        raise OSError('private audit path')
    monkeypatch.setattr(S, '_report', fail)
    result = S.sync_once(config, runner=runner, catalog_api=api)
    assert result['ok'] and result['changed'] and result['audit_written'] is False
    assert api.CatalogStore(config['store_root']).status()['revision'] == result['revision']
    runner.fail = 'fetch'
    with pytest.raises(S.SyncError, match='git_failed'):
        S.sync_once(config, runner=runner, catalog_api=api)


def test_review_runs_on_unchanged_versions_and_failure_is_separate(fixture, tmp_path):
    _, config, runner, api = fixture
    config['review'] = {'state_root':str(tmp_path/'review'), 'engine_root':str(tmp_path/'engine')}
    calls = []

    class Review:
        def __init__(self, state, engine, library):
            calls.append((state, engine, library.status()['revision']))

        def run_once(self):
            return {'revision':calls[-1][2], 'suggestion_count':2}
    first = S.sync_once(config, runner=runner, catalog_api=api, review_factory=Review)
    assert first['review']['ok'] and first['review']['suggestion_count'] == 2
    second = S.sync_once(config, runner=runner, catalog_api=api, review_factory=Review)
    assert not second['changed'] and len(calls) == 2

    class Broken(Review):
        def run_once(self):
            raise RuntimeError('private model credential must never leak')
    result = S.sync_once(config, runner=runner, catalog_api=api, review_factory=Broken)
    assert result['ok'] and result['review'] == {'ok':False, 'error':'review_failed'}
    assert 'credential' not in json.dumps(result)


@pytest.mark.parametrize('body,code,timeout', [('import time;time.sleep(5)', 'git_timeout', .1),
                                             ('print("x"*100000)', 'git_output_limit', 5)])
def test_subprocess_deadline_and_output_are_bounded(tmp_path, body, code, timeout):
    with pytest.raises(S.SyncError, match=code):
        S._run([sys.executable, '-c', body], cwd=tmp_path, env=dict(os.environ), timeout=timeout, max_output=1024)


def test_live_resource_monitor_stops_owned_process(tmp_path):
    def exhausted():
        raise S.SyncError('cache_limit')
    with pytest.raises(S.SyncError, match='cache_limit'):
        S._run([sys.executable, '-c', 'import time;time.sleep(5)'], cwd=tmp_path,
               env=dict(os.environ), timeout=10, check=exhausted)


def test_unexpected_monitor_failure_also_stops_its_owned_descendant(tmp_path):
    started, late = tmp_path/'child-started', tmp_path/'child-must-not-survive'
    child = ('import time;from pathlib import Path;Path('+repr(str(started))+').touch();'
             'time.sleep(.7);Path('+repr(str(late))+').touch()')
    parent = 'import subprocess,sys,time;subprocess.Popen([sys.executable,"-c",'+repr(child)+']);time.sleep(5)'

    def monitor():
        if started.exists():
            raise RuntimeError('owned monitor error')
    with pytest.raises(RuntimeError, match='owned monitor error'):
        S._run([sys.executable, '-c', parent], cwd=tmp_path, env=dict(os.environ), timeout=5, check=monitor)
    assert started.exists()
    time.sleep(.9)
    assert not late.exists()


def test_cli_rejects_duplicate_json_keys_without_showing_contents(tmp_path, capsys):
    path = tmp_path/'config.json'
    path.write_text('{"schema":1,"schema":1,"secret":"DO_NOT_PRINT"}')
    assert S.main(['--config', str(path), '--once']) == 1
    assert json.loads(capsys.readouterr().out) == {'ok':False, 'error':'sync_failed'}
