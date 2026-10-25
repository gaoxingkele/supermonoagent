"""Run the Android overview contract against release archive bytes, not loose files."""
import asyncio
import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_packaged_mobile_overview_defaults_to_sixteen_and_remains_readonly(tmp_path):
    pytest.importorskip('playwright.async_api')
    if not Path('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe').exists():
        pytest.skip('Local Edge required')
    release = load('overview_release_contract', ROOT / 'scripts/ui_update_release.py')
    files = release.files_for_platform(ROOT, 'android',
                                      'https://117.50.48.22/updates/stable/manifest.json')
    archive = tmp_path / 'android.zip'
    release.make_archive(archive, files)
    probe = load('overview_packaged_ui_probe', ROOT / 'android/tests/debug-grid-ui.py')
    probe.OUT = tmp_path / 'browser'
    asyncio.run(probe.main(android_zip=archive))
