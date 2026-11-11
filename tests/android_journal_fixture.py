"""Loopback-only Android visual fixture. No PTY, provider process, or real credentials.

Run with the repository virtualenv, then open the printed URL through CUA.
The actual Android assets and shared renderer are served without DOM injection.
"""
import argparse
import asyncio
import base64
import hashlib
import json
import mimetypes
import struct
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from websockets.asyncio.server import serve
from websockets.datastructures import Headers
from websockets.http11 import Response

ROOT = Path(__file__).resolve().parents[1]
TOKEN, ROOM = 'android-journal-synthetic-viewer', 'synthetic-room'
STAMP = '2026-11-11T08:30:00Z'
RAW = ('\x1b[32mSynthetic Codex terminal / 只读测试数据\x1b[0m\r\n'
       '原始终端仍持续解析；没有连接任何真实 CLI。\r\n'
       '权限请求示例：请自行检查，记录界面不会自动接受。\r\n'
       'codex> 等待输入（此测试夹具不会执行输入）\r\n').encode()


def packed(op, value):
    body = bytes([op]) + (json.dumps(value, ensure_ascii=False).encode() if isinstance(value, dict) else value)
    return struct.pack('<I', len(body)) + body


class Fixture:
    def __init__(self):
        self.origin = ''
        self.online = True
        self.terminals = set()
        self.input_frames = self.resize_frames = 0
        self.bound = {'j1': True, 'j2': True}
        self.providers = {'j1': 'codex', 'j2': 'codex'}
        self.contents = {}

    def manifest(self, jid):
        closed = jid == 'j0'
        provider = self.providers.get(jid, 'codex')
        return dict(schema_version=1, journal_id=jid, host_installation_id='synthetic-host',
                    owner_scope='synthetic-owner', room=ROOM, source_epoch='fixture-epoch',
                    window_id='w1' if jid == 'j1' else 'w2', session_id='s1' if jid == 'j1' else 's2',
                    process_generation='pty-fixture-' + jid, title='合成会话 · ' + jid, agent=provider,
                    created_at=STAMP, capture_started_at=STAMP, coverage='full',
                    terminal_state='ended' if closed else 'running', capture_state='sealed' if closed else 'capturing',
                    sealed_at=STAMP if closed else None, last_seq=4, local_error=None,
                    source=dict(bound=self.bound.get(jid, True), supported=provider == 'codex',
                                healthy=provider == 'codex', reason='verified_synthetic_fixture' if provider == 'codex' else 'claude_finality_unproven'),
                    storage=dict(available=True), mirror=dict(through_seq=4, destination_id='fixture-server', last_receipt={'commit_id': 'fixture'}))

    def events(self, jid):
        paragraphs = '\n\n'.join(f'{n}. 中文阅读段落：保留原始文本、时间和来源。关闭记录窗口不会结束后台会话；离线归档仍可读取。😀' for n in range(1, 16))
        rows = [
            ('user_input', 'user', 'accepted', 'provider_user_item', '请解释这次合成记录，并给出包含中文和代码的最终答复。'),
            ('raw_output', 'terminal', 'observed', None, RAW.decode()),
            ('assistant_candidate', 'assistant', 'candidate', None, '候选答复仅在历史可见；不会冒充已经确认的结论。'),
            ('assistant_final', 'assistant', 'confirmed', 'provider_final_phase', '已完成合成示例。\n\n' + paragraphs + '\n\n```python\nprint("中文原文 😀")\n```\n\n最终结论：保留全文，代码默认折叠。'),
        ]
        result = []
        for seq, (kind, actor, state, proof, text) in enumerate(rows, 1):
            body = text.encode(); cid = f'{jid}-content-{seq}'; self.contents[cid] = body
            result.append(dict(event_id=f'{jid}-e{seq}', seq=seq, kind=kind, actor=actor, state=state,
                               proof=proof, origin='provider', observed_at=STAMP, source_created_at=STAMP,
                               content_id=cid, content_bytes=len(body), encoding='utf-8',
                               content_sha256=hashlib.sha256(body).hexdigest(), preview=text[:1024]))
        return result

    def query(self, action, params):
        jid = params.get('journal_id') or ('j2' if params.get('session_id') == 's2' else 'j1')
        if jid not in {'j0', 'j1', 'j2'}:
            raise ValueError('not_found')
        if action == 'status':
            return self.manifest(jid)
        if action == 'list':
            return dict(journals=[self.manifest(j) for j in ['j1', 'j2', 'j0']], next_cursor=None)
        if action == 'read_page':
            events = self.events(jid)
            if params.get('projection') == 'conversation':
                events = [row for row in events if row['kind'] in {'user_input', 'assistant_final'}]
            return dict(journal=self.manifest(jid), events=events, next_cursor=None)
        if action == 'read_content':
            cid = params['content_id']; body = self.contents[cid]; offset = params.get('offset', 0)
            chunk = body[offset:offset + min(params.get('limit', 49152), 49152)]
            end = offset + len(chunk)
            return dict(content_id=cid, offset=offset, data_base64=base64.b64encode(chunk).decode(),
                        total_bytes=len(body), sha256=hashlib.sha256(body).hexdigest(), next_offset=end if end < len(body) else None)
        if action == 'list_sources':
            provider = params['provider']
            return dict(sources=[dict(source_ref='opaque-source-' + provider, provider=provider,
                                      provider_session_id='exact-synthetic-root-' + provider,
                                      provider_version='0.167' if provider == 'codex' else '2.1.310', supported=provider == 'codex')])
        if action == 'bind_source':
            if params.get('terminal_generation') != self.manifest(jid)['process_generation']:
                raise ValueError('terminal_generation_mismatch')
            provider = {'opaque-source-codex': 'codex', 'opaque-source-claude': 'claude'}[params['source_ref']]
            self.bound[jid] = True; self.providers[jid] = provider
            return self.manifest(jid)
        raise ValueError('unsupported')

    def catalog(self):
        return dict(capabilities=dict(multiplex=True, targeted_input=True, resize_lease=True, snapshot_sgr=True,
                                      snapshot_refresh=True, conversation_journal=1), machine='Synthetic Android fixture',
                    project='No real CLI', selected='w1', session='s1',
                    controls={sid: dict(cols=80, rows=24, owner=False, busy=False) for sid in ['s1', 's2']},
                    windows=[dict(id='w1', title='Codex 合成会话', focused='s1', sessions=[dict(id='s1', shell='codex', agent='codex', journal_id='j1')]),
                             dict(id='w2', title='另一独立窗口', focused='s2', sessions=[dict(id='s2', shell='codex', agent='codex', journal_id='j2')])])

    async def socket(self, ws):
        archive = urlsplit(ws.request.path).path == '/journals/ws'
        if archive:
            greeting = json.loads(await ws.recv())
            if greeting != dict(op='hello', protocol='sma-journal-1', role='viewer', token=TOKEN, room=ROOM):
                await ws.close(4001); return
            await ws.send(json.dumps(dict(op='hello', protocol='sma-journal-1', ok=True, role='viewer')))
        try:
            async for frame in ws:
                if archive:
                    value = json.loads(frame)
                    if value.get('op') != 'request' or value.get('action') not in {'status', 'list', 'read_page', 'read_content'}:
                        await ws.close(4001); return
                    await ws.send(json.dumps(self.response(value), ensure_ascii=False)); continue
                if isinstance(frame, str):
                    value = json.loads(frame)
                    if value.get('op') == 'watch':
                        await ws.send(json.dumps(dict(op='directory', revision=0, hosts=[], capabilities={}))); continue
                    if value.get('op') == 'phone' and value.get('token') == TOKEN and value.get('code') == ROOM:
                        if not self.online:
                            await ws.close(4004); return
                        await ws.send(json.dumps(dict(op='ready'))); continue
                    await ws.close(4001); return
                op, payload = frame[4], frame[5:]
                if op == 1:
                    if json.loads(payload).get('token') != TOKEN:
                        await ws.close(4001); return
                    self.terminals.add(ws); await ws.send(packed(2, self.catalog()))
                elif op == 16:
                    for sid in json.loads(payload).get('sessions', []):
                        await ws.send(packed(8, sid.encode() + b'\0' + RAW))
                elif op == 10:
                    await ws.send(packed(11, payload))
                elif op == 24:
                    await ws.send(packed(24, self.response(json.loads(payload))))
                elif op in {4, 17}:
                    self.input_frames += 1  # Deliberately no execution or echo.
                elif op == 5:
                    self.resize_frames += 1
        finally:
            self.terminals.discard(ws)

    def response(self, value):
        response = dict(op='response', request_id=value.get('request_id'))
        try:
            response.update(ok=True, result=self.query(value['action'], value.get('params', {})))
        except (KeyError, ValueError):
            response.update(ok=False, error='unsupported')
        return response

    def http_response(self, status, body, mime='text/html; charset=utf-8'):
        data = body.encode() if isinstance(body, str) else body
        return Response(status, 'OK' if status == 200 else 'Not Found', Headers({'Content-Type': mime, 'Content-Length': str(len(data)), 'Cache-Control': 'no-store'}), data)

    async def http(self, connection, request):
        url = urlsplit(request.path); route = unquote(url.path)
        if request.headers.get('Upgrade', '').lower() == 'websocket':
            return None
        if route == '/':
            config = dict(endpoint=self.origin, code=ROOM, token=TOKEN, renderer='dom', uiRevision='journal-synthetic')
            target = '/mobile/index.html#' + quote(json.dumps(config))
            return self.http_response(200, '<meta charset="utf-8"><title>Android journal fixture</title><h1>Synthetic Android journal</h1>'
                                      f'<p><a href="{target}">打开实际 Android 页面</a></p>'
                                      '<p><a href="/fixture/offline">断开合成主机（归档继续在线）</a></p>'
                                      '<p><a href="/fixture/online">恢复合成主机</a></p><p><a href="/fixture/metrics">合成帧计数</a></p>')
        if route == '/fixture/offline':
            self.online = False
            for ws in list(self.terminals):
                await ws.close(4004)
            return self.http_response(200, '<meta charset="utf-8">合成主机已离线。原 Android 标签页可从历史记录读取中央归档。')
        if route == '/fixture/online':
            self.online = True
            return self.http_response(200, '<meta charset="utf-8">合成主机已恢复；Android 页面会自动重连。')
        if route == '/fixture/metrics':
            return self.http_response(200, json.dumps(dict(input_frames=self.input_frames, resize_frames=self.resize_frames, host_online=self.online)), 'application/json')
        if route.startswith('/mobile/'):
            name = route[len('/mobile/'):]
            base = ROOT / ('src/supermonoagent/static' if name.startswith('vendor/') else 'android/assets/mobile')
            asset = base / (name[7:] if name.startswith('vendor/') else name)
            if asset.resolve().is_relative_to(base.resolve()) and asset.is_file():
                mime = mimetypes.guess_type(str(asset))[0] or 'text/javascript'
                return self.http_response(200, asset.read_bytes(), mime + '; charset=utf-8')
        return self.http_response(404, 'not found', 'text/plain')


async def main(port):
    fixture = Fixture()
    async with serve(fixture.socket, '127.0.0.1', port, process_request=fixture.http) as server:
        fixture.origin = 'http://127.0.0.1:' + str(server.sockets[0].getsockname()[1])
        print(fixture.origin + '/', flush=True)
        await asyncio.Future()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('--port', type=int, default=8767)
    asyncio.run(main(parser.parse_args().port))
