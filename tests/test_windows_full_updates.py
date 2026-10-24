"""Full updates use signed fixtures, temporary installs, and mocked I/O/UI only."""
import base64
import io
import json
from pathlib import Path
import subprocess
import time
import zipfile

import pytest

from test_windows_installer import (
    KEY, OPENSSL, SCRIPTS, digest, install, package, powershell,
    ps_literal, pytestmark, record, sign,
)

FEED = 'https://117.50.48.22/updates/windows/stable/manifest.json'


def signed_feed(tmp_path, archive=b'archive', **changes):
    now = int(time.time())
    value = dict(schema=1, format='supermono-windows-full-update-v1',
                 channel='stable', arch='x64', version='0.2.35',
                 sequence=now * 1000000, created=now, expires=now + 86400,
                 url='/updates/installers/0.2.35/SuperMonoAgent-0.2.35-Setup.zip',
                 size=len(archive), sha256=digest(archive))
    value.update(changes)
    raw = json.dumps(value, sort_keys=True, separators=(',', ':')).encode()
    signature = subprocess.run([OPENSSL, 'dgst', '-sha256', '-sign', str(KEY)],
                               input=raw, capture_output=True, check=True).stdout
    path = tmp_path / 'feed.json'
    path.write_text(json.dumps(dict(schema=1, key_id='c728238ab3ebabab',
        payload=base64.b64encode(raw).decode(), signature=base64.b64encode(signature).decode())))
    return path


def run_ps(body, timeout=30):
    return powershell(['-Command', f". {ps_literal(SCRIPTS/'Update.ps1')};{body}"], timeout=timeout)


def fixture_update(tmp_path, *, deep=False):
    old, _ = package(tmp_path)
    new, catalog = package(tmp_path, '0.2.35', private=False)
    if deep:
        runtime = new / 'payload'
        for relative in ['SuperMonoAgent/' + 'x' * 71 + '.dll',
                         'SuperMonoAgent/' + 'a' * 26 + '/' + 'b' * 26 + '/data.txt']:
            path = runtime / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'deep runtime fixture')
            catalog['files'][relative] = record(path)
        sign(new, catalog)
    root = tmp_path / ('i' * max(1, 99 - len(str(tmp_path)))) if deep else tmp_path / 'installed'
    state = tmp_path / 'state'
    result = install(old, root, state)
    assert result.returncode == 0, result.stderr
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as z:
        for path in new.rglob('*'):
            if path.is_file():
                z.writestr(path.relative_to(new).as_posix(), path.read_bytes())
    zip_path = tmp_path / 'public.zip'
    zip_path.write_bytes(archive.getvalue())
    feed = signed_feed(tmp_path, archive.getvalue())
    return root, state, feed, zip_path


def mock_download(feed, archive):
    return f"""
    function Get-SmaUpdateBytes($Uri,$Limit) {{
      if ($Uri.AbsolutePath.EndsWith('manifest.json')) {{return ,[IO.File]::ReadAllBytes({ps_literal(feed)})}}
      return ,[IO.File]::ReadAllBytes({ps_literal(archive)})
    }}
    """


def test_unix_time_matches_python_in_local_timezone():
    before = time.time()
    result = run_ps('Get-SmaUpdateNow')
    assert result.returncode == 0, result.stderr
    assert abs(float(result.stdout.strip()) - before) < 5


def test_real_signature_and_current_epoch_are_accepted(tmp_path):
    feed = signed_feed(tmp_path)
    result = run_ps(f"$r=Read-SmaFullFeed ([IO.File]::ReadAllBytes({ps_literal(feed)})) ([Uri]'{FEED}');$r.Manifest.version")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == '0.2.35'


