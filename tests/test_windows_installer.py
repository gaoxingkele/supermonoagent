"""Signed one-click installation runs only against temporary roots and fake EXEs."""
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from test_ui_updates import OPENSSL

ROOT=Path(__file__).resolve().parents[1]
SCRIPTS=ROOT/'scripts/windows-installer'
KEY=ROOT/'.omx/update-signing/development-private.pem'
PS=shutil.which('powershell.exe')
pytestmark=pytest.mark.skipif(sys.platform!='win32' or not PS or not OPENSSL or not KEY.is_file(),reason='Windows PowerShell and local release signing fixture required')
BOOTSTRAP=('Install.cmd','Install.ps1','Launch.ps1','Uninstall.ps1','Update.ps1')
TOKEN='private-installer-fixture-token'
RELAY='wss://117.50.48.22/ws'


def digest(data):return hashlib.sha256(data).hexdigest()


def record(path):
    data=path.read_bytes()
    return {'size':len(data),'sha256':digest(data)}


def sign(package,payload):
    raw=json.dumps(payload,sort_keys=True,separators=(',',':')).encode()
    signature=subprocess.run([OPENSSL,'dgst','-sha256','-sign',str(KEY)],input=raw,capture_output=True,check=True).stdout
    envelope={'schema':1,'key_id':'c728238ab3ebabab','payload':base64.b64encode(raw).decode(),'signature':base64.b64encode(signature).decode()}
    (package/'install-manifest.json').write_text(json.dumps(envelope),encoding='utf8')


def package(tmp_path,version='0.2.34',private=True):
    folder=tmp_path/('package-'+version);folder.mkdir()
    for name in BOOTSTRAP:shutil.copy2(SCRIPTS/name,folder/name)
    runtime=folder/'payload';(runtime/'SuperMonoAgent').mkdir(parents=True)
    (runtime/'SuperMonoAgent/SuperMonoAgent.exe').write_bytes(b'MZ\0UNEXECUTED_INSTALLER_FIXTURE')
    (runtime/'README.txt').write_text('signed runtime '+version,encoding='utf8')
    profile=None
    if private:
        profile=json.dumps({'relay':RELAY,'token':TOKEN,'room_mode':'computer-name'}).encode()
        (folder/'defaults.private.json').write_bytes(profile)
    catalog={'schema':1,'format':'supermono-windows-install-v1','version':version,'arch':'x64','entry':'SuperMonoAgent/SuperMonoAgent.exe','relay':RELAY,
             'files':{p.relative_to(runtime).as_posix():record(p) for p in runtime.rglob('*') if p.is_file()},
             'bootstrap_files':{name:record(folder/name) for name in BOOTSTRAP},'defaults_private_sha256':digest(profile) if profile else None}
    sign(folder,catalog)
    return folder,catalog


def powershell(arguments,timeout=20):
    return subprocess.run([PS,'-NoLogo','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass',*map(str,arguments)],capture_output=True,text=True,encoding='utf8',errors='replace',timeout=timeout)


def install(folder,root,state):
    result=powershell(['-File',folder/'Install.ps1','-InstallRoot',root,'-StateDir',state,'-NoLaunch','-NoShortcuts','-NoRegistry'])
    assert TOKEN not in result.stdout+result.stderr
    return result


def ps_literal(value):return "'"+str(value).replace("'","''")+"'"


