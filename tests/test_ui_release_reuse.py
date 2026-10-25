"""Android reuse uses only local, ephemeral RSA fixtures; never a live signer/feed."""
import base64
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import time

import pytest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('ui_release_reuse', ROOT / 'scripts/ui_update_release.py')
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


@pytest.fixture(scope='module')
def signer(tmp_path_factory):
    root = tmp_path_factory.mktemp('reuse-fixture-signer')
    tool = release.openssl_tool()
    keys = []
    for name in ('fixture', 'wrong'):
        key = root / (name + '.pem')
        subprocess.run([tool, 'genpkey', '-algorithm', 'RSA', '-pkeyopt',
                        'rsa_keygen_bits:3072', '-out', str(key)], capture_output=True, check=True)
        keys.append(key)
    der = subprocess.run([tool, 'pkey', '-in', str(keys[0]), '-pubout', '-outform', 'DER'],
                         capture_output=True, check=True).stdout
    trust = {'schema': 1, 'keys': {hashlib.sha256(der).hexdigest()[:16]: base64.b64encode(der).decode()}}
    return tool, keys[0], keys[1], trust


def sign(value, fixture):
    tool, key, _, trust = fixture
    payload = release.json_bytes(value)
    signature = subprocess.run([tool, 'dgst', '-sha256', '-sign', str(key)], input=payload,
                               capture_output=True, check=True).stdout
    return release.json_bytes({'schema': 1, 'key_id': next(iter(trust['keys'])),
        'payload': base64.b64encode(payload).decode(), 'signature': base64.b64encode(signature).decode()})


@pytest.fixture
def fixture(tmp_path, signer, monkeypatch):
    root, baseline, out = (tmp_path / name for name in ('source', 'baseline', 'out'))
    static = root / 'src/supermonoagent/static'
    static.mkdir(parents=True)
    (static / 'update-trust.json').write_text(json.dumps(signer[3]))
    for name in list(release.DESKTOP) + ['clipboard-files.js', 'mylib-assistant.js', 'assistant-chat.js']:
        path = static / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(('NEW desktop ' + name).encode())
    for name in ('workspace.py', 'defaults.py'):
        (static.parent / name).write_text('fixture source')
    for name in list(release.MOBILE) + ['agent-modes/' + mode + '.json' for mode in release.AGENT_MODES]:
        path = root / 'android/assets/mobile' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(('DIRTY ANDROID DEBUG GRID ' + name).encode())
    baseline.mkdir()
    now = int(time.time())
    metadata = {'schema': 1, 'channel': 'stable', 'revision': '0.2.34', 'sequence': 123,
                'created': now - 5, 'expires': now + 86400, 'artifacts': {}}
    for platform, names in (('desktop', release.DESKTOP), ('android', release.ANDROID)):
        files = {name: ('OLD trusted ' + name).encode() for name in names}
        archive = baseline / (platform + '.zip')
        release.make_archive(archive, files)
        raw = archive.read_bytes()
        metadata['artifacts'][platform] = {'url': 'releases/0.2.34/' + platform + '.zip',
            'sha256': release.digest(raw), 'size': len(raw), 'native_api': 2,
            'files': {name: {'sha256': release.digest(data), 'size': len(data)} for name, data in files.items()}}
    (baseline / 'manifest.json').write_bytes(sign(metadata, signer))
    calls = []

    def files_for_platform(source, platform, feed):
        calls.append(platform)
        if platform == 'desktop':
            return {name: (static / name).read_bytes() for name in release.DESKTOP}
        return {name: ('DIRTY Android ' + name).encode() for name in release.ANDROID}

    monkeypatch.setattr(release, 'files_for_platform', files_for_platform)
    return {'root': root, 'baseline': baseline, 'out': out, 'metadata': metadata,
            'signer': signer, 'calls': calls}


def build(fixture, **changes):
    args = {'root': fixture['root'], 'revision': '0.2.39', 'private_key': fixture['signer'][1],
            'openssl': fixture['signer'][0], 'reuse_android_from': fixture['baseline'], **changes}
    return release.package(fixture['out'], **args)


