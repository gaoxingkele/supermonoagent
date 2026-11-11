"""Real sockets: a local rendezvous plus two agents with separate state directories."""
import asyncio
import base64
import hashlib
import json
import os
import zlib
from pathlib import Path

import pytest

from supermonoagent.p2p import rendezvous, secure
from supermonoagent.p2p.agent import Agent

CHUNK = 64 * 1024


def tree(root: Path) -> dict:
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*') if p.is_file()}


def make_source(base: Path) -> Path:
    src = base / '源目录'
    (src / 'docs' / 'empty').mkdir(parents=True)
    (src / 'big.bin').write_bytes(os.urandom(3 * 1024 * 1024 + 123))
    (src / 'docs' / '说明.md').write_text('你好\n' * 1000, 'utf-8')
    (src / 'zero.txt').write_bytes(b'')
    for i in range(60):
        (src / 'docs' / f'f{i:03}.txt').write_bytes(os.urandom(i * 37))
    return src


async def pair(tmp_path, **options):
    server, listener = await rendezvous.serve('127.0.0.1', 0)
    address = f"127.0.0.1:{listener.sockets[0].getsockname()[1]}"
    a = Agent(tmp_path / 'state-a', rendezvous=address, peer_port=0, name='电脑A', chunk=CHUNK, **options)
    b = Agent(tmp_path / 'state-b', rendezvous=address, peer_port=0, name='电脑B', chunk=CHUNK, **options)
    b.join(secure.join_code(a.key))
    a.roots = b.roots = [str(tmp_path)]
    await a.start()
    await b.start()
    await until(lambda: b.peer_id in a.peers and a.peer_id in b.peers)
    return server, listener, a, b


async def close(listener, *agents):
    for agent in agents:
        await agent.stop()
    listener.close()


async def until(check, timeout=30):
    deadline = asyncio.get_running_loop().time() + timeout
    while not check():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError('timed out')
        await asyncio.sleep(0.02)


async def wait_done(out, timeout=60):
    await until(lambda: out.status in ('done', 'failed'), timeout)
    assert out.status == 'done', out.error


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 120))


def test_direct_transfer_and_resend_skips(tmp_path):
    async def body():
        server, listener, a, b = await pair(tmp_path)
        try:
            src = make_source(tmp_path)
            dest = tmp_path / '收件'
            dest.mkdir()
            listing = await (await a.control_for(b.peer_id)).call('list', path=str(tmp_path))
            assert {'收件', '源目录'} <= {e['name'] for e in listing['entries']}
            out = a.send(str(src), b.peer_id, str(dest))
            await wait_done(out)
            assert out.route in ('lan', 'ipv6', 'public', 'punch')
            assert tree(dest / '源目录') == tree(src)
            assert (dest / '源目录' / 'docs' / 'empty').is_dir()
            assert not (dest / '源目录' / '.sma-p2p').exists()
            # Same source to same place: nothing to send, every file skipped.
            again = a.send(str(src), b.peer_id, str(dest))
            await wait_done(again)
            assert again.skipped_files == len(out.manifest['files'])
        finally:
            await close(listener, a, b)
    run(body())


def test_relay_fallback_is_end_to_end(tmp_path):
    async def body():
        server, listener, a, b = await pair(tmp_path, force_relay=True)
        try:
            src = make_source(tmp_path)
            dest = tmp_path / 'dest'
            dest.mkdir()
            out = a.send(str(src), b.peer_id, str(dest))
            await wait_done(out)
            assert out.route == 'relay'
            assert tree(dest / '源目录') == tree(src)
            assert server.stats['pipes_total'] >= 2
            # The relay saw ciphertext only: the bytes it spliced exceed the payload (TLS framing).
            assert server.stats['pipe_bytes'] > out.bytes_total
        finally:
            await close(listener, a, b)
    run(body())


