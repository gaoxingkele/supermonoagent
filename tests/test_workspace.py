"""Workspace safety boundaries supplement the browser integration suite."""
import re
from pathlib import Path

from supermonoagent.workspace import PAGE


def test_workspace_assets_exist_and_are_same_origin():
    static = Path(__file__).parents[1] / "src" / "supermonoagent" / "static"
    for asset in re.findall(r'(?:src|href)="(/[^"?]+\.(?:js|css))"', PAGE):
        assert (static / asset.lstrip("/")).is_file(), asset
    assert "https://" not in PAGE


def test_workspace_exposes_separate_detach_and_termination_controls():
    assert 'id="terminate-dialog"' in PAGE
    assert "关闭视图会保留后台进程" in PAGE
    assert 'id="renderer"' in PAGE
    assert 'id="softkeys"' in PAGE