def test_private_install_verifies_payload_and_initializes_private_state(tmp_path):
    folder,catalog=package(tmp_path);root=tmp_path/'install space';state=tmp_path/'private state'
    result=install(folder,root,state)
    assert result.returncode==0,result.stderr
    response=json.loads(result.stdout);assert response['installed'] and response['version']=='0.2.34'
    assert (root/'versions/0.2.34/SuperMonoAgent/SuperMonoAgent.exe').read_bytes()==(folder/'payload/SuperMonoAgent/SuperMonoAgent.exe').read_bytes()
    assert (state/'token.txt').read_text()==TOKEN
    assert json.loads((state/'token-source.json').read_text())['path']==str(state/'token.txt')
    assert not (state/'relay-code').exists() and not (state/'token').exists()
    cfg=json.loads((root/'config.json').read_text());assert cfg['relay']==RELAY and cfg['token_file']==str(state/'token.txt')
    assert TOKEN not in (root/'config.json').read_text()+(root/'current.json').read_text()
    assert not (root/'defaults.private.json').exists()
    acl=powershell(['-Command',f"$p={ps_literal(state)};$f={ps_literal(state/'token.txt')};@($p,$f)|ForEach-Object{{$a=Get-Acl -LiteralPath $_;[pscustomobject]@{{protected=$a.AreAccessRulesProtected;sids=@($a.Access|ForEach-Object{{$_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value}})}}}}|ConvertTo-Json -Compress"])
    assert acl.returncode==0,acl.stderr
    records=json.loads(acl.stdout)
    sid=powershell(['-Command','[Security.Principal.WindowsIdentity]::GetCurrent().User.Value']).stdout.strip()
    assert all(row['protected'] and set(row['sids'])=={sid,'S-1-5-18'} for row in records)


def test_legacy_four_script_catalog_remains_installable(tmp_path):
    folder, catalog = package(tmp_path)
    (folder / 'Update.ps1').unlink()
    del catalog['bootstrap_files']['Update.ps1']
    sign(folder, catalog)
    root, state = tmp_path / 'legacy-install', tmp_path / 'legacy-state'
    result = install(folder, root, state)
    assert result.returncode == 0, result.stderr
    assert (root / 'Install.ps1').is_file()
    assert not (root / 'Update.ps1').exists()


def test_upgrade_keeps_old_version_credentials_pointer_and_room(tmp_path):
    first,_=package(tmp_path,'0.2.34');second,_=package(tmp_path,'0.2.35')
    root=tmp_path/'install';state=tmp_path/'state';state.mkdir()
    external=tmp_path/'existing-token.txt';external.write_text('existing-owner-fixture')
    pointer=state/'token-source.json';pointer.write_text(json.dumps({'path':str(external)}))
    (state/'relay-code').write_text('existing-room')
    before={p.name:p.read_bytes() for p in state.iterdir()}
    result=install(first,root,state);assert result.returncode==0,result.stderr
    result=install(second,root,state);assert result.returncode==0,result.stderr
    assert json.loads((root/'current.json').read_text())['version']=='0.2.35'
    assert json.loads((root/'previous.json').read_text())['version']=='0.2.34'
    assert (root/'versions/0.2.34/README.txt').is_file()
    assert {p.name:p.read_bytes() for p in state.iterdir()}==before
    assert external.read_text()=='existing-owner-fixture'
    assert not (state/'token.txt').exists()


def test_generic_package_reuses_legacy_token_without_prompt_or_rotation(tmp_path):
    folder,_=package(tmp_path,private=False);root=tmp_path/'install';state=tmp_path/'state';state.mkdir()
    (state/'token').write_text('legacy-owner-fixture')
    result=install(folder,root,state)
    assert result.returncode==0,result.stderr
    assert (state/'token').read_text()=='legacy-owner-fixture'
    assert (state/'token.txt').read_text()=='legacy-owner-fixture'