@pytest.mark.parametrize('changes', [
    {'created': int(time.time()) + 600}, {'expires': 1}, {'sequence': 0},
    {'sequence': 1.5}, {'schema': '1'}, {'extra': True},
    {'url': 'http://117.50.48.22/updates/installers/0.2.35/SuperMonoAgent-0.2.35-Setup.zip'},
    {'url': 'https://elsewhere.test/updates/installers/0.2.35/SuperMonoAgent-0.2.35-Setup.zip'},
    {'url': '/updates/installers/0.2.35/SuperMonoAgent-0.2.35-Setup.zip?secret=x'},
    {'url': '/other.zip'},
])
def test_signed_but_invalid_feed_is_rejected(tmp_path, changes):
    feed = signed_feed(tmp_path, **changes)
    result = run_ps(f"try {{Read-SmaFullFeed ([IO.File]::ReadAllBytes({ps_literal(feed)})) ([Uri]'{FEED}');exit 2}} catch {{'rejected';exit 0}}")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'rejected'


def test_tampered_feed_signature_is_rejected(tmp_path):
    feed = signed_feed(tmp_path)
    value = json.loads(feed.read_text());value['signature'] = base64.b64encode(b'x' * 384).decode()
    feed.write_text(json.dumps(value))
    result = run_ps(f"try {{Read-SmaFullFeed ([IO.File]::ReadAllBytes({ps_literal(feed)})) ([Uri]'{FEED}');exit 2}} catch {{'rejected';exit 0}}")
    assert result.returncode == 0 and result.stdout.strip() == 'rejected'


def test_no_prompt_downloads_validates_and_preserves_current_and_credentials(tmp_path):
    root, state, feed, archive = fixture_update(tmp_path)
    token = (state / 'token.txt').read_bytes()
    result = run_ps(mock_download(feed, archive) + f"""
      function Show-SmaUpdatePrompt {{throw 'must not prompt'}}
      function Install-SmaProgramUpdate {{throw 'must not install'}}
      Invoke-SmaProgramUpdate {ps_literal(root)} '{FEED}' -SuppressPrompt | ConvertTo-Json -Compress
    """)
    assert result.returncode == 0, result.stderr
    response = json.loads(result.stdout)
    assert response['status'] == 'downloaded'
    assert (state / 'token.txt').read_bytes() == token
    assert json.loads((root / 'current.json').read_text())['version'] == '0.2.34'
    assert not list(root.rglob('defaults.private.json'))
    assert (root / 'program-updates/state.json').is_file()
    assert (root / 'program-updates/cache').is_dir()


def test_declining_same_version_only_prompts_once(tmp_path):
    root, _, feed, archive = fixture_update(tmp_path)
    result = run_ps(mock_download(feed, archive) + f"""
      $script:prompts=0
      function Show-SmaUpdatePrompt {{$script:prompts++;return $false}}
      function Install-SmaProgramUpdate {{throw 'must not install'}}
      $a=Invoke-SmaProgramUpdate {ps_literal(root)} '{FEED}'
      $b=Invoke-SmaProgramUpdate {ps_literal(root)} '{FEED}'
      @{{first=$a.status;second=$b.status;prompts=$script:prompts}}|ConvertTo-Json -Compress
    """)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {'first': 'declined', 'second': 'downloaded', 'prompts': 1}


@pytest.mark.parametrize('name', ['../escape.txt', 'defaults.private.json', 'payload/../escape.txt', 'payload/CON.txt'])
def test_archive_rejects_escape_private_profile_and_devices(tmp_path, name):
    archive = tmp_path / 'bad.zip'
    with zipfile.ZipFile(archive, 'w') as z:
        z.writestr(name, 'bad')
    result = run_ps(f"try {{Expand-SmaProgramArchive {ps_literal(archive)} {ps_literal(tmp_path/'out')};exit 2}} catch {{'rejected';exit 0}}")
    assert result.returncode == 0 and result.stdout.strip() == 'rejected'
    assert not (tmp_path / 'escape.txt').exists()