def test_interrupted_transfer_resumes_without_resending(tmp_path):
    async def body():
        server, listener, a, b = await pair(tmp_path, workers=2)
        try:
            src = tmp_path / 'one'
            src.mkdir()
            (src / 'large.bin').write_bytes(os.urandom(12 * 1024 * 1024))
            dest = tmp_path / 'dest'
            dest.mkdir()
            out = a.send(str(src), b.peer_id, str(dest))
            await until(lambda: out.bytes_done >= 3 * 1024 * 1024 and out.status == 'sending')
            a.pause(out.tid)
            await until(lambda: out.task is None)
            done_before = out.bytes_done
            assert 0 < done_before < out.bytes_total
            assert list((dest / 'one').glob('*.sma-part'))
            first = b.incoming[out.tid]['transfer']
            a.resume(out.tid)
            await until(lambda: b.incoming[out.tid]['transfer'] is not first and out.status in ('sending', 'finishing', 'done'))
            # The receiver's journal reported what is already on disk; only the rest is planned.
            resumed_from = out.bytes_done
            assert resumed_from >= done_before - 2 * 4 * CHUNK, (resumed_from, done_before)
            await wait_done(out)
            assert tree(dest / 'one') == tree(src)
        finally:
            await close(listener, a, b)
    run(body())


def test_restart_resumes_from_saved_records(tmp_path):
    async def body():
        server, listener, a, b = await pair(tmp_path, workers=1)
        src = tmp_path / 'r'
        src.mkdir()
        (src / 'data.bin').write_bytes(os.urandom(8 * 1024 * 1024))
        dest = tmp_path / 'dest'
        dest.mkdir()
        out = a.send(str(src), b.peer_id, str(dest))
        await until(lambda: out.bytes_done >= 1024 * 1024)
        await a.stop()   # sender process goes away mid-transfer
        address = a.config['rendezvous']
        a2 = Agent(tmp_path / 'state-a', rendezvous=address, peer_port=0, chunk=CHUNK)
        a2.roots = [str(tmp_path)]
        assert a2.outgoing[out.tid].status == 'waiting'
        await a2.start()
        try:
            out2 = a2.outgoing[out.tid]
            await until(lambda: out2.status == 'done', 60)
            assert tree(dest / 'r') == tree(src)
        finally:
            await close(listener, a2, b)
    run(body())


def test_peer_cannot_escape_roots_or_destination(tmp_path):
    async def body():
        server, listener, a, b = await pair(tmp_path)
        try:
            control = await a.control_for(b.peer_id)
            with pytest.raises(RuntimeError, match='根目录'):
                await control.call('list', path=str(tmp_path.parent))
            with pytest.raises(RuntimeError, match='根目录'):
                await control.call('mkdir', path=str(tmp_path.parent), name='x')
            manifest = base64.b64encode(zlib.compress(json.dumps(
                {'files': [['../../evil.txt', 3, 0]], 'dirs': []}).encode())).decode()
            dest = tmp_path / 'dest'
            dest.mkdir()
            with pytest.raises(RuntimeError):
                await control.call('offer', tid='1' * 24, dest=str(dest), name='x', single=False,
                                   chunk=CHUNK, manifest=manifest)
            with pytest.raises(RuntimeError, match='根目录'):
                await control.call('offer', tid='1' * 24, dest=str(tmp_path.parent), name='x', single=False,
                                   chunk=CHUNK, manifest=manifest)
            assert not (tmp_path.parent / 'evil.txt').exists()
        finally:
            await close(listener, a, b)
    run(body())


def test_wrong_network_key_cannot_connect(tmp_path):
    async def body():
        server, listener, a, b = await pair(tmp_path)
        try:
            # A third agent on the same rendezvous but another network sees nobody.
            c = Agent(tmp_path / 'state-c', rendezvous=a.config['rendezvous'], peer_port=0, name='外人')
            await c.start()
            await until(lambda: c.rv_state == 'online')
            await asyncio.sleep(0.2)
            assert c.peers == {}
            # Dialing a's port directly with the wrong PSK fails the TLS handshake.
            with pytest.raises(Exception):
                await asyncio.wait_for(asyncio.open_connection('127.0.0.1', a.listen_port, ssl=c.client_ctx,
                                                               server_hostname=''), 5)
            await c.stop()
        finally:
            await close(listener, a, b)
    run(body())
