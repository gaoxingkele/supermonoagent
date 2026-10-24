import hashlib
import asyncio
import importlib.util
import json
from pathlib import Path
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler

import pytest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('download_server',ROOT/'deploy/downloads/download_server.py')
downloads=importlib.util.module_from_spec(spec);spec.loader.exec_module(downloads)


@pytest.fixture
def broker(tmp_path):
    key='fixture-private-download-key-'+'a'*32
    body=b'private-installer-fixture'
    (tmp_path/'installer.zip').write_bytes(body)
    record={'key_sha256':hashlib.sha256(key.encode()).hexdigest(),'file':'installer.zip','download_name':'SuperMonoAgent-0.2.34-Setup.zip','sha256':hashlib.sha256(body).hexdigest(),'size':len(body),'expires':time.time()+60}
    (tmp_path/'registry.json').write_text(json.dumps({'downloads':[record]}))
    server=downloads.DownloadServer(('127.0.0.1',0),tmp_path)
    job=threading.Thread(target=server.serve_forever);job.start()
    try:yield f'http://127.0.0.1:{server.server_port}',key,body,record,tmp_path
    finally:server.shutdown();job.join(3);server.server_close()


def fetch(url,key=None,method='GET'):
    headers={} if key is None else {'Authorization':'Bearer '+key}
    return build_opener(ProxyHandler({})).open(Request(url,headers=headers,method=method),timeout=3)


def test_archive_requires_bearer_and_never_accepts_query_or_path_keys(broker):
    base,key,body,*_=broker
    for path in ['/install/archive','/install/archive?key='+key,'/install/'+key+'/archive','/registry.json','/installer.zip']:
        with pytest.raises(HTTPError) as error:fetch(base+path)
        assert error.value.code==404
    with fetch(base+'/install/archive',key) as response:
        assert response.read()==body
        assert response.headers['Cache-Control']=='no-store'
        assert response.headers['X-Content-SHA256']==hashlib.sha256(body).hexdigest()
        assert 'Access-Control-Allow-Origin' not in response.headers
    with fetch(base+'/install/archive',key,method='HEAD') as response:assert response.read()==b''


def test_wrong_expired_or_escaping_records_are_indistinguishable(broker):
    base,key,_body,record,root=broker
    with pytest.raises(HTTPError) as error:fetch(base+'/install/archive','b'*60)
    assert error.value.code==404
    for change in [{'expires':time.time()-1},{'expires':float('nan')},{'file':'../installer.zip'},{'download_name':'x\r\nBad: injected'},{'size':1000000}]:
        (root/'registry.json').write_text(json.dumps({'downloads':[{**record,**change}]}))
        with pytest.raises(HTTPError) as error:fetch(base+'/install/archive',key)
        assert error.value.code==404


def test_landing_uses_fragment_key_and_no_external_scripts_or_credentials(broker):
    base,key,*_=broker
    with fetch(base+'/install/') as response:
        assert key.encode() not in response.read()
        assert response.headers['Referrer-Policy']=='no-referrer'
        assert "frame-ancestors 'none'" in response.headers['Content-Security-Policy']
    with fetch(base+'/install/download.js') as response:
        script=response.read().decode()
        assert 'location.hash' in script and 'history.replaceState' in script
        assert "Authorization:'Bearer '+key" in script and "credentials:'omit'" in script


def test_download_server_refuses_public_plaintext_listener(tmp_path):
    with pytest.raises(ValueError):downloads.DownloadServer(('0.0.0.0',0),tmp_path)


def test_browser_link_downloads_archive_and_removes_fragment_key(broker,tmp_path):
    playwright=pytest.importorskip('playwright.async_api')
    edge=Path('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe')
    if not edge.exists():pytest.skip('Hidden Edge required')
    base,key,body,*_=broker
    async def check():
        async with playwright.async_playwright() as runtime:
            browser=await runtime.chromium.launch(executable_path=str(edge),headless=True)
            try:
                page=await browser.new_page(accept_downloads=True)
                async with page.expect_download(timeout=10000) as pending:
                    await page.goto(base+'/install/#'+key)
                download=await pending.value
                await download.save_as(tmp_path/'downloaded.zip')
                assert (tmp_path/'downloaded.zip').read_bytes()==body
                assert page.url==base+'/install/'
                assert key not in await page.locator('body').inner_text()
            finally:await browser.close()
    asyncio.run(check())