@pytest.mark.parametrize('tamper',['payload','extra','bootstrap','profile','signature','traversal','case-collision','unsigned-profile'])
def test_invalid_package_fails_before_install_or_state_changes(tmp_path,tamper):
    folder,catalog=package(tmp_path,private=tamper!='unsigned-profile')
    if tamper=='payload':(folder/'payload/README.txt').write_text('altered')
    elif tamper=='extra':(folder/'payload/extra.txt').write_text('unexpected')
    elif tamper=='bootstrap':(folder/'Launch.ps1').write_text('throw "altered"')
    elif tamper=='profile':(folder/'defaults.private.json').write_text(json.dumps({'relay':RELAY,'token':'other-fixture','room_mode':'computer-name'}))
    elif tamper=='signature':
        envelope=json.loads((folder/'install-manifest.json').read_text());envelope['signature']=base64.b64encode(b'x'*384).decode();(folder/'install-manifest.json').write_text(json.dumps(envelope))
    elif tamper=='traversal':catalog['files']['../escape.txt']={'size':0,'sha256':digest(b'')};sign(folder,catalog)
    elif tamper=='case-collision':catalog['files']['readme.txt']=catalog['files']['README.txt'];sign(folder,catalog)
    elif tamper=='unsigned-profile':(folder/'defaults.private.json').write_text('{}')
    root=tmp_path/'install';state=tmp_path/'state'
    result=install(folder,root,state)
    assert result.returncode!=0
    assert not root.exists() and not state.exists()


def test_launch_plan_has_no_token_value_and_restores_process_environment(tmp_path):
    folder,_=package(tmp_path);root=tmp_path/'install';state=tmp_path/'state'
    assert install(folder,root,state).returncode==0
    script=f""". {ps_literal(root/'Launch.ps1')} -InstallRoot {ps_literal(root)}
function Test-SmaMainWindow {{return $false}}
function Test-SmaDesktopRuntime {{return $false}}
function Start-SmaUpdateWorker {{return $false}}
$env:SUPERMONO_STATE_DIR='before-fixture'
function Start-Process {{param($FilePath,$ArgumentList,$WorkingDirectory,$WindowStyle,[switch]$PassThru) $script:seen=@{{file=$FilePath;args=$ArgumentList;window=$WindowStyle;state=$env:SUPERMONO_STATE_DIR}};return [pscustomobject]@{{HasExited=$true;ExitCode=0}}}}
$result=Invoke-SmaLaunch {ps_literal(root)}
@{{result=$result;seen=$script:seen;restored=$env:SUPERMONO_STATE_DIR}}|ConvertTo-Json -Depth 5 -Compress
"""
    result=powershell(['-Command',script]);assert result.returncode==0,result.stderr
    report=json.loads(result.stdout)
    assert report['result']['mode']=='browser'
    assert '--workspace' in report['seen']['args'] and '--token-file' in report['seen']['args']
    assert '--code' not in report['seen']['args'] and TOKEN not in result.stdout+result.stderr
    assert report['seen']['state']==str(state) and report['restored']=='before-fixture'
    assert report['seen']['window']=='Hidden'


def test_launch_with_existing_main_window_does_not_start_another_process(tmp_path):
    script=f""". {ps_literal(SCRIPTS/'Launch.ps1')} -InstallRoot {ps_literal(tmp_path/'not-installed')}
function Test-SmaMainWindow {{return $true}}
function Show-SmaMainWindow {{$script:activated=$true;return $true}}
function Start-Process {{throw 'Duplicate window attempted'}}
$script:activated=$false
$r=Invoke-SmaLaunch {ps_literal(tmp_path/'not-installed')}
@{{result=$r;activated=$script:activated}}|ConvertTo-Json -Compress
"""
    result=powershell(['-Command',script]);assert result.returncode==0,result.stderr
    report=json.loads(result.stdout)
    assert report['result']['reason']=='existing-main-window'
    assert report['activated'] and report['result']['activated']


def test_uninstall_preserves_state_and_any_running_version(tmp_path):
    folder,_=package(tmp_path);root=tmp_path/'install';state=tmp_path/'state'
    assert install(folder,root,state).returncode==0
    snapshot={p.name:p.read_bytes() for p in state.iterdir()}
    script=f""". {ps_literal(root/'Uninstall.ps1')} -InstallRoot {ps_literal(root)} -NoShortcuts -NoRegistry
function Test-SmaVersionInUse {{param($Path) return $true}}
Invoke-SmaUninstall {ps_literal(root)}|ConvertTo-Json -Compress -Depth 4
"""
    result=powershell(['-Command',script]);assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)['preserved_versions']==['0.2.34']
    assert (root/'versions/0.2.34/SuperMonoAgent/SuperMonoAgent.exe').is_file()
    assert {p.name:p.read_bytes() for p in state.iterdir()}==snapshot


