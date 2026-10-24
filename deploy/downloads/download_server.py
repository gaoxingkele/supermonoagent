"""Loopback-only private installer downloads behind the existing HTTPS proxy."""
from __future__ import annotations

import argparse
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import threading
import time

PAGE = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>SuperMonoAgent 安装包</title><link rel="stylesheet" href="/install/style.css"><main><h1>SuperMonoAgent</h1><p>完整 Windows 安装包</p><p id="status">正在准备专用下载…</p><a id="save" hidden>保存安装包</a><p>下载并解压后，双击 Install.cmd，即可安装并使用预设配置。已有后台和终端会保留。</p></main><script src="/install/download.js"></script></html>'''.encode('utf-8')
STYLE = b'body{background:#171717;color:#eee;font:16px system-ui;margin:0}main{max-width:600px;margin:12vh auto;padding:28px}a{display:inline-block;background:#eee;color:#111;padding:12px 20px;border-radius:6px;text-decoration:none}p{line-height:1.7;color:#bbb}[hidden]{display:none}'
SCRIPT = r''''use strict';
const key=location.hash.slice(1);history.replaceState(null,'',location.pathname);
const status=document.getElementById('status'),save=document.getElementById('save');
if(!/^[A-Za-z0-9_-]{40,128}$/.test(key)){status.textContent='请使用提供给你的完整专用下载链接。';}
else (async()=>{
 const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),240000);
 try{
  const response=await fetch('/install/archive',{headers:{Authorization:'Bearer '+key},credentials:'omit',cache:'no-store',redirect:'error',signal:controller.signal});
  if(!response.ok)throw new Error('unavailable');
  const length=Number(response.headers.get('Content-Length')),expected=response.headers.get('X-Content-SHA256');
  if(!Number.isSafeInteger(length)||length<1||length>134217728||!/^[0-9a-f]{64}$/.test(expected||''))throw new Error('invalid');
  status.textContent='正在下载并校验完整安装包…';const data=await response.arrayBuffer();
  const digest=[...new Uint8Array(await crypto.subtle.digest('SHA-256',data))].map(value=>value.toString(16).padStart(2,'0')).join('');
  if(data.byteLength!==length||digest!==expected)throw new Error('integrity');
  const filename=response.headers.get('X-Download-Name');
  if(!/^SuperMonoAgent-[0-9]+\.[0-9]+\.[0-9]+-Setup\.zip$/.test(filename||''))throw new Error('filename');
  save.href=URL.createObjectURL(new Blob([data],{type:'application/zip'}));save.download=filename;save.hidden=false;
  status.textContent='校验通过。如果浏览器没有自动保存，请点击下方按钮。';save.click();
 }catch{status.textContent='下载暂不可用、链接已过期或校验失败，请重新获取专用链接。';}
 finally{clearTimeout(timer);}
})();'''.encode('utf-8')


class DownloadServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, root):
        if not ipaddress.ip_address(address[0]).is_loopback:
            raise ValueError('Downloads must listen on loopback behind HTTPS')
        self.root = Path(root).resolve()
        self.slots = threading.BoundedSemaphore(16)
        super().__init__(address, DownloadHandler)


class DownloadHandler(BaseHTTPRequestHandler):
    server_version = 'SuperMonoDownload'

    def log_message(self, *_):
        pass  # Neither bearer keys nor request headers belong in access logs.

    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def _headers(self, status, content_type, length, extra=None):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(length))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'")
        self.send_header('Connection', 'close')
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.close_connection = True

    def _small(self, status, data, content_type='text/plain; charset=utf-8', *, head=False):
        self._headers(status, content_type, len(data))
        if not head:
            self.wfile.write(data)

    def _lookup(self):
        authorization = self.headers.get('Authorization', '')
        if not authorization.startswith('Bearer '):
            return None
        key = authorization[7:]
        if not re.fullmatch(r'[A-Za-z0-9_-]{40,128}', key):
            return None
        digest = hashlib.sha256(key.encode('ascii')).hexdigest()
        try:
            with (self.server.root / 'registry.json').open('rb') as source:
                raw = source.read(65537)
            if len(raw) > 65536:
                return None
            records = json.loads(raw).get('downloads', [])
            if not isinstance(records, list) or len(records) > 64:
                return None
            for record in records:
                if not isinstance(record, dict) or not isinstance(record.get('key_sha256'), str):
                    continue
                if not hmac.compare_digest(record['key_sha256'], digest):
                    continue
                name = record.get('file', '')
                filename = record.get('download_name', '')
                if (type(record.get('expires')) not in (int, float) or not math.isfinite(record['expires']) or record['expires'] <= time.time()
                        or not re.fullmatch(r'[A-Za-z0-9_.-]+\.zip', name)
                        or not re.fullmatch(r'SuperMonoAgent-[0-9]+\.[0-9]+\.[0-9]+-Setup\.zip', filename)
                        or not re.fullmatch(r'[0-9a-f]{64}', record.get('sha256', ''))
                        or type(record.get('size')) is not int or not 0 < record['size'] <= 134217728):
                    return None
                path = self.server.root / name
                if path.is_symlink() or path.resolve().parent != self.server.root:
                    return None
                return path, record
        except (OSError, ValueError, TypeError, AttributeError):
            return None
        return None

    def _serve(self, *, head=False):
        if self.path in {'/install/', '/install/index.html'}:
            return self._small(200, PAGE, 'text/html; charset=utf-8', head=head)
        if self.path == '/install/download.js':
            return self._small(200, SCRIPT, 'text/javascript; charset=utf-8', head=head)
        if self.path == '/install/style.css':
            return self._small(200, STYLE, 'text/css; charset=utf-8', head=head)
        if self.path != '/install/archive':
            return self._small(404, b'Not found', head=head)
        match = self._lookup()
        if match is None:
            return self._small(404, b'Not found', head=head)
        if not self.server.slots.acquire(blocking=False):
            return self._small(503, b'Please retry later', head=head)
        try:
            path, record = match
            try:
                source = path.open('rb')
            except OSError:
                return self._small(404, b'Not found', head=head)
            with source:
                if os.fstat(source.fileno()).st_size != record['size']:
                    return self._small(404, b'Not found', head=head)
                self._headers(200, 'application/zip', record['size'], {
                    'Content-Disposition': 'attachment; filename="'+record['download_name']+'"',
                    'X-Download-Name': record['download_name'], 'X-Content-SHA256': record['sha256'],
                })
                if not head:
                    while chunk := source.read(65536):
                        self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        finally:
            self.server.slots.release()

    def do_GET(self):
        self._serve()

    def do_HEAD(self):
        self._serve(head=True)


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--root', type=Path, required=True)
    cli.add_argument('--port', type=int, default=18818)
    args = cli.parse_args()
    if not 1 <= args.port <= 65535:
        cli.error('Invalid local port')
    with DownloadServer(('127.0.0.1', args.port), args.root) as server:
        server.serve_forever()


if __name__ == '__main__':
    main()