def test_reuse_preserves_exact_android_bytes_catalog_and_api_despite_dirty_sources(fixture):
    old_bytes = (fixture['baseline'] / 'android.zip').read_bytes()
    # Reuse must not even fingerprint Android source inputs.
    shutil.rmtree(fixture['root'] / 'android')
    folder = build(fixture)
    payload = release.verify_local(folder, fixture['signer'][3], fixture['signer'][0])
    assert fixture['calls'] == ['desktop']
    assert (folder / 'android.zip').read_bytes() == old_bytes
    expected = copy.deepcopy(fixture['metadata']['artifacts']['android'])
    expected['url'] = 'releases/0.2.39/android.zip'
    assert payload['artifacts']['android'] == expected
    assert len(payload['artifacts']['desktop']['files']) == 17
    assert len(payload['artifacts']['android']['files']) == 16
    assert all(item['native_api'] == 2 for item in payload['artifacts'].values())
    assert set(path.name for path in folder.iterdir()) == {'desktop.zip', 'android.zip', 'manifest.json'}


def test_default_still_builds_both_platforms_and_accepts_legacy_revision(fixture):
    folder = build(fixture, reuse_android_from=None, revision='fixture-default-legacy')
    assert fixture['calls'] == ['desktop', 'android']
    assert (folder / 'android.zip').read_bytes() != (fixture['baseline'] / 'android.zip').read_bytes()


@pytest.mark.parametrize('revision', [None, '', 'custom-name', '20261025T000000Z', '../escape',
                                      '0.2', 'v0.2.39', '0.2.39-beta', '0.02.39'])
def test_reuse_requires_explicit_numeric_version(fixture, revision):
    with pytest.raises(ValueError, match='numeric'):
        build(fixture, revision=revision)
    assert not (fixture['out'] / 'releases').exists()


def test_reuse_refuses_to_replace_baseline_revision(fixture):
    with pytest.raises(ValueError, match='revision'):
        build(fixture, revision='0.2.34')


def test_reuse_sequence_exceeds_verified_baseline_and_local_highest(fixture):
    fixture['metadata']['sequence'] = release.SAFE_INTEGER - 5
    (fixture['baseline'] / 'manifest.json').write_bytes(sign(fixture['metadata'], fixture['signer']))
    fixture['out'].mkdir()
    (fixture['out'] / '.highest-sequence').write_text(str(release.SAFE_INTEGER - 3))
    folder = build(fixture)
    value = release.verify_local(folder, fixture['signer'][3], fixture['signer'][0])
    assert value['sequence'] == release.SAFE_INTEGER - 2


@pytest.mark.parametrize('damage', ['unknown-key', 'signature', 'expired', 'android-api', 'desktop-api',
    'catalog', 'injected-resource', 'url', 'zip', 'desktop-zip', 'missing-desktop', 'oversize-manifest',
    'boolean-envelope-schema', 'boolean-payload-schema'])
def test_invalid_complete_baseline_fails_before_build_or_partial_release(fixture, damage):
    base = fixture['baseline']
    data = fixture['metadata']
    if damage == 'unknown-key':
        envelope = json.loads((base / 'manifest.json').read_bytes())
        envelope['key_id'] = 'unknown-fixture-key'
        (base / 'manifest.json').write_bytes(release.json_bytes(envelope))
    elif damage == 'signature':
        envelope = json.loads((base / 'manifest.json').read_bytes())
        envelope['signature'] = base64.b64encode(b'bad-signature').decode()
        (base / 'manifest.json').write_bytes(release.json_bytes(envelope))
    elif damage in ('zip', 'desktop-zip'):
        path = base / ('android.zip' if damage == 'zip' else 'desktop.zip')
        path.write_bytes(path.read_bytes() + b'tampered')
    elif damage == 'missing-desktop':
        (base / 'desktop.zip').unlink()
    elif damage == 'oversize-manifest':
        (base / 'manifest.json').write_bytes(b'x' * (128 * 1024 + 1))
    elif damage == 'boolean-envelope-schema':
        envelope = json.loads((base / 'manifest.json').read_bytes())
        envelope['schema'] = True
        (base / 'manifest.json').write_bytes(release.json_bytes(envelope))
    else:
        if damage == 'expired':
            data.update(created=1, expires=2)
        elif damage.endswith('-api'):
            data['artifacts'][damage.split('-')[0]]['native_api'] = 3
        elif damage == 'catalog':
            data['artifacts']['android']['files']['mobile/app.js']['sha256'] = '0' * 64
        elif damage == 'injected-resource':
            data['artifacts']['android']['files']['mobile/extra.js'] = {'sha256': '0' * 64, 'size': 0}
        elif damage == 'url':
            data['artifacts']['android']['url'] = 'https://other.invalid/android.zip'
        elif damage == 'boolean-payload-schema':
            data['schema'] = True
        (base / 'manifest.json').write_bytes(sign(data, fixture['signer']))
    with pytest.raises((ValueError, RuntimeError, OSError, KeyError)):
        build(fixture)
    assert fixture['calls'] == []
    assert not fixture['out'].exists()


