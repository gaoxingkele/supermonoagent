"""Prevent a Windows artifact from being labelled with another source version."""
from pathlib import Path
import subprocess
import tomllib

from supermonoagent import __version__

ROOT = Path(__file__).resolve().parents[1]


def test_project_and_source_release_versions_match():
    assert tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf-8'))['project']['version'] == __version__


def test_windows_build_rejects_different_version_before_compilation():
    result = subprocess.run(['powershell.exe', '-NoProfile', '-File', str(ROOT / 'scripts/build-windows.ps1'),
        '-Python', __import__('sys').executable, '-Version', '999.0.0'],
        capture_output=True, timeout=20)
    assert result.returncode != 0
    assert b'Requested Windows version does not match the source release version' in result.stderr
