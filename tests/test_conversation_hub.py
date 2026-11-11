import asyncio
import base64

from supermonoagent.conversation_manager import ConversationManager
from supermonoagent.hub import Hub
from test_windows import FakePty


def test_hub_captures_exact_bytes_without_viewer_and_detach_does_not_seal(tmp_path):
    async def run():
        manager = ConversationManager(tmp_path, default_token='owner', drain_seconds=0)
        hub = Hub(pty_factory=lambda *_: FakePty())
        hub.default_owner = 'owner'
        hub.groups['default'].owner = 'owner'
        hub.journals = manager
        hub.attach_loop(asyncio.get_running_loop())
        window = hub.open_window(owner='owner')
        target = hub.open_cli(window.id, 'codex')
        assert target.journal_id
        client = hub.attach(asyncio.Queue(), token='owner')
        client.conversation_journal = True
        assert hub.summary('owner')['capabilities']['conversation_journal'] == 1
        hub.detach(client.key)
        raw = b'\x1b[31mfirst\x00\xff\r\nlast'
        target.pty.emit(raw)
        for _ in range(100):
            await asyncio.sleep(.01)
            if manager.capture.status()['committed_items']:
                break
        scope = manager.owner_scope('owner')
        assert manager.store.status(scope, target.journal_id)['terminal_state'] == 'running'
        target.pty.close()
        for _ in range(200):
            await asyncio.sleep(.01)
            if manager.store.status(scope, target.journal_id)['capture_state'] == 'sealed':
                break
        page = manager.store.read_page(scope, target.journal_id)
        rows = [row for row in page['events'] if row['kind'] == 'raw_output']
        saved = b''.join(base64.b64decode(manager.store.read_content(scope, target.journal_id,
            row['content_id'])['data_base64']) for row in rows)
        assert saved == raw
        assert manager.store.status(scope, target.journal_id)['capture_state'] == 'sealed'
        await manager.aclose()
    asyncio.run(run())