def test_launch_worker_is_invisible_and_contains_no_token_value(tmp_path):
    folder, _ = package(tmp_path)
    root, state = tmp_path / 'install', tmp_path / 'state'
    assert install(folder, root, state).returncode == 0
    result = run_ps(f"""
      function Get-SmaMainProcess {{return $null}}
      function Start-SmaUpdateProcess($Info) {{
        $script:captured=@{{hidden=$Info.CreateNoWindow;shell=$Info.UseShellExecute;args=$Info.Arguments}}
        return $null
      }}
      [void](Start-SmaUpdateWorker {ps_literal(root)})
      $script:captured | ConvertTo-Json -Compress
    """)
    assert result.returncode == 0, result.stderr
    response = json.loads(result.stdout)
    assert response['hidden'] is True and response['shell'] is False
    assert '-CheckOnce' in response['args'] and '-NoPrompt' in response['args']
    assert 'private-installer-fixture-token' not in response['args']


def test_browser_one_shot_can_prompt_and_does_not_bind_short_lived_pid(tmp_path):
    folder, _ = package(tmp_path)
    root, state = tmp_path / 'install', tmp_path / 'state'
    assert install(folder, root, state).returncode == 0
    result = run_ps(f"""
      function Get-SmaMainProcess {{throw 'one-shot must not require native GUI'}}
      function Get-Process {{throw 'one-shot must not bind a transient process'}}
      function Start-SmaUpdateProcess($Info) {{$script:arguments=$Info.Arguments;return $null}}
      [void](Start-SmaUpdateWorker {ps_literal(root)} -OwnerPid 123 -OneShot)
      $script:arguments
    """)
    assert result.returncode == 0, result.stderr
    assert '-CheckOnce' in result.stdout
    assert '-NoPrompt' not in result.stdout and '-OwnerPid' not in result.stdout


def test_browser_launch_selects_one_shot_worker_without_real_spawn(tmp_path):
    root = tmp_path / 'mock-install'
    root.mkdir()
    result = run_ps(f"""
      function Test-SmaMainWindow {{return $false}}
      function Get-SmaLaunchPlan {{return @{{Executable='fixture.exe';Arguments=@('browser','--workspace');WorkingDirectory={ps_literal(root)};StateDir={ps_literal(tmp_path/'state')};Mode='browser'}}}}
      function Start-Process {{return [pscustomobject]@{{HasExited=$true;ExitCode=0;Id=123}}}}
      function Start-SmaUpdateWorker($Root,$OwnerPid,[switch]$OneShot,[switch]$NewClient) {{$script:oneShot=$OneShot.IsPresent;$script:newClient=$NewClient.IsPresent;return $true}}
      $r=Invoke-SmaLaunch {ps_literal(root)}
      @{{mode=$r.mode;one_shot=$script:oneShot;new_client=$script:newClient;started=$r.started}} | ConvertTo-Json -Compress
    """)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {'mode': 'browser', 'one_shot': True, 'new_client': True, 'started': True}


def test_accepting_installs_into_temp_root_without_touching_state(tmp_path):
    root, state, feed, archive = fixture_update(tmp_path)
    before = {p.name: p.read_bytes() for p in state.iterdir()}
    result = run_ps(mock_download(feed, archive) + f"""
      function Show-SmaUpdatePrompt {{return $true}}
      function Show-SmaUpdateInstalled {{}}
      function Install-SmaProgramUpdate($Package,$Root,$State) {{
        & (Join-Path $Package 'Install.ps1') -InstallRoot $Root -StateDir $State -NoLaunch -NoShortcuts -NoRegistry | Out-Null
      }}
      Invoke-SmaProgramUpdate {ps_literal(root)} '{FEED}' | ConvertTo-Json -Compress
    """)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['status'] == 'installed'
    assert json.loads((root / 'current.json').read_text())['version'] == '0.2.35'
    assert json.loads((root / 'previous.json').read_text())['version'] == '0.2.34'
    assert {p.name: p.read_bytes() for p in state.iterdir()} == before


