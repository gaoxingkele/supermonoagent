"""Private, read-only recorder for already running legacy local PTYs.

Only HELLO and SUBSCRIBE are sent. Initial rendition is labeled bootstrap,
positioned deltas are exact bytes, and connection/catalog loss never proves EOF.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
from pathlib import Path
import uuid

from websockets.asyncio.client import connect

from .conversation_contract import JournalError, canonical_json, checked_id
from .conversation_sources import private_directory, private_file, _plain_file
from .conversation_store import JournalStore
from .history_context import unpack_history_context
from .notification_bridge import socket_url
from .protocol import (OP_HELLO, OP_HELLO_OK, OP_SESSIONS, OP_HISTORY_CONTEXT,
                       OP_SUBSCRIBE, OP_OUTPUT, OP_SNAPSHOT, pack_json, unpack, unpack_json)


class LegacyJournalCompanion:
    def __init__(self, backend, owner_token_file, store, *, owner_scope, room='local',
                 state_dir=None, trusted_remote=False):
        # A non-loopback endpoint requires a separate trusted installation config;
        # the CLI deliberately does not offer a convenience remote override.
        self.backend = socket_url(backend, local=not trusted_remote)
        self.owner_token_file = Path(owner_token_file)
        self.owner_scope = checked_id(owner_scope)
        self.room = checked_id(room)
        self.store = store
        self.state_dir = private_directory(state_dir or store.root / 'companion')
        self.source = hashlib.sha256(self.backend.encode()).hexdigest()[:32]
        self.state_path = self.state_dir / ('cursors-' + self.source + '.json')
        self.sessions = {}
        self.streams = {}
        self.catalog = {}
        self.connected = False
        self.native_capture = False
        self.error = None
        self._lease = None
        self._started = False
        self._closed = False
        self._task = None
        self._token = None
        for pattern in ('cursor-*.tmp','cursors-*.json','recorder-*.lock'):
            for existing in self.state_dir.glob(pattern):
                private_file(existing)
        if self.state_path.exists():
            private_file(self.state_path)
            try:
                state = json.loads(self.state_path.read_bytes())
                if state.get('owner_scope') != owner_scope: raise JournalError('companion_owner_mismatch')
                self.streams = state['streams']
            except (ValueError, KeyError, TypeError): raise JournalError('invalid_companion_state') from None

    def start(self):
        if self._started: return self
        if self._closed: raise JournalError('companion_closed')
        path = self.state_dir / ('recorder-' + self.source + '.lock')
        if path.exists(): private_file(path)
        lease = path.open('a+b')
        private_file(path)
        try:
            if os.name == 'nt':
                import msvcrt
                lease.seek(0)
                msvcrt.locking(lease.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(lease.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            token = _plain_file(self.owner_token_file).read_text(encoding='utf8').strip()
            if not token or len(token)>8192: raise JournalError('invalid_owner_token_file')
            self._recover_positions()
            self._token = token
            self._lease = lease
            self._started = True
            return self
        except JournalError:
            lease.close()
            raise
        except Exception:
            lease.close()
            raise JournalError('companion_already_running') from None

    def _recover_positions(self):
        """The journal commit wins if a crash happened before cursor-file replace."""
        for saved in self.streams.values():
            manifest=self.store.status(self.owner_scope,saved['journal_id'])
            if (manifest['process_generation']!=saved['stream_id']
                    or manifest['source_epoch']!=self.source):
                raise JournalError('companion_source_mismatch')
            cursor=None
            while True:
                page=self.store.read_page(self.owner_scope,saved['journal_id'],cursor=cursor,limit=100)
                for event in page['events']:
                    metadata=event.get('metadata') or {}
                    end=metadata.get('end_position')
                    if event['kind'] in {'raw_output','bootstrap'} and type(end) is int and end>=0:
                        saved['position']=max(saved.get('position') or 0,end)
                cursor=page.get('next_cursor')
                if cursor is None: break
        self._save()

    def _save(self):
        temporary = self.state_dir / ('cursor-' + uuid.uuid4().hex + '.tmp')
        with temporary.open('xb') as stream:
            private_file(temporary)
            stream.write(canonical_json(dict(owner_scope=self.owner_scope,streams=self.streams)))
            stream.flush()
            os.fsync(stream.fileno())
        if self.state_path.exists(): private_file(self.state_path)
        os.replace(temporary,self.state_path)

    def _journal(self, sid, context):
        stream_id = context['stream_id']
        key = sid + ':' + stream_id
        if key in self.streams:
            saved = self.streams[key]
            # Re-check owner/journal identity after every restart.
            manifest = self.store.status(self.owner_scope,saved['journal_id'])
            if manifest['process_generation'] != stream_id:
                raise JournalError('companion_source_mismatch')
            return saved
        session = self.sessions[sid]
        journal=None
        cursor=None
        while True:
            page=self.store.list_journals(self.owner_scope,cursor=cursor,limit=100)
            journal=next((record for record in page['journals'] if record['source_epoch']==self.source
                and record['session_id']==sid and record['process_generation']==stream_id),None)
            cursor=page.get('next_cursor')
            if journal is not None or cursor is None: break
        if journal is None:
            journal = self.store.create(self.owner_scope,room=self.room,window_id=session['window_id'],
                session_id=sid,source_epoch=self.source,process_generation=stream_id,
                title=session['title'],agent=session.get('agent',''),coverage='partial')
        saved = dict(journal_id=journal['journal_id'],session_id=sid,stream_id=stream_id,
            position=None,epoch=context['epoch'],bootstrap=False,daemon_id=self.catalog.get('daemon_instance_id'))
        self.streams[key] = saved
        self._recover_positions()
        self._save()
        return saved

    def _gap(self, saved, reason, *, expected=None, actual=None):
        metadata = dict(reason=reason)
        if expected is not None: metadata['expected_position']=expected
        if actual is not None: metadata['actual_position']=actual
        self.store.append_event(saved['journal_id'],dict(kind='capture_gap',actor='system',
            origin='legacy_companion',state='incomplete',metadata=metadata))

    def receive(self, frame):
        if not self._started or self._closed: raise JournalError('companion_not_running')
        op,payload = unpack(frame)
        if op in {OP_HELLO_OK,OP_SESSIONS}:
            catalog = unpack_json(payload)
            if not isinstance(catalog,dict): raise JournalError('invalid_source_catalog')
            self.connected = True
            self.catalog = catalog
            capabilities = catalog.get('capabilities',{})
            self.native_capture = capabilities.get('conversation_journal') == 1
            previous = self.sessions
            self.sessions = {str(item['id']):dict(window_id=str(window['id']),
                title=str(window.get('title',''))[:256],agent=str(item.get('agent',''))[:128],
                terminal_state=item.get('terminal_state'))
                for window in catalog.get('windows',[]) for item in window.get('sessions',[])}
            for saved in self.streams.values():
                sid = saved['session_id']
                current = self.sessions.get(sid)
                if (current and current.get('terminal_state')=='ended'
                        and saved.get('daemon_id') and saved['daemon_id']==catalog.get('daemon_instance_id')):
                    self.store.terminal_end(saved['journal_id'])
                    self.store.seal(saved['journal_id'],incomplete=True)
                elif sid in previous and sid not in self.sessions or self.native_capture:
                    self.store.suspend(saved['journal_id'],reason='native_capture_handoff' if self.native_capture else 'source_catalog_missing')
            return True
        if self.native_capture: return False
        if op in {OP_OUTPUT,OP_SNAPSHOT}:
            # Without stable positions/replay identity no exactly-once promise is
            # possible. An explicit capability gap is safer than duplicate bytes.
            self.error = 'history_positions_unavailable'
            return False
        if op != OP_HISTORY_CONTEXT: return False
        sid,context,raw = unpack_history_context(payload)
        if sid not in self.sessions: return False
        if context['v']!=2:
            self.error='history_positions_unavailable'
            return False
        saved = self._journal(sid,context)
        # A stable v2 stream frame proves this backend producer is still alive;
        # merely reconnecting a socket or seeing its catalog does not.
        self.store.resume_capture(saved['journal_id'],process_generation=context['stream_id'],source_epoch=self.source)
        end = context['end_position']
        position = saved['position']
        kind = context['kind']
        if kind == 'snapshot':
            if position is not None and end<position:
                raise JournalError('source_position_regressed')
            if position is not None:
                self._gap(saved,'resume_snapshot',expected=position,actual=end)
            saved['bootstrap'] = True
        elif kind == 'output':
            saved['bootstrap'] = False
        if kind=='snapshot' or saved['bootstrap']:
            if raw:
                source = 'bootstrap:' + saved['stream_id'] + ':' + str(end) + ':' + hashlib.sha256(raw).hexdigest()
                self.store.append_event(saved['journal_id'],dict(kind='bootstrap',actor='system',origin='legacy_companion',
                    state='partial',source_record_id=source,metadata=dict(coverage='partial',end_position=end,epoch=context['epoch'])),raw)
            saved['position'] = end
            if kind=='resume' and not raw: saved['bootstrap']=False
        else:
            start = end-len(raw)
            if start<0: raise JournalError('invalid_source_position')
            if position is None:
                self._gap(saved,'missing_bootstrap',actual=start)
                position=start
            if end<=position:
                # Already durably captured, including a repeated resume frame.
                return False
            if start>position:
                self._gap(saved,'resume_gap',expected=position,actual=start)
            elif start<position:
                raw=raw[position-start:]
                start=position
            if raw:
                self.store.append_raw(saved['journal_id'],raw,
                    source_record_id='legacy:'+saved['stream_id']+':'+str(start)+':'+str(end),
                    metadata=dict(start_position=start,end_position=end))
            saved['position']=end
        saved['epoch']=context['epoch']
        self._save()
        return False

    def subscription(self):
        if self.native_capture: return None
        cursors={}
        for sid in self.sessions:
            matches=[saved for saved in self.streams.values() if saved['session_id']==sid and saved['position'] is not None]
            if matches:
                saved=matches[-1]
                cursors[sid]=dict(position=saved['position'],epoch=saved['epoch'],stream_id=saved['stream_id'])
        return dict(sessions=sorted(self.sessions)[:256],history_after=cursors)

    def status(self):
        return dict(connected=self.connected,native_capture=self.native_capture,
            journals=len(self.streams),error=self.error,recording=self._started and not self._closed and not self.native_capture,
            coverage='partial')

    def _suspend(self,reason):
        for saved in self.streams.values():
            try: self.store.suspend(saved['journal_id'],reason=reason)
            except JournalError: self.error='storage_error'

    async def run(self,stop_event,*,connector=None):
        self.start()
        try:
            while not stop_event.is_set():
                try:
                    async with (connector or connect)(self.backend,max_size=1024*1024,
                            additional_headers={'Authorization':'Bearer '+self._token}) as ws:
                        await ws.send(pack_json(OP_HELLO,dict(token=self._token,multiplex=True,reliable=True,
                            history_context=1,history_resume=1,history_chunks=1,conversation_journal=1)))
                        subscribed=None
                        iterator=ws.__aiter__()
                        while not stop_event.is_set():
                            incoming=asyncio.create_task(iterator.__anext__())
                            stopped=asyncio.create_task(stop_event.wait())
                            done,_=await asyncio.wait([incoming,stopped],return_when=asyncio.FIRST_COMPLETED)
                            if stopped in done:
                                incoming.cancel()
                                with contextlib.suppress(asyncio.CancelledError,StopAsyncIteration): await incoming
                                break
                            stopped.cancel()
                            with contextlib.suppress(asyncio.CancelledError): await stopped
                            try: frame=incoming.result()
                            except StopAsyncIteration: break
                            if not isinstance(frame,bytes): continue
                            changed=await asyncio.to_thread(self.receive,frame)
                            if changed:
                                subscription=self.subscription()
                                wanted=None if subscription is None else subscription['sessions']
                                if subscription is not None and wanted!=subscribed:
                                    await ws.send(pack_json(OP_SUBSCRIBE,subscription))
                                    subscribed=wanted
                except asyncio.CancelledError: raise
                except Exception:
                    self.error='source_disconnected'
                finally:
                    self.connected=False
                    await asyncio.to_thread(self._suspend,'source_disconnected')
                if not stop_event.is_set():
                    try: await asyncio.wait_for(stop_event.wait(),2)
                    except asyncio.TimeoutError: pass
        finally:
            self.close()

    def close(self):
        if self._closed: return
        self._closed=True
        self.connected=False
        self._suspend('recorder_stopped')
        if self._lease:
            if os.name=='nt':
                import msvcrt
                self._lease.seek(0)
                msvcrt.locking(self._lease.fileno(),msvcrt.LK_UNLCK,1)
            self._lease.close()
            self._lease=None
        self._token=None


async def _run(args):
    with JournalStore(args.state_dir/'history') as store:
        companion=LegacyJournalCompanion(args.backend,args.owner_token_file,store,
            owner_scope=args.owner_scope,room=args.room,state_dir=args.state_dir/'companion')
        await companion.run(asyncio.Event())


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend',required=True)
    parser.add_argument('--owner-token-file',type=Path,required=True)
    parser.add_argument('--owner-scope',required=True)
    parser.add_argument('--room',default='local')
    parser.add_argument('--state-dir',type=Path,required=True)
    try: asyncio.run(_run(parser.parse_args(argv)))
    except KeyboardInterrupt: pass


if __name__=='__main__':
    main()
