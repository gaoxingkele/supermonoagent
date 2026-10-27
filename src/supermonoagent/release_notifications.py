"""Poll the public release manifest and send untrusted update hints to viewers."""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
from urllib.request import ProxyHandler, Request, build_opener


DEFAULT_FEED = 'https://117.50.48.22/updates/stable/manifest.json'
MAX_MANIFEST = 128 * 1024


def release_hint(raw):
    if not isinstance(raw, bytes) or len(raw) > MAX_MANIFEST:
        raise ValueError('Invalid release manifest')
    envelope = json.loads(raw)
    payload = json.loads(base64.b64decode(envelope['payload'], validate=True))
    revision, sequence = payload['revision'], payload['sequence']
    if (payload.get('schema') != 1 or payload.get('channel') != 'stable'
            or not isinstance(revision, str)
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,79}', revision)
            or '..' in revision or type(sequence) is not int
            or not 0 < sequence <= 2**53 - 1):
        raise ValueError('Invalid release identity')
    return json.dumps({'op': 'ui_update', 'revision': revision,
                       'sequence': sequence}, separators=(',', ':'))


def fetch_release(url=DEFAULT_FEED):
    if not url.startswith('https://'):
        raise ValueError('Release notifications require HTTPS')
    request = Request(url, headers={'Cache-Control': 'no-cache',
                                    'User-Agent': 'SuperMonoAgent-Relay-Updates/1'})
    with build_opener(ProxyHandler({})).open(request, timeout=8) as response:
        return release_hint(response.read(MAX_MANIFEST + 1))


class ReleaseNotifier:
    def __init__(self, rooms, *, feed=None, interval=30, fetch=fetch_release):
        self.rooms = rooms
        self.feed = os.environ.get('SUPERMONO_UPDATE_FEED', DEFAULT_FEED) if feed is None else feed
        self.interval = interval
        self.fetch = fetch
        self.current = None
        self.task = None

    def start(self):
        if self.feed and (self.task is None or self.task.done()):
            self.task = asyncio.create_task(self.run())

    async def close(self):
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    async def send_current(self, ws):
        if self.current is not None:
            await ws.send(self.current)

    async def observe(self, raw):
        message = release_hint(raw)
        if message == self.current:
            return False
        self.current = message
        await self.broadcast()
        return True

    async def broadcast(self):
        targets = [phone for room in list(self.rooms().values())
                   if getattr(room, 'service', 'terminal') == 'terminal'
                   for phone in list(room.phones.values())]
        if targets and self.current is not None:
            await asyncio.gather(*(phone.send(self.current) for phone in targets),
                                 return_exceptions=True)

    async def run(self):
        delay = 0
        while True:
            if delay:
                await asyncio.sleep(delay)
            try:
                message = await asyncio.to_thread(self.fetch, self.feed)
                if message != self.current:
                    self.current = message
                    await self.broadcast()
                delay = self.interval
            except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
                delay = max(self.interval, 60)
