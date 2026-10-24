"""A GUI executable may start with its options instead of an explicit MODE."""
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from supermonoagent import launcher


@pytest.mark.parametrize('arguments',[
    ['--relay','wss://fixture.invalid/ws','--token','fixture-owner'],
    ['--relay=wss://fixture.invalid/ws','--token-file','C:/private/token.txt'],
    ['--state-dir','C:/fixture state','--no-start'],
])
def test_flag_first_defaults_to_workspace_and_forwards_all_options(arguments):
    module=SimpleNamespace(main=Mock())
    with patch.object(launcher.importlib,'import_module',return_value=module) as load:
        launcher.main(arguments)
    load.assert_called_once_with('supermonoagent.desktop')
    module.main.assert_called_once_with(arguments)


def test_explicit_workspace_keeps_its_existing_routing():
    module=SimpleNamespace(main=Mock())
    with patch.object(launcher.importlib,'import_module',return_value=module) as load:
        launcher.main(['workspace','--relay','wss://fixture.invalid/ws','--token','fixture-owner'])
    load.assert_called_once_with('supermonoagent.desktop')
    module.main.assert_called_once_with(['--relay','wss://fixture.invalid/ws','--token','fixture-owner'])


@pytest.mark.parametrize('flag',['-h','--help'])
def test_top_level_help_stays_top_level_without_importing_a_client(flag,capsys):
    with patch.object(launcher.importlib,'import_module') as load:
        launcher.main([flag])
    load.assert_not_called()
    assert 'Use MODE --help' in capsys.readouterr().out


def test_unknown_nonflag_mode_is_still_rejected():
    with patch.object(launcher.importlib,'import_module') as load:
        with pytest.raises(SystemExit) as error:launcher.main(['not-a-mode','--token','fixture'])
    assert error.value.code==2
    load.assert_not_called()


def test_explicit_host_forwards_arguments_and_restores_sys_argv():
    seen=[]
    def host():seen.append(list(launcher.sys.argv))
    original=['SuperMonoAgent.exe','unrelated-original']
    module=SimpleNamespace(main=host)
    with patch.object(launcher.sys,'argv',original),patch.object(launcher.importlib,'import_module',return_value=module) as load:
        launcher.main(['host','--relay','wss://fixture.invalid/ws','--token','fixture-owner'])
        assert launcher.sys.argv is original
    load.assert_called_once_with('supermonoagent.daemon')
    assert seen==[['SuperMonoAgent.exe','--relay','wss://fixture.invalid/ws','--token','fixture-owner']]