def test_download_hash_mismatch_does_not_change_current(tmp_path):
    root, _, feed, archive = fixture_update(tmp_path)
    archive.write_bytes(b'tampered')
    result = run_ps(mock_download(feed, archive) + f"""
      try {{Invoke-SmaProgramUpdate {ps_literal(root)} '{FEED}' -SuppressPrompt;exit 2}} catch {{'rejected';exit 0}}
    """)
    assert result.returncode == 0 and result.stdout.strip() == 'rejected'
    assert json.loads((root / 'current.json').read_text())['version'] == '0.2.34'


def test_rollback_sequence_is_rejected_before_download(tmp_path):
    root, _, feed, archive = fixture_update(tmp_path)
    result = run_ps(mock_download(feed, archive) + f"""
      Invoke-SmaProgramUpdate {ps_literal(root)} '{FEED}' -SuppressPrompt | Out-Null
      $s=Read-SmaUpdateState {ps_literal(root)};$s.highest_sequence++;Save-SmaUpdateState {ps_literal(root)} $s
      try {{Invoke-SmaProgramUpdate {ps_literal(root)} '{FEED}' -SuppressPrompt;exit 2}} catch {{'rejected';exit 0}}
    """)
    assert result.returncode == 0 and result.stdout.strip() == 'rejected'


def test_failed_high_sequence_download_persists_floor_and_rejects_lower_feed(tmp_path):
    root, _, feed, archive = fixture_update(tmp_path)
    signed = json.loads(base64.b64decode(json.loads(feed.read_text())['payload']))
    low = signed_feed(tmp_path, archive.read_bytes(), sequence=signed['sequence'] - 1)
    low_bytes = low.read_bytes()
    high = signed_feed(tmp_path, archive.read_bytes(), sequence=signed['sequence'])
    high_bytes = high.read_bytes()
    high.write_bytes(high_bytes)
    low = tmp_path / 'low.json';low.write_bytes(low_bytes)
    result = run_ps(f"""
      $script:downloads=0;$script:feed={ps_literal(high)}
      function Get-SmaUpdateBytes($Uri,$Limit) {{
        if ($Uri.AbsolutePath.EndsWith('manifest.json')) {{return ,[IO.File]::ReadAllBytes($script:feed)}}
        $script:downloads++;throw 'fixture download failure'
      }}
      try {{Invoke-SmaProgramUpdate {ps_literal(root)} '{FEED}' -SuppressPrompt}} catch {{}}
      $floor=(Read-SmaUpdateState {ps_literal(root)}).highest_sequence
      $script:feed={ps_literal(low)}
      $rejected=$false
      try {{Invoke-SmaProgramUpdate {ps_literal(root)} '{FEED}' -SuppressPrompt}} catch {{$rejected=$true}}
      @{{floor=$floor;rejected=$rejected;downloads=$script:downloads}} | ConvertTo-Json -Compress
    """)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {'floor': signed['sequence'], 'rejected': True, 'downloads': 1}


@pytest.mark.parametrize('mode', ['missing', 'wrong-start', 'closed-window'])
def test_bound_client_identity_never_adopts_another_gui(mode):
    mock = {
        'missing': "throw 'fixture process gone'",
        'wrong-start': "return [pscustomobject]@{ProcessName='SuperMonoAgent';StartTime=[DateTime]::UtcNow;MainWindowHandle=1}",
        'closed-window': "return [pscustomobject]@{ProcessName='SuperMonoAgent';StartTime=[DateTime]::MinValue;MainWindowHandle=0}",
    }[mode]
    result = run_ps(f"""
      function Get-Process {{{mock}}}
      function Get-SmaMainProcess {{throw 'must not adopt another GUI'}}
      $client=Get-SmaUpdateClient 123 1
      if ($null -eq $client) {{'closed'}} else {{throw 'unexpected live client'}}
    """)
    assert result.returncode == 0 and result.stdout.strip() == 'closed', result.stderr


