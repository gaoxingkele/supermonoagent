from pathlib import Path

from supermonoagent.frontend import bundled_ui_files, UI_FILES
from supermonoagent.workspace import javascript
from supermonoagent import web


def test_features_travel_in_existing_signed_workspace_resource():
    bundle = bundled_ui_files()
    assert set(bundle) == set(UI_FILES) and len(bundle) == 17
    code = bundle['workspace.js'].decode('utf-8')
    assert code == javascript() == web.static_text('workspace.js')
    for marker in ('queue_sidecar_request',):
        assert marker not in code  # No backend code or credentials in UI bytes.
    assert 'window.SuperMonoFilePaste=' in code
    assert "toggle.id='mylib-toggle'" in code


def test_legacy_static_tree_keeps_terminal_core_when_optional_files_missing(tmp_path):
    (tmp_path / 'workspace.js').write_text('window.coreFixture=true;', encoding='utf-8')
    assert javascript(tmp_path) == 'window.coreFixture=true;'