def test_wrong_output_signer_does_not_publish_or_replace_existing_channel(fixture):
    fixture['out'].mkdir()
    sentinel = (fixture['baseline'] / 'manifest.json').read_bytes()
    (fixture['out'] / 'manifest.json').write_bytes(sentinel)
    (fixture['out'] / '.highest-sequence').write_text('456')
    with pytest.raises(RuntimeError):
        build(fixture, private_key=fixture['signer'][2])
    assert not (fixture['out'] / 'releases/0.2.39').exists()
    assert (fixture['out'] / 'manifest.json').read_bytes() == sentinel
    assert (fixture['out'] / '.highest-sequence').read_text() == '456'


def test_existing_revision_is_never_modified(fixture):
    existing = fixture['out'] / 'releases/0.2.39'
    existing.mkdir(parents=True)
    (existing / 'sentinel').write_bytes(b'immutable')
    with pytest.raises(ValueError, match='immutable'):
        build(fixture)
    assert list(existing.iterdir()) == [existing / 'sentinel']
    assert (existing / 'sentinel').read_bytes() == b'immutable'


def test_source_changed_after_frozen_verification_cannot_replace_reused_bytes(fixture, monkeypatch):
    old_bytes = (fixture['baseline'] / 'android.zip').read_bytes()
    original = release.verify_local
    changed = False

    def verify(directory, *args, **kwargs):
        nonlocal changed
        result = original(directory, *args, **kwargs)
        if not changed:
            changed = True
            (fixture['baseline'] / 'android.zip').write_bytes(b'after-verification replacement')
            (fixture['baseline'] / 'manifest.json').write_bytes(b'after-verification replacement')
        return result

    monkeypatch.setattr(release, 'verify_local', verify)
    folder = build(fixture)
    assert changed
    assert (folder / 'android.zip').read_bytes() == old_bytes
    original(folder, fixture['signer'][3], fixture['signer'][0])


def test_cli_reuse_parameter_is_explicit_and_cannot_combine_with_existing_release():
    args = release.parser().parse_args(['--revision', '0.2.39', '--reuse-android-from', 'fixture-baseline'])
    assert args.reuse_android_from == Path('fixture-baseline')


def test_cli_forwards_reuse_without_publishing(fixture, monkeypatch, capsys):
    import sys
    captured = {}

    def package(output, **kwargs):
        captured.update(kwargs)
        return fixture['baseline']

    monkeypatch.setattr(release, 'package', package)
    monkeypatch.setattr(release, 'publish', lambda *a, **k: pytest.fail('build-only must not publish'))
    monkeypatch.setattr(sys, 'argv', ['publish-ui-update.py', '--build-only', '--revision', '0.2.39',
                                    '--reuse-android-from', str(fixture['baseline'])])
    release.main(publishing=True)
    assert captured['reuse_android_from'] == fixture['baseline']
    assert captured['revision'] == '0.2.39'
    assert json.loads(capsys.readouterr().out)['published'] is False


def test_cli_rejects_reuse_with_existing_release_before_verification(fixture, monkeypatch):
    import sys
    monkeypatch.setattr(sys, 'argv', ['publish-ui-update.py', '--build-only', '--release', 'missing',
                                    '--reuse-android-from', str(fixture['baseline'])])
    with pytest.raises(ValueError, match='cannot be combined'):
        release.main(publishing=True)


def test_baseline_replacement_between_frozen_reads_is_rejected(fixture, monkeypatch):
    original = Path.open
    changed = False

    def replacing(path, *args, **kwargs):
        nonlocal changed
        if path == fixture['baseline'] / 'desktop.zip' and not changed:
            changed = True
            (fixture['baseline'] / 'android.zip').write_bytes(b'concurrent replacement')
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'open', replacing)
    with pytest.raises(ValueError, match='hash/size'):
        build(fixture)
    assert changed
    assert fixture['calls'] == []
    assert not fixture['out'].exists()


def test_both_signed_platform_catalogs_pass_actual_client_verification(fixture):
    from supermonoagent import ui_updates as client
    folder = build(fixture)
    for platform in ('desktop', 'android'):
        payload = client.verify_envelope((folder / 'manifest.json').read_bytes(), fixture['signer'][3], platform)
        files = client.verify_archive(payload, (folder / (platform + '.zip')).read_bytes(), platform)
        assert set(files) == client.FILES[platform]


def test_unknown_output_key_fails_without_touching_output(fixture):
    with pytest.raises(ValueError, match='not bundled'):
        build(fixture, key_id='unknown-output-key')
    assert not fixture['out'].exists()