def test_unbound_client_discovery_can_find_current_gui():
    result = run_ps("function Get-SmaMainProcess {return 'fixture-current-gui'};Get-SmaUpdateClient 0 0")
    assert result.returncode == 0 and result.stdout.strip() == 'fixture-current-gui'


def test_partial_cached_package_is_rebuilt_from_verified_archive(tmp_path):
    root, _, feed, archive = fixture_update(tmp_path)
    result = run_ps(mock_download(feed, archive) + f"""
      $a=Invoke-SmaProgramUpdate {ps_literal(root)} '{FEED}' -SuppressPrompt
      Remove-Item -LiteralPath (Join-Path $a.package 'payload/README.txt')
      $b=Invoke-SmaProgramUpdate {ps_literal(root)} '{FEED}' -SuppressPrompt
      @{{status=$b.status;restored=(Test-Path -LiteralPath (Join-Path $b.package 'payload/README.txt'));stages=@(Get-ChildItem -LiteralPath (Split-Path $b.package) -Directory -Filter 's-*').Count}} | ConvertTo-Json -Compress
    """)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {'status': 'downloaded', 'restored': True, 'stages': 0}


def test_extract_failure_never_publishes_partial_package_and_retry_recovers(tmp_path):
    root, _, feed, archive = fixture_update(tmp_path)
    result = run_ps(mock_download(feed, archive) + f"""
      $script:extract=${{function:Expand-SmaProgramArchive}};$script:attempts=0
      function Expand-SmaProgramArchive($Archive,$Destination) {{
        $script:attempts++
        if ($script:attempts -eq 1) {{
          [void][IO.Directory]::CreateDirectory($Destination)
          [IO.File]::WriteAllText((Join-Path $Destination 'partial.txt'),'fixture partial')
          throw 'fixture disk failure'
        }}
        & $script:extract $Archive $Destination
      }}
      try {{Invoke-SmaProgramUpdate {ps_literal(root)} '{FEED}' -SuppressPrompt | Out-Null}} catch {{}}
      $partial=@(Get-ChildItem -LiteralPath (Join-Path {ps_literal(root)} 'program-updates/cache') -Recurse -Directory | Where-Object {{$_.Name -in @('p','package') -or $_.Name -like 's-*' -or $_.Name -like 'package-stage-*'}}).Count
      $r=Invoke-SmaProgramUpdate {ps_literal(root)} '{FEED}' -SuppressPrompt
      @{{partial=$partial;status=$r.status;attempts=$script:attempts}} | ConvertTo-Json -Compress
    """)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {'partial': 0, 'status': 'downloaded', 'attempts': 2}


def test_update_cleanup_refuses_unowned_path(tmp_path):
    cache = tmp_path / 'cache';cache.mkdir()
    protected = tmp_path / 'versions';protected.mkdir()
    (protected / 'keep.txt').write_text('keep')
    result = run_ps(f"try {{Remove-SmaUpdatePackage {ps_literal(cache)} {ps_literal(protected)};exit 2}} catch {{'rejected';exit 0}}")
    assert result.returncode == 0 and result.stdout.strip() == 'rejected'
    assert (protected / 'keep.txt').read_text() == 'keep'


def test_deep_cache_paths(tmp_path):
    root, _, feed, archive = fixture_update(tmp_path, deep=True)
    assert len(str(root)) >= 80
    result = run_ps(mock_download(feed, archive) + f"""
      Invoke-SmaProgramUpdate {ps_literal(root)} '{FEED}' -SuppressPrompt | ConvertTo-Json -Compress
    """)
    assert result.returncode == 0, result.stderr
    response = json.loads(result.stdout)
    assert response['status'] == 'downloaded'
    prepared = Path(response['package'])
    assert prepared.name == 'p' and len(prepared.parent.name) == 16
    assert (prepared / 'payload/SuperMonoAgent' / ('x' * 71 + '.dll')).is_file()
    assert (prepared / 'payload/SuperMonoAgent' / ('a' * 26) / ('b' * 26) / 'data.txt').is_file()