def test_uninstall_removes_only_managed_nonrunning_payload_and_keeps_state(tmp_path):
    folder,_=package(tmp_path);root=tmp_path/'install';state=tmp_path/'state'
    assert install(folder,root,state).returncode==0
    snapshot={p.name:p.read_bytes() for p in state.iterdir()}
    script=f""". {ps_literal(root/'Uninstall.ps1')} -InstallRoot {ps_literal(root)} -NoShortcuts -NoRegistry
function Test-SmaVersionInUse {{param($Path) return $false}}
Invoke-SmaUninstall {ps_literal(root)}|ConvertTo-Json -Compress -Depth 4
"""
    result=powershell(['-Command',script]);assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)['removed_versions']==['0.2.34']
    assert not (root/'versions/0.2.34').exists()
    assert {p.name:p.read_bytes() for p in state.iterdir()}==snapshot


def test_signed_package_cannot_follow_payload_junction(tmp_path):
    folder,_=package(tmp_path);outside=tmp_path/'outside';outside.mkdir();(outside/'sentinel.txt').write_text('preserved')
    junction=folder/'payload/link'
    result=subprocess.run(['cmd.exe','/c','mklink','/J',str(junction),str(outside)],capture_output=True)
    if result.returncode:pytest.skip('Directory junction fixture unavailable')
    try:
        root=tmp_path/'install';state=tmp_path/'state';result=install(folder,root,state)
        assert result.returncode!=0 and not root.exists() and not state.exists()
        assert (outside/'sentinel.txt').read_text()=='preserved'
    finally:junction.rmdir()


def test_reinstall_preserves_previous_and_enterprise_state_directory(tmp_path):
    first,_=package(tmp_path,'0.2.34');second,_=package(tmp_path,'0.2.35');root=tmp_path/'install';state=tmp_path/'enterprise state'
    assert install(first,root,state).returncode==0
    assert install(second,root,state).returncode==0
    before=(root/'previous.json').read_bytes();token=(state/'token.txt').read_bytes()
    # Omitting StateDir during an upgrade must reuse the existing per-user setting.
    result=powershell(['-File',second/'Install.ps1','-InstallRoot',root,'-NoLaunch','-NoShortcuts','-NoRegistry'])
    assert result.returncode==0,result.stderr
    assert (root/'previous.json').read_bytes()==before and (state/'token.txt').read_bytes()==token
    other=tmp_path/'unrelated state'
    result=install(second,root,other)
    assert result.returncode!=0 and not other.exists()


def test_launch_failure_is_not_reported_as_started(tmp_path):
    folder,_=package(tmp_path);root=tmp_path/'install';state=tmp_path/'state';assert install(folder,root,state).returncode==0
    script=f""". {ps_literal(root/'Launch.ps1')} -InstallRoot {ps_literal(root)}
function Test-SmaMainWindow {{return $false}}
function Test-SmaDesktopRuntime {{return $false}}
function Start-SmaUpdateWorker {{return $false}}
function Start-Process {{param($FilePath,$ArgumentList,$WorkingDirectory,$WindowStyle,[switch]$PassThru) return [pscustomobject]@{{HasExited=$true;ExitCode=9}}}}
$env:SUPERMONO_STATE_DIR='original-fixture'
try {{Invoke-SmaLaunch {ps_literal(root)};throw 'Failure was ignored'}}catch {{if($_.Exception.Message -eq 'Failure was ignored'){{throw}};@{{failed=$true;restored=$env:SUPERMONO_STATE_DIR}}|ConvertTo-Json -Compress}}
"""
    result=powershell(['-Command',script]);assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)=={'failed':True,'restored':'original-fixture'}


