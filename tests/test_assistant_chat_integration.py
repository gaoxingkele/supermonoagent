import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from types import SimpleNamespace
import threading

from supermonoagent.desktop_assistant import DesktopAssistant
from test_workspace_profile_rooms import fixture, run


def test_chat_uses_real_local_model_adapter_without_terminal_actions(tmp_path):
    run(lambda playwright: exercise(playwright, tmp_path))


async def exercise(playwright, tmp_path):
    calls = []
    answer = 'Use meta-rsi; record independent evidence. <img src=x onerror=alert(1)>'
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            calls.append({'body': body, 'authorization': self.headers.get('Authorization')})
            data = json.dumps({'choices': [{'message': {'role': 'assistant', 'content': answer}, 'finish_reason': 'stop'}]}).encode()
            self.send_response(200); self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    class Event:
        def __iadd__(self, _callback): return self
    assistant = None
    try:
        async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages, _daemons, _ops, _windows, sessions):
            window = SimpleNamespace(expose=lambda *_: None, get_current_url=lambda: url,
                events=SimpleNamespace(loaded=Event(), closed=Event()))
            assistant = DesktopAssistant(window, url, tmp_path / 'library', preferences_path=tmp_path / 'preferences.json')
            profile = assistant.get_assistant_preferences()['preferences']
            profile.update(provider='api', model='fixture-model', endpoint='http://127.0.0.1:' + str(server.server_port) + '/v1')
            assistant.save_assistant_preferences(profile)
            assistant.set_assistant_api_key('fixture-model-key')
            page = await browser.new_page(viewport={'width': 1200, 'height': 900})
            await page.goto(url)
            await page.wait_for_function('smaWorkspace?.connected&&smaWorkspace.activePane?.ready')
            for name, method in [('prefs', assistant.get_assistant_preferences), ('chat', assistant.assistant_chat),
                ('recommend', assistant.recommend_skills), ('cancel', assistant.cancel_assistant_chat)]:
                await page.expose_function('fixture_' + name, method)
            await page.evaluate('''()=>{window.pywebview={api:{get_assistant_preferences:fixture_prefs,assistant_chat:fixture_chat,recommend_skills:fixture_recommend,cancel_assistant_chat:fixture_cancel}};dispatchEvent(new Event('pywebviewready'));}''')
            await page.locator('#mylib-toggle').click()
            await page.locator('#assistant-chat-question').fill('Which skill helps verify an improvement?')
            await page.locator('#assistant-chat-send').click()
            await page.wait_for_function('document.querySelector("#assistant-chat-history").textContent.includes("independent evidence")')
            assert len(calls) == 1
            request = calls[0]
            assert request['authorization'] == 'Bearer fixture-model-key'
            assert request['body']['tools'] == [] and request['body']['tool_choice'] == 'none'
            assert request['body']['store'] is False
            assert 'fixture-model-key' not in json.dumps(request['body'])
            assert json.loads(request['body']['messages'][-1]['content'])['terminal_context'] == ''
            assert await page.locator('#assistant-chat-history img').count() == 0
            assert answer in await page.locator('#assistant-chat-history').inner_text()
            assert not any(session.pty.written for owned in sessions for session in owned)
            assert 'fixture-model-key' not in (tmp_path / 'preferences.json').read_text()
    finally:
        if assistant:
            assistant._close()
        server.shutdown(); server.server_close(); thread.join(2)
        assert not thread.is_alive()
