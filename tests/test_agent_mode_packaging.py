"""Native API2 fixed JSON routes and revision-pinned desktop snapshot coverage."""
import asyncio
import json
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest

from supermonoagent import web,ui_updates
from supermonoagent.frontend import UI_FILES,NATIVE_UPDATE_API
from test_desktop_ui_updates import frontend_fixture,signer

MODES=('codex','claude-code','grok','kimi','pi-dev','cursor-cli')


def test_fixed_profile_resources_and_api2_catalogs():
    assert ui_updates.NATIVE_API==NATIVE_UPDATE_API==2
    assert len(ui_updates.FILES['desktop'])==len(UI_FILES)==17
    assert len(ui_updates.FILES['android'])==18
    assert set(UI_FILES)==ui_updates.FILES['desktop']
    for mode in MODES:
        path='agent-modes/'+mode+'.json'
        assert path in ui_updates.FILES['desktop'] and 'mobile/'+path in ui_updates.FILES['android']
        raw,mime=web.page_content('/'+path)
        assert mime.startswith('application/json') and json.loads(raw)['id']==mode
    for path in ['/agent-modes/unknown.json','/agent-modes/../update-trust.json','/agent-modes/%2e%2e/update-trust.json']:
        assert web.page_content(path) is None
    root=Path(__file__).resolve().parents[1]
    assert 'static/agent-modes/*.json' in (root/'pyproject.toml').read_text()
    assert json.loads((root/'android/assets/update-api.json').read_text())['native_api']==2


def test_api2_fixed_json_routes_are_revision_pinned(tmp_path,signer):
    asyncio.run(_routes(tmp_path,signer))


async def _routes(tmp_path,signer):
    async with frontend_fixture(tmp_path,signer) as (frontend,_feed,_daemon,_session,_other,origin):
        def get(path):
            with urlopen(origin+path,timeout=5) as response:return response.read(),response.headers['Content-Type']
        for mode in MODES:
            path='agent-modes/'+mode+'.json'
            raw,mime=await asyncio.to_thread(get,'/__ui/bundled/'+path+'?bundle='+frontend.bundle_fingerprint)
            assert mime.startswith('application/json')
            assert raw==frontend.update_store.snapshot('bundled')[path] and json.loads(raw)['id']==mode
        for path in ['/__ui/bundled/agent-modes/unknown.json','/__ui/bundled/agent-modes/../update-trust.json','/__ui/bundled/agent-modes/%2e%2e/update-trust.json']:
            with pytest.raises(HTTPError) as error:await asyncio.to_thread(get,path)
            assert error.value.code==404