def test_runtime_detection_uses_documented_webview_registry_location():
    script=f""". {ps_literal(SCRIPTS/'Launch.ps1')}
$script:probed=@()
function Get-ItemProperty {{param($Path,$Name,$ErrorAction) if($Name -eq 'Release'){{return [pscustomobject]@{{Release=528040}}}};$script:probed+=@($Path);if($Path -eq 'HKCU:\\SOFTWARE\\Microsoft\\EdgeUpdate\\Clients\\{{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}}'){{return [pscustomobject]@{{pv='120.0.0.0'}}}};throw 'not installed'}}
@{{available=(Test-SmaDesktopRuntime);paths=$script:probed}}|ConvertTo-Json -Compress
"""
    result=powershell(['-Command',script]);assert result.returncode==0,result.stderr
    report=json.loads(result.stdout);assert report['available']
    assert report['paths']==['HKCU:\\SOFTWARE\\Microsoft\\EdgeUpdate\\Clients\\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}']


def test_private_cmd_entry_installs_without_prompting_or_executing_payload(tmp_path):
    folder,_=package(tmp_path);root=tmp_path/'cmd install';state=tmp_path/'cmd state'
    result=subprocess.run([str(folder/'Install.cmd'),'-InstallRoot',str(root),'-StateDir',str(state),'-NoLaunch','-NoShortcuts','-NoRegistry'],
                          capture_output=True,text=True,encoding='utf8',errors='replace',timeout=20,creationflags=subprocess.CREATE_NO_WINDOW)
    assert result.returncode==0,result.stderr
    assert (root/'current.json').is_file() and (state/'token.txt').read_text()==TOKEN
    assert TOKEN not in result.stdout+result.stderr


def test_single_shortcut_uses_stable_launcher_and_preserves_foreign_link(tmp_path):
    root=tmp_path/'install';root.mkdir();desktop=tmp_path/'temporary desktop';desktop.mkdir()
    executable=root/'fixture.exe';executable.write_bytes(b'MZ_UNEXECUTED_FIXTURE')
    script=f""". {ps_literal(SCRIPTS/'Install.ps1')}
New-SmaShortcut {ps_literal(root)} {ps_literal(executable)} {ps_literal(desktop)}
$shell=New-Object -ComObject WScript.Shell;$link=$shell.CreateShortcut({ps_literal(desktop/'SuperMonoAgent.lnk')})
@{{target=$link.TargetPath;args=$link.Arguments;icon=$link.IconLocation;description=$link.Description}}|ConvertTo-Json -Compress
"""
    result=powershell(['-Command',script]);assert result.returncode==0,result.stderr
    report=json.loads(result.stdout)
    assert report['target'].lower().endswith('powershell.exe')
    assert str(root/'Launch.ps1') in report['args'] and report['icon']==str(executable)+',0'
    assert len(list(desktop.iterdir()))==1
    foreign=f"""$shell=New-Object -ComObject WScript.Shell;$path={ps_literal(desktop/'SuperMonoAgent.lnk')};$link=$shell.CreateShortcut($path);$link.Description='Foreign link';$link.Save()
$before=[Convert]::ToBase64String([IO.File]::ReadAllBytes($path))
. {ps_literal(SCRIPTS/'Install.ps1')}
try{{New-SmaShortcut {ps_literal(root)} {ps_literal(executable)} {ps_literal(desktop)};throw 'Foreign link replaced'}}catch{{if($_.Exception.Message -eq 'Foreign link replaced'){{throw}}}}
if([Convert]::ToBase64String([IO.File]::ReadAllBytes($path)) -ne $before){{throw 'Foreign bytes changed'}}
Write-Output 'foreign-preserved'
"""
    result=powershell(['-Command',foreign]);assert result.returncode==0,result.stderr
    assert result.stdout.strip()=='foreign-preserved'