def test_reused_release_uses_existing_three_file_transaction_locally(fixture):
    helper_spec = importlib.util.spec_from_file_location('reuse_local_publisher_fixture',
        ROOT / 'tests/test_ui_update_publisher.py')
    helper = importlib.util.module_from_spec(helper_spec)
    helper_spec.loader.exec_module(helper)
    if not Path(helper.BASH).is_file():
        pytest.skip('Local Git Bash fixture unavailable')
    remote = fixture['root'].parent / 'remote'
    remote.mkdir()
    runner = helper.LocalSSH(remote)
    release.publish(fixture['baseline'], target='fixture@localhost', remote='/fixture-root',
                    trust=fixture['signer'][3], openssl=fixture['signer'][0], runner=runner)
    old_manifest = (remote / 'manifest.json').read_bytes()
    folder = build(fixture)
    result = release.publish(folder, target='fixture@localhost', remote='/fixture-root',
        trust=fixture['signer'][3], openssl=fixture['signer'][0], runner=runner, preserve_android=True)
    assert result['revision'] == '0.2.39'
    assert (remote / 'manifest.json').read_bytes() == (folder / 'manifest.json').read_bytes()
    assert (remote / 'releases/0.2.34/manifest.json').read_bytes() == old_manifest
    assert (remote / 'releases/0.2.39/android.zip').read_bytes() == (fixture['baseline'] / 'android.zip').read_bytes()
    assert set(path.name for path in (remote / 'releases/0.2.39').iterdir()) == {
        'desktop.zip', 'android.zip', 'manifest.json'}
    # A corrupted upload cannot switch the channel, even for the same revision.
    expected = (remote / 'manifest.json').read_bytes()
    with pytest.raises(RuntimeError):
        release.publish(folder, target='fixture@localhost', remote='/fixture-root',
            trust=fixture['signer'][3], openssl=fixture['signer'][0], runner=helper.LocalSSH(remote, corrupt=True))
    assert (remote / 'manifest.json').read_bytes() == expected


def local_publisher(fixture):
    helper_spec = importlib.util.spec_from_file_location('reuse_cas_local_fixture',
        ROOT / 'tests/test_ui_update_publisher.py')
    helper = importlib.util.module_from_spec(helper_spec)
    helper_spec.loader.exec_module(helper)
    if not Path(helper.BASH).is_file():
        pytest.skip('Local Git Bash fixture unavailable')
    remote = fixture['root'].parent / 'cas-remote'
    remote.mkdir()
    runner = helper.LocalSSH(remote)

    def publish(folder, **kwargs):
        return release.publish(folder, target='fixture@localhost', remote='/fixture-root',
            trust=fixture['signer'][3], openssl=fixture['signer'][0], runner=runner, **kwargs)

    return remote, publish


@pytest.mark.parametrize('stage_only', [False, True])
def test_android_compare_and_swap_rejects_concurrent_android_change(fixture, stage_only):
    remote, publish = local_publisher(fixture)
    publish(fixture['baseline'])
    changed = build(fixture, revision='0.2.40', reuse_android_from=None)
    publish(changed)
    # The stale Android baseline is reused after another publisher updated it,
    # so this candidate has a newer sequence and cannot rely on rollback checks.
    stale = build(fixture, revision='0.2.41')
    candidate = release.verify_local(stale, fixture['signer'][3], fixture['signer'][0])
    current = release.verify_local(changed, fixture['signer'][3], fixture['signer'][0])
    assert candidate['sequence'] > current['sequence']
    before = {path.relative_to(remote).as_posix(): path.read_bytes()
              for path in remote.rglob('*') if path.is_file()}
    with pytest.raises(RuntimeError, match='Android'):
        publish(stale, preserve_android=True, stage_only=stage_only)
    after = {path.relative_to(remote).as_posix(): path.read_bytes()
             for path in remote.rglob('*') if path.is_file()}
    assert after == before
    assert not (remote / 'releases/0.2.41').exists()
    assert not list(remote.glob('.incoming-*'))
    # Opting out retains the ordinary publisher's ability to change Android.
    publish(stale)
    assert (remote / 'manifest.json').read_bytes() == (stale / 'manifest.json').read_bytes()


def test_android_compare_and_swap_refuses_missing_channel_before_creating_revision(fixture):
    remote, publish = local_publisher(fixture)
    folder = build(fixture)
    with pytest.raises(RuntimeError, match='Android'):
        publish(folder, preserve_android=True)
    assert not (remote / 'manifest.json').exists()
    assert list((remote / 'releases').iterdir()) == []
    assert not list(remote.glob('.incoming-*'))


