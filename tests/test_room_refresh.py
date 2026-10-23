"""Room refresh replays only open views; synthetic PTYs never touch a user shell."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch

from supermonoagent.daemon import Daemon
from supermonoagent.protocol import OP_OUTPUT,OP_SNAPSHOT,OP_SUBSCRIBE,unpack
from test_windows import FakePty


class RoomRefreshTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary=tempfile.TemporaryDirectory()
        self.daemons=[]

    async def asyncTearDown(self):
        for daemon in self.daemons:
            for window in list(daemon.hub.windows):
                daemon.hub.close_window(window)
        await asyncio.sleep(.02)
        self.temporary.cleanup()

    def daemon(self):
        root=Path(self.temporary.name)/str(len(self.daemons))
        with patch('supermonoagent.daemon.STATE',root),patch.dict(os.environ,{'SUPERMONO_STATE_DIR':str(root)}):
            daemon=Daemon('owner')
        daemon.hub.persist=False
        daemon.hub.pty_factory=lambda *_:FakePty()
        daemon.hub.attach_loop(asyncio.get_running_loop())
        self.daemons.append(daemon)
        return daemon

    def session(self,daemon,owner='owner',sid=None):
        window=daemon.hub.open_window(owner=owner)
        if sid:
            with patch('supermonoagent.hub.secrets.token_hex',return_value=sid):
                return daemon.hub.open_cli(window.id,'cmd')
        return daemon.hub.open_cli(window.id,'cmd')

    def client(self,daemon,key='refresh-client'):
        client=daemon.hub.attach(asyncio.Queue(64),key=key,token='owner')
        client.multiplex=True
        client.reliable=True
        return client

    @staticmethod
    def drain(client):
        frames=[]
        while not client.queue.empty():
            frames.append(unpack(client.queue.get_nowait()))
        return frames

    async def subscribe(self,daemon,client,sessions,refresh=None):
        body={'sessions':sessions}
        if refresh is not None:
            body['refresh']=refresh
        frames,close=await daemon._op(client,OP_SUBSCRIBE,json.dumps(body).encode())
        self.assertIsNone(close)
        return frames

    async def test_refresh_preserves_subscriptions_lease_and_never_writes_or_resizes(self):
        daemon=self.daemon(); session=self.session(daemon); unopened=self.session(daemon)
        client=self.client(daemon)
        await self.subscribe(daemon,client,[session.id]); self.drain(client)
        daemon.hub.resize_session(client,session.id,100,30)
        self.drain(client)
        before=(set(client.subscriptions),session.resize_owner,session.cols,session.rows)
        session.pty.write=Mock(side_effect=AssertionError('Refresh must not write PTY'))
        session.pty.resize=Mock(side_effect=AssertionError('Refresh must not resize PTY'))
        session.pump.remember(b'visible history')
        unopened.pump.remember(b'directory only')
        await self.subscribe(daemon,client,[session.id],True)
        self.assertEqual(self.drain(client),[(OP_SNAPSHOT,session.id.encode()+b'\0visible history')])
        self.assertEqual((set(client.subscriptions),session.resize_owner,session.cols,session.rows),before)
        self.assertNotIn(unopened.id,client.feed_offsets)
        session.pty.write.assert_not_called();session.pty.resize.assert_not_called()
        await self.subscribe(daemon,client,[session.id])
        self.assertEqual(self.drain(client),[],'Legacy subscription without refresh remains a no-op')

    async def test_new_session_snapshot_once_old_once_and_foreign_owner_never(self):
        daemon=self.daemon(); old=self.session(daemon); new=self.session(daemon); foreign=self.session(daemon,'other')
        client=self.client(daemon)
        await self.subscribe(daemon,client,[old.id]); self.drain(client)
        for session,text in [(old,b'old'),(new,b'new'),(foreign,b'foreign-secret')]:
            session.pump.remember(text)
        await self.subscribe(daemon,client,[old.id,new.id,new.id,foreign.id,'missing'],True)
        frames=self.drain(client)
        self.assertCountEqual(frames,[(OP_SNAPSHOT,old.id.encode()+b'\0old'),(OP_SNAPSHOT,new.id.encode()+b'\0new')])
        self.assertEqual(client.subscriptions,{old.id,new.id})
        self.assertNotIn(b'foreign-secret',b''.join(body for _,body in frames))
        client.authorize=lambda:False
        daemon.hub.refresh_subscriptions(client,[old.id,new.id])
        self.assertEqual(self.drain(client),[])

    async def test_refresh_position_deduplicates_pending_batch_and_preserves_raw_live_bytes(self):
        daemon=self.daemon(); session=self.session(daemon);client=self.client(daemon)
        session.pump.remember(b'initial')
        await self.subscribe(daemon,client,[session.id]); self.drain(client)
        session.pump.remember(b'-pending')
        await self.subscribe(daemon,client,[session.id],True)
        self.assertEqual(self.drain(client),[(OP_SNAPSHOT,session.id.encode()+b'\0initial-pending')])
        session.pump.flushed_position=len(b'initial-pending')
        daemon.hub._flushed(session.id,b'initial-pending',False)
        self.assertEqual(self.drain(client),[])
        raw=b'\x1b[31mraw\xff\x1b[0m\r\n'
        session.pump.remember(raw);session.pump.flushed_position+=len(raw)
        daemon.hub._flushed(session.id,raw,False)
        self.assertEqual(self.drain(client),[(OP_OUTPUT,session.id.encode()+b'\0'+raw)])

    async def test_identical_session_ids_on_two_hosts_refresh_only_the_addressed_host(self):
        first=self.daemon();second=self.daemon()
        one=self.session(first,sid='deadbeef');two=self.session(second,sid='deadbeef')
        a=self.client(first);b=self.client(second)
        await self.subscribe(first,a,[one.id]); await self.subscribe(second,b,[two.id])
        self.drain(a);self.drain(b)
        one.pump.remember(b'host-one');two.pump.remember(b'host-two')
        await self.subscribe(first,a,[one.id],True)
        self.assertEqual(self.drain(a),[(OP_SNAPSHOT,b'deadbeef\0host-one')])
        self.assertEqual(self.drain(b),[])
        await self.subscribe(second,b,[two.id],True)
        self.assertEqual(self.drain(b),[(OP_SNAPSHOT,b'deadbeef\0host-two')])
        self.assertEqual(a.subscriptions,{'deadbeef'});self.assertEqual(b.subscriptions,{'deadbeef'})
