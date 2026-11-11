"""Validate visual-fixture HTTP/WS boundaries without opening a browser or CLI."""
import asyncio
import json
import urllib.request

from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from android_journal_fixture import Fixture, TOKEN, ROOM, packed


def test_android_fixture_uses_actual_assets_and_archive_survives_host_shutdown():
    asyncio.run(check_fixture())


async def check_fixture():
    fixture = Fixture()
    async with serve(fixture.socket, '127.0.0.1', 0, process_request=fixture.http) as server:
        fixture.origin = 'http://127.0.0.1:' + str(server.sockets[0].getsockname()[1])
        def get(route):
            return urllib.request.urlopen(fixture.origin + route).read()
        html = (await asyncio.to_thread(get, '/mobile/index.html')).decode()
        assert 'id="journal-history"' in html
        script = (await asyncio.to_thread(get, '/mobile/vendor/terminal-client.js')).decode()
        assert 'function createConversationViewer' in script
        address = fixture.origin.replace('http:', 'ws:')
        async with connect(address + '/ws') as host, connect(address + '/journals/ws') as archive:
            await host.send(json.dumps(dict(op='phone', code=ROOM, token=TOKEN)))
            assert json.loads(await host.recv())['op'] == 'ready'
            await host.send(packed(1, dict(token=TOKEN, multiplex=True)))
            catalog = json.loads((await host.recv())[5:])
            assert catalog['capabilities']['conversation_journal'] == 1
            await archive.send(json.dumps(dict(op='hello', protocol='sma-journal-1', role='viewer', token=TOKEN, room=ROOM)))
            assert json.loads(await archive.recv())['ok']
            await asyncio.to_thread(get, '/fixture/offline')
            assert not fixture.online
            await archive.send(json.dumps(dict(op='request', request_id='archive-test', action='read_page', params={'journal_id': 'j0', 'projection': 'conversation'})))
            page = json.loads(await archive.recv())['result']
            assert page['journal']['capture_state'] == 'sealed'
            assert [event['kind'] for event in page['events']] == ['user_input', 'assistant_final']
            assert fixture.input_frames == fixture.resize_frames == 0
