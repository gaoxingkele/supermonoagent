"""Explicit OSC agent identity reaches chooser catalogs without output floods."""
from unittest.mock import Mock

from supermonoagent.hub import Hub, Session


def test_agent_identity_catalog_changes_only_when_explicit_app_changes():
    hub = Hub()
    window = hub.open_window('Build', owner='owner')
    session = Session('12345678', window.id, 'powershell', '.', object())
    hub.sessions[session.id] = session
    window.sessions[session.id] = session
    hub._catalog = Mock()

    def catalog_session():
        return hub.summary('owner')['windows'][0]['sessions'][0]

    assert 'agent' not in catalog_session()
    hub._task_output(session.id, b'ordinary output')
    assert hub._catalog.call_count == 0
    hub._task_output(session.id, b'\x1b]7501;app=cod')
    assert hub._catalog.call_count == 0
    hub._task_output(session.id, b'ex;state=idle\x07')
    assert catalog_session()['agent'] == 'codex'
    assert hub._catalog.call_count == 1
    hub._task_output(session.id, b'\x1b]7501;app=codex;state=working\x07')
    assert hub._catalog.call_count == 1
    assert hub.summary('other')['windows'] == []
    hub._task_output(session.id, b'\x1b]7501;state=clear\x07')
    assert 'agent' not in catalog_session()
    assert hub._catalog.call_count == 2