def test_short_stage_reservation_retries_without_overwriting_collision(tmp_path):
    cache = tmp_path / 'owned-cache';cache.mkdir()
    result = run_ps(f"""
      $script:attempts=0;$script:collision=$null
      function New-Item($Path,$ItemType,$ErrorAction) {{
        $script:attempts++
        if($script:attempts -eq 1){{
          [void][IO.Directory]::CreateDirectory($Path)
          [IO.File]::WriteAllText((Join-Path $Path 'keep.txt'),'collision')
          $script:collision=$Path
          throw 'fixture directory already exists'
        }}
        Microsoft.PowerShell.Management\\New-Item -Path $Path -ItemType $ItemType -ErrorAction Stop
      }}
      $s=New-SmaUpdateStage {ps_literal(cache)}
      Remove-SmaUpdatePackage {ps_literal(cache)} $s
      @{{attempts=$script:attempts;name=[IO.Path]::GetFileName($s);kept=[IO.File]::ReadAllText((Join-Path $script:collision 'keep.txt'))}}|ConvertTo-Json -Compress
    """)
    assert result.returncode == 0, result.stderr
    response = json.loads(result.stdout)
    assert response['attempts'] == 2 and response['kept'] == 'collision'
    assert len(response['name']) == 10 and response['name'].startswith('s-')


def test_worker_once_is_bounded_and_does_not_wait_for_client(tmp_path):
    folder, _ = package(tmp_path)
    root, state = tmp_path / 'install', tmp_path / 'state'
    assert install(folder, root, state).returncode == 0
    result = run_ps(f"""
      function Invoke-SmaProgramUpdate($Root,$FeedUrl,[switch]$SuppressPrompt,[switch]$RequireClient) {{
        return @{{status='mock';prompt=$SuppressPrompt.IsPresent;require_client=$RequireClient.IsPresent}}
      }}
      function Get-SmaUpdateClient {{throw 'must not wait for client'}}
      function Wait-SmaUpdateInterval {{throw 'must not wait 15 minutes'}}
      Invoke-SmaUpdateWorker {ps_literal(root)} '{FEED}' -Once -SuppressPrompt | ConvertTo-Json -Compress
    """)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {'status': 'mock', 'prompt': True, 'require_client': False}


def test_follow_worker_stops_when_client_closes(tmp_path):
    folder, _ = package(tmp_path)
    root, state = tmp_path / 'install', tmp_path / 'state'
    assert install(folder, root, state).returncode == 0
    result = run_ps(f"""
      $script:checks=0;$script:waits=0
      function Get-SmaUpdateClient {{return @{{Id=123}}}}
      function Invoke-SmaProgramUpdate {{$script:checks++;return @{{status='mock'}}}}
      function Wait-SmaUpdateInterval {{$script:waits++;return $false}}
      $r=Invoke-SmaUpdateWorker {ps_literal(root)} '{FEED}'
      @{{status=$r.status;checks=$script:checks;waits=$script:waits}} | ConvertTo-Json -Compress
    """)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {'status': 'client-closed', 'checks': 1, 'waits': 1}


def test_existing_update_mutex_prevents_duplicate_worker_spawn(tmp_path):
    folder, _ = package(tmp_path)
    root, state = tmp_path / 'install', tmp_path / 'state'
    assert install(folder, root, state).returncode == 0
    result = run_ps(f"""
      function Start-SmaUpdateProcess {{throw 'must not spawn a duplicate'}}
      $m=New-Object Threading.Mutex($false,(Get-SmaUpdateMutexName {ps_literal(root)}))
      try {{Start-SmaUpdateWorker {ps_literal(root)}}} finally {{$m.Dispose()}}
    """)
    assert result.returncode == 0 and result.stdout.strip() == 'True', result.stderr