@pytest.mark.parametrize('reuse,explicit,existing,expected', [
    (True, False, False, True), (True, True, False, True),
    (False, True, True, True), (False, False, False, False),
])
def test_cli_preserves_android_automatically_for_reuse_or_explicitly_for_release(
        fixture, monkeypatch, capsys, reuse, explicit, existing, expected):
    import sys
    captured = {}
    monkeypatch.setattr(release, 'package', lambda *a, **k: fixture['baseline'])
    monkeypatch.setattr(release, 'verify_local', lambda *a, **k: fixture['metadata'])

    def publish(*args, **kwargs):
        captured.update(kwargs)
        return {}

    monkeypatch.setattr(release, 'publish', publish)
    arguments = ['publish-ui-update.py', '--publish', '--ssh-target', 'fixture@localhost',
                 '--remote-root', '/fixture-root']
    if existing:
        arguments.extend(['--release', str(fixture['baseline'])])
    if reuse:
        arguments.extend(['--revision', '0.2.39', '--reuse-android-from', str(fixture['baseline'])])
    if explicit:
        arguments.append('--preserve-android')
    monkeypatch.setattr(sys, 'argv', arguments)
    release.main(publishing=True)
    assert captured['preserve_android'] is expected
    assert json.loads(capsys.readouterr().out)['published'] is True


def test_preserve_flag_validation_occurs_before_file_access_or_commands():
    with pytest.raises(ValueError, match='boolean'):
        release.publish('missing-fixture', target='fixture@localhost', preserve_android='false',
                        runner=lambda *a, **k: pytest.fail('invalid flag must not run commands'))


@pytest.mark.parametrize('publisher', [release.publish, release._publish_public_snapshot])
def test_shared_production_promotion_rejects_preserve_before_reads_or_ssh(publisher):
    calls = []
    with pytest.raises(ValueError, match='shared.*revision.*Android.*rollback'):
        publisher('missing-fixture', target='fixture@localhost', preserve_android=True,
                  runner=lambda *a, **k: calls.append(a))
    assert calls == []


@pytest.mark.parametrize('option', ['--reuse-android-from', '--preserve-android'])
def test_cli_rejects_shared_production_promotion_before_build_or_publish(monkeypatch, option):
    import sys
    monkeypatch.setattr(release, 'package', lambda *a, **k: pytest.fail('unsafe CLI must reject before build'))
    monkeypatch.setattr(release, 'publish', lambda *a, **k: pytest.fail('unsafe CLI must reject before SSH'))
    args = ['publish-ui-update.py', '--publish', '--ssh-target', 'fixture@localhost']
    if option == '--reuse-android-from':
        args += [option, 'missing-baseline', '--revision', '0.2.40']
    else:
        args += [option, '--release', 'missing-release']
    monkeypatch.setattr(sys, 'argv', args)
    with pytest.raises(ValueError, match='shared.*revision.*Android.*rollback'):
        release.main(publishing=True)


@pytest.mark.parametrize('stage_only,preserve', [(True, True), (False, False)])
def test_shared_root_allows_staging_or_default_full_publish_with_no_real_ssh(
        fixture, stage_only, preserve):
    calls = []
    result = release.publish(fixture['baseline'], target='fixture@localhost',
        trust=fixture['signer'][3], openssl=fixture['signer'][0], stage_only=stage_only,
        preserve_android=preserve, runner=lambda args, **kwargs: calls.append((args, kwargs)))
    assert result['staged_only'] is stage_only
    assert len(calls) == 3
    assert calls[0][0][0] == 'ssh' and calls[1][0][0] == 'scp' and calls[2][0][0] == 'ssh'
    # These are captured command arguments only, never run against a host.
    assert b'/var/lib/supermono-updates/stable' in calls[2][1]['input']


def test_cli_reuse_can_stage_on_shared_root_without_promoting(fixture, monkeypatch, capsys):
    import sys
    captured = {}
    monkeypatch.setattr(release, 'package', lambda *a, **k: fixture['baseline'])

    def publish(*args, **kwargs):
        captured.update(kwargs)
        return {'staged_only': True}

    monkeypatch.setattr(release, 'publish', publish)
    monkeypatch.setattr(sys, 'argv', ['publish-ui-update.py', '--stage-only', '--ssh-target',
        'fixture@localhost', '--revision', '0.2.40', '--reuse-android-from', str(fixture['baseline'])])
    release.main(publishing=True)
    assert captured['stage_only'] is True and captured['preserve_android'] is True
    assert captured['remote'] == '/var/lib/supermono-updates/stable'
    assert json.loads(capsys.readouterr().out)['published'] is False