def test_new_client_spawns_waiting_candidate_despite_old_worker_mutex(tmp_path):
    folder, _ = package(tmp_path)
    root, state = tmp_path / 'install', tmp_path / 'state'
    assert install(folder, root, state).returncode == 0
    result = run_ps(f"""
      function Start-SmaUpdateProcess($Info) {{$script:arguments=$Info.Arguments;return $null}}
      $m=New-Object Threading.Mutex($false,(Get-SmaUpdateMutexName {ps_literal(root)}))
      try {{[void](Start-SmaUpdateWorker {ps_literal(root)} -OneShot -NewClient)}} finally {{$m.Dispose()}}
      $script:arguments
    """)
    assert result.returncode == 0, result.stderr
    assert '-WaitForPrevious' in result.stdout and '-CheckOnce' in result.stdout
    assert '-NoPrompt' not in result.stdout


@pytest.mark.parametrize('client_alive', [True, False])
def test_new_client_waiter_checks_only_after_old_mutex_releases(tmp_path, client_alive):
    folder, _ = package(tmp_path)
    root, state = tmp_path / 'install', tmp_path / 'state'
    assert install(folder, root, state).returncode == 0
    client = "return 'fixture-client'" if client_alive else 'return $null'
    result = run_ps(f"""
      $script:acquires=0;$script:checks=0;$script:released=0
      function New-SmaUpdateMutex {{
        $m=[pscustomobject]@{{}}
        $m|Add-Member ScriptMethod WaitOne {{$script:acquires++;return $script:acquires -gt 1}}
        $m|Add-Member ScriptMethod ReleaseMutex {{$script:released++}}
        $m|Add-Member ScriptMethod Dispose {{}}
        return $m
      }}
      function Get-SmaUpdateClient {{{client}}}
      function Start-Sleep {{}}
      function Invoke-SmaProgramUpdate {{
        if($script:acquires -lt 2){{throw 'must not check while old worker holds mutex'}}
        $script:checks++;return @{{status='mock'}}
      }}
      function Wait-SmaUpdateInterval {{return $false}}
      $r=Invoke-SmaUpdateWorker {ps_literal(root)} '{FEED}' -ClientPid 123 -ClientTicks 1 -WaitPrevious
      @{{status=$r.status;checks=$script:checks;acquires=$script:acquires;released=$script:released}}|ConvertTo-Json -Compress
    """)
    assert result.returncode == 0, result.stderr
    response = json.loads(result.stdout)
    assert response == {'status': 'client-closed', 'checks': int(client_alive),
                        'acquires': 2 if client_alive else 1, 'released': int(client_alive)}


def test_explicit_feed_rejects_insecure_origin_without_network():
    result = run_ps("try {Get-SmaUpdateUri 'http://localhost/manifest.json';exit 2} catch {'rejected';exit 0}")
    assert result.returncode == 0 and result.stdout.strip() == 'rejected'


def test_archive_case_collision_and_symlink_are_rejected(tmp_path):
    for index, entries in enumerate([
        [('payload/A.txt', b'a'), ('payload/a.txt', b'b')],
        [('payload/link', b'target')],
    ]):
        archive = tmp_path / f'bad-{index}.zip'
        with zipfile.ZipFile(archive, 'w') as z:
            for name, contents in entries:
                info = zipfile.ZipInfo(name)
                if index == 1:
                    info.create_system = 3
                    info.external_attr = 0o120777 << 16
                z.writestr(info, contents)
        result = run_ps(f"try {{Expand-SmaProgramArchive {ps_literal(archive)} {ps_literal(tmp_path/f'out-{index}')};exit 2}} catch {{'rejected';exit 0}}")
        assert result.returncode == 0 and result.stdout.strip() == 'rejected'
