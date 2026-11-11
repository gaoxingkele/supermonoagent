"""Explicit root bindings and read-only, version-pinned semantic collection.

No discovery rule assigns a thread to a terminal. The trusted local owner must
select an opaque catalog reference; hooks are hints or candidates, never proof
that a Claude Stop or a Codex notify is the final answer.
"""
from __future__ import annotations

import asyncio
from contextlib import closing
import hashlib
import hmac
import json
import os
from pathlib import Path
import queue
import secrets
import shutil
import sqlite3
import stat
import subprocess
import threading
import time
import uuid

from .conversation_contract import JournalError, canonical_json, checked_id
from .file_companion_manager import (_secure as _secure_storage,
    _private as _check_private_storage)

SUPPORTED_CODEX = frozenset({'0.167.0'})
SUPPORTED_CLAUDE = frozenset({'2.1.310', '2.1.246'})
COPY_LIMIT = 10 * 1024 * 1024
RPC_ALLOWLIST = frozenset({'initialize', 'thread/read', 'thread/turns/list', 'thread/items/list'})


async def joined_io(function,*args,**kwargs):
    """Cancellation joins the actual worker before its owner can close storage."""
    task=asyncio.create_task(asyncio.to_thread(function,*args,**kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        owner=getattr(function,'__self__',None)
        cancel=getattr(owner,'cancel',None)
        if callable(cancel): cancel()
        while not task.done():
            try: await asyncio.shield(task)
            except asyncio.CancelledError: continue
            except Exception: break
        # Retrieve a failed worker exception without disclosing its text or
        # replacing the caller's cancellation with a provider/storage failure.
        if task.done() and not task.cancelled(): task.exception()
        raise


async def _join_reconciliation(done):
    """A second caller joins the committed result, never a stale pending roster."""
    try:
        await asyncio.shield(done)
    except asyncio.CancelledError:
        # The owner may still hold a private reader/SQLite lease. As with the
        # owner's worker, cancellation does not imply that work has finished.
        while not done.done():
            try: await asyncio.shield(done)
            except asyncio.CancelledError: continue
        raise


def _project_key(value):
    return str(value).replace('\\','/').removeprefix('//?/').rstrip('/').casefold()


def private_directory(path):
    """Replace permissions on this application-owned directory, never parents."""
    path = Path(path).absolute()
    for parent in (*reversed(path.parents), path):
        if parent.exists() and (parent.is_symlink() or getattr(parent.lstat(), 'st_file_attributes', 0) & 0x400):
            raise JournalError('unsafe_storage')
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    _protect_storage(path,directory=True)
    return path


def _protect_storage(path,*,directory=False):
    try:
        _secure_storage(path,directory)
        _check_private_storage(path,directory)
    except (OSError,ValueError):
        raise JournalError('private_storage_unavailable') from None


def private_file(path):
    """Repair an explicitly application-owned file before reading/writing it."""
    path=_plain_file(path)
    _protect_storage(path)
    return path


def check_private_file(path):
    """Validate a helper's config without changing an arbitrary caller path."""
    path=_plain_file(path)
    try: _check_private_storage(path)
    except (OSError,ValueError): raise JournalError('private_storage_unavailable') from None
    return path


def _private_snapshot_tree(path):
    """Only a selected application-owned snapshot tree; reject links first."""
    private_directory(path)
    for child in path.iterdir():
        info=child.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0)&0x400:
            raise JournalError('unsafe_storage')
        if stat.S_ISDIR(info.st_mode): _private_snapshot_tree(child)
        else: private_file(child)


def _plain_file(path):
    path = Path(path)
    for part in (*reversed(path.parents), path):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise JournalError('unsafe_source')
    if not path.is_file() or path.stat().st_nlink != 1:
        raise JournalError('unsafe_source')
    return path


def _read_db(path):
    db = sqlite3.connect(_plain_file(path).resolve().as_uri() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA query_only=ON')
    db.execute('BEGIN')
    return db


def _root_source(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            pass
    return value in ('cli', 'vscode', 'appServer') if isinstance(value, str) else False


def _rollout_identity(path, home, thread_id):
    path = _plain_file(path)
    if not path.resolve().is_relative_to(Path(home).resolve()):
        raise JournalError('unsafe_source')
    with path.open('rb') as stream:
        line = stream.readline(65537)
    if len(line)>65536 or not line.endswith(b'\n'):
        raise JournalError('unsupported_schema')
    try:
        envelope = json.loads(line)
        metadata = envelope['payload']
    except (ValueError, KeyError, TypeError):
        raise JournalError('unsupported_schema') from None
    if (envelope.get('type')!='session_meta' or not isinstance(metadata,dict)
            or metadata.get('id')!=thread_id or not _root_source(metadata.get('source'))
            or any(metadata.get(key) for key in ('parent_thread_id','parentThreadId','forked_from_id','agent_id'))):
        raise JournalError('source_identity_mismatch')
    return path


def _text_parts(value):
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return None
    # Mixed tool results, attachments and unknown blocks are not user text.
    if not value or any(not isinstance(part, dict) or part.get('type') not in {'text', 'input_text', 'inputText'}
                        or not isinstance(part.get('text'), str) for part in value):
        return None
    return ''.join(part['text'] for part in value)


def _event(binding, turn, item, kind, **extra):
    checked_id(turn,512)
    checked_id(item,512)
    source = f"{binding['provider']}:{turn}:{item}:{kind}"
    # Provider IDs are untrusted and can be long; the identity is not text based.
    identity = hashlib.sha256(source.encode('utf8')).hexdigest()
    return dict(kind=kind, binding_id=binding['binding_id'],
        provider_thread_id=binding['provider_session_id'], provider_turn_id=turn,
        provider_item_id=item, source_record_id=identity, **extra)


def normalize_codex_turn(binding, turn, items):
    """Select exact user/final text, retaining uncertain input only in History."""
    if not isinstance(turn,dict) or not isinstance(items,list): raise JournalError('unsupported_schema')
    tid = turn.get('id', turn.get('turnId'))
    if not isinstance(tid, str) or not tid:
        raise JournalError('unsupported_schema')
    if (turn.get('threadId',binding['provider_session_id'])!=binding['provider_session_id']
            or turn.get('agent_id') or turn.get('parent_thread_id')):
        raise JournalError('source_identity_mismatch')
    rows = []
    for wrapped in items:
        if not isinstance(wrapped,dict): raise JournalError('unsupported_schema')
        item = wrapped.get('item', wrapped)
        if not isinstance(item,dict): raise JournalError('unsupported_schema')
        if (item.get('threadId',binding['provider_session_id'])!=binding['provider_session_id']
                or item.get('turnId',tid)!=tid or item.get('agent_id') or item.get('isSidechain')):
            continue
        iid = item.get('id')
        if not isinstance(iid, str) or not iid:
            continue
        if item.get('type') == 'userMessage':
            text = _text_parts(item.get('content'))
            if text is None:
                continue
            origin = item.get('origin', item.get('provenance'))
            human = isinstance(origin,str) and origin in {'human', 'user', 'native_cli', 'desktop', 'phone'}
            rows.append((_event(binding, tid, iid, 'user_input', actor='human' if human else 'unknown',
                origin=origin if human else 'unknown', state='accepted' if human else 'observed',
                proof='provider_human_input' if human else 'unknown'), text))
        elif (item.get('type') == 'agentMessage' and item.get('phase') == 'final_answer'
                and turn.get('status') == 'completed' and not turn.get('error')
                and isinstance(item.get('text'), str)):
            rows.append((_event(binding, tid, iid, 'assistant_final', actor='assistant', origin='native_cli',
                state='completed', proof='provider_final_phase'), item['text']))
    if turn.get('status') in {'completed', 'failed', 'interrupted'}:
        rows.append((_event(binding, tid, tid, 'turn_outcome', actor='system', origin='native_cli',
            state=turn['status'], proof='provider_terminal_result'), ''))
    return rows


def normalize_claude_record(binding, record):
    if not isinstance(record,dict): raise JournalError('invalid_provider_record')
    if (record.get('sessionId', record.get('session_id')) != binding['provider_session_id']
            or record.get('isSidechain') or record.get('agent_id') or record.get('parent_tool_use_id')):
        return []
    hook = record.get('hook_event_name')
    if hook in {'StopFailure', 'SubagentStop'}:
        return []
    identity = record.get('uuid', record.get('prompt_id'))
    if not isinstance(identity, str) or not identity:
        return []
    turn = record.get('promptId', record.get('prompt_id', identity))
    if hook == 'Stop':
        text = record.get('last_assistant_message')
        if isinstance(text, str):
            # Multiple continuations share a prompt ID. Distinguish candidates
            # without falsely using a text hash to deduplicate user requests.
            iid = record.get('uuid') or identity + ':' + hashlib.sha256(text.encode()).hexdigest()
            return [(_event(binding, turn, iid, 'assistant_candidate', actor='assistant', origin='native_cli',
                state='candidate', proof='provider_stop'), text)]
        return []
    if record.get('type') == 'user' or hook == 'UserPromptSubmit':
        message=record.get('message',{})
        if not isinstance(message,dict): raise JournalError('invalid_provider_record')
        content = record.get('prompt') if hook else message.get('content')
        text = _text_parts(content)
        if text is None:
            return []
        provenance = record.get('promptSource', record.get('origin'))
        human = isinstance(provenance,str) and provenance in {'human', 'user', 'native_cli'} and not record.get('isMeta')
        return [(_event(binding, turn, identity, 'user_input', actor='human' if human else 'unknown',
            origin='native_cli' if human else 'unknown', state='accepted' if human else 'observed',
            proof='provider_human_input' if human else 'unknown'), text)]
    if record.get('type')=='assistant' and not record.get('isApiErrorMessage'):
        message = record.get('message',{})
        if not isinstance(message,dict): raise JournalError('invalid_provider_record')
        if message.get('stop_reason')=='end_turn':
            # A transcript end_turn remains a candidate: Stop continuations may
            # happen afterwards. Ignore thinking/tool blocks entirely.
            parts = message.get('content',[])
            if isinstance(parts,list):
                text = ''.join(part['text'] for part in parts if isinstance(part,dict)
                    and part.get('type')=='text' and isinstance(part.get('text'),str))
                if text:
                    return [(_event(binding,turn,identity,'assistant_candidate',actor='assistant',origin='native_cli',
                        state='candidate',proof='provider_end_turn'),text)]
    return []


def classify_claude_result(record):
    """Pure managed-result guard; does not launch or authorize managed sessions."""
    return bool(isinstance(record,dict) and record.get('type') == 'result' and record.get('subtype') == 'success'
        and record.get('is_error') is False and type(record.get('num_turns')) is int
        and record['num_turns'] > 0 and record.get('terminal_reason') == 'completed'
        and record.get('root') is True and isinstance(record.get('result'), str))


class PassiveRPC:
    """Bounded stdio client: no resume/start/tool/model methods can be sent."""
    def __init__(self, binary, home):
        keep = {'SYSTEMROOT','WINDIR','PATH','PATHEXT','COMSPEC','SYSTEMDRIVE',
                'PROCESSOR_ARCHITECTURE','NUMBER_OF_PROCESSORS'}
        env = {k:v for k,v in os.environ.items() if k.upper() in keep}
        env.update(CODEX_HOME=str(home), USERPROFILE=str(home), HOME=str(home), TEMP=str(home), TMP=str(home),
            RUST_LOG='off', NO_COLOR='1', HTTP_PROXY='http://127.0.0.1:9', HTTPS_PROXY='http://127.0.0.1:9',
            ALL_PROXY='http://127.0.0.1:9', NO_PROXY='')
        command = [str(binary), '-c', 'analytics.enabled=false', '-c', 'check_for_update_on_startup=false',
            '-c', 'cli_auth_credentials_store="file"', '-c', 'notify=[]', '-c', 'mcp_servers={}',
            '-c', 'features.hooks=false', 'app-server', '--listen', 'stdio://']
        self.process = subprocess.Popen(command, cwd=home, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.messages = queue.Queue(64)
        self.stop = threading.Event()
        self.next_id = 0
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        total = 0
        try:
            while not self.stop.is_set():
                line = self.process.stdout.readline(4 * 1024 * 1024 + 1)
                total += len(line)
                if not line or len(line) > 4 * 1024 * 1024 or total > 24 * 1024 * 1024:
                    break
                self.messages.put(json.loads(line), timeout=1)
        except (ValueError, OSError, queue.Full):
            pass
        finally:
            try: self.messages.put_nowait(None)
            except queue.Full: pass

    def rpc(self, method, params):
        if method not in RPC_ALLOWLIST:
            raise JournalError('passive_method_denied')
        self.next_id += 1
        self.process.stdin.write(canonical_json(dict(jsonrpc='2.0', id=self.next_id, method=method, params=params)) + b'\n')
        self.process.stdin.flush()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try: event = self.messages.get(timeout=max(.01, deadline-time.monotonic()))
            except queue.Empty: break
            if event is None: break
            if event.get('id') == self.next_id:
                if 'error' in event: raise JournalError('passive_rpc_error')
                if method=='initialize':
                    self.process.stdin.write(canonical_json(dict(method='initialized',params={})) + b'\n')
                    self.process.stdin.flush()
                return event.get('result')
        raise JournalError('passive_rpc_unavailable')

    def close(self):
        self.stop.set()
        try: self.process.stdin.close()
        except OSError: pass
        try: self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=2)
        self.reader.join(timeout=2)
        self.process.stdout.close()


class CodexPassiveReader:
    def __init__(self, home, state_dir, *, rpc_factory=None, binary_resolver=None, version_probe=None):
        self.home = Path(home).absolute()
        # Construction occurs on the daemon loop; ACL/filesystem work belongs
        # to read(), which the registry runs in its joined worker.
        self.state_dir = Path(state_dir).absolute()
        self.rpc_factory = rpc_factory or PassiveRPC
        self.binary_resolver = binary_resolver or self._binary
        self.version_probe = version_probe or self._version
        self._cancelled=threading.Event()

    def cancel(self):
        self._cancelled.set()

    def _binary(self, version):
        candidate = self.home / 'packages/app-server-daemon/releases' / (version + '-x86_64-pc-windows-msvc') / 'bin/codex.exe'
        if not candidate.is_file(): raise JournalError('matching_runtime_unavailable')
        return candidate

    @staticmethod
    def _version(binary):
        return subprocess.check_output([str(binary), '--version'], text=True, timeout=10,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)).strip().removeprefix('codex-cli ')

    @staticmethod
    def _copy_schema(source, destination):
        for kind in ('table', 'index', 'trigger'):
            for row in source.execute('SELECT name,sql FROM sqlite_master WHERE type=? AND sql IS NOT NULL ORDER BY name', (kind,)):
                if not row['name'].startswith('sqlite_'):
                    destination.execute(row['sql'])

    @staticmethod
    def _copy_rows(source, destination, table, thread=None, rewrite=None):
        exists = source.execute('SELECT 1 FROM sqlite_master WHERE type="table" AND name=?', (table,)).fetchone()
        if not exists:
            if table == '_sqlx_migrations': return
            raise JournalError('unsupported_schema')
        where = ' WHERE ' + ('id' if table == 'threads' else 'thread_id') + '=?' if thread else ''
        copied_bytes = 0
        for row in source.execute('SELECT * FROM "' + table + '"' + where, (thread,) if thread else ()):
            record = dict(row)
            copied_bytes += sum(len(value) if isinstance(value,(bytes,str)) else 8 for value in record.values())
            if copied_bytes>COPY_LIMIT:
                raise JournalError('passive_read_limit')
            if rewrite: record.update(rewrite)
            destination.execute('INSERT INTO "' + table + '" VALUES (' + ','.join('?' for _ in record) + ')', tuple(record.values()))

    def _pages(self, rpc, method, params):
        cursor, seen = None, set()
        for _ in range(128):
            if self._cancelled.is_set(): raise JournalError('passive_read_cancelled')
            result = rpc.rpc(method, {**params, **({'cursor':cursor} if cursor else {})})
            if not isinstance(result, dict) or not isinstance(result.get('data'), list):
                raise JournalError('unsupported_schema')
            if any(not isinstance(item,dict) for item in result['data']):
                raise JournalError('unsupported_schema')
            yield from result['data']
            cursor = result.get('nextCursor')
            if cursor is None: return
            if not isinstance(cursor, str) or cursor in seen:
                raise JournalError('invalid_provider_cursor')
            seen.add(cursor)
        raise JournalError('passive_read_limit')

    def read(self, thread_id, *, expected_version):
        checked_id(thread_id)
        if expected_version not in SUPPORTED_CODEX:
            raise JournalError('unsupported_provider_version')
        private_directory(self.state_dir)
        for old in self.state_dir.glob('passive-read-private-*'):
            _private_snapshot_tree(old)
        binary = self.binary_resolver(expected_version)
        if self.version_probe(binary) != expected_version:
            raise JournalError('provider_version_mismatch')
        private = self.state_dir / ('passive-read-private-' + uuid.uuid4().hex)
        rpc = None
        try:
            private_directory(private)
            with closing(_read_db(self.home / 'state_5.sqlite')) as state, closing(_read_db(self.home / 'thread_history_1.sqlite')) as history:
                row = state.execute('SELECT * FROM threads WHERE id=?', (thread_id,)).fetchone()
                if row is None or row['cli_version'] != expected_version or not _root_source(row['source']):
                    raise JournalError('source_identity_mismatch')
                source = _rollout_identity(Path(row['rollout_path']),self.home,thread_id)
                # Never let provider metadata cause arbitrary file import.
                if not source.resolve().is_relative_to(self.home.resolve()) or source.stat().st_size > COPY_LIMIT:
                    raise JournalError('unsafe_source')
                payload = source.read_bytes()
                before = hashlib.sha256(payload).digest()
                copy = private / 'sessions' / source.name
                private_directory(copy.parent)
                with copy.open('xb') as stream:
                    private_file(copy)
                    stream.write(payload)
                for original, filename, tables in [(state,'state_5.sqlite', ['_sqlx_migrations','threads']),
                        (history,'thread_history_1.sqlite', ['_sqlx_migrations','thread_turns','thread_items','thread_history_projection_state','thread_realtime_items'])]:
                    with closing(sqlite3.connect(private / filename)) as destination:
                        private_file(private/filename)
                        self._copy_schema(original, destination)
                        for table in tables:
                            self._copy_rows(original, destination, table, None if table == '_sqlx_migrations' else thread_id,
                                {'rollout_path':str(copy)} if table == 'threads' else None)
                            if sum(p.stat().st_size for p in private.rglob('*') if p.is_file()) > COPY_LIMIT:
                                raise JournalError('passive_read_limit')
                        destination.commit()
            if self._cancelled.is_set(): raise JournalError('passive_read_cancelled')
            rpc = self.rpc_factory(binary, private)
            rpc.rpc('initialize', dict(clientInfo=dict(name='supermono-passive-journal',version='1'), capabilities=dict(experimentalApi=True)))
            result = rpc.rpc('thread/read', dict(threadId=thread_id, includeTurns=False))
            if not isinstance(result, dict) or result.get('thread', {}).get('id') != thread_id:
                raise JournalError('source_identity_mismatch')
            rows = []
            for turn in self._pages(rpc, 'thread/turns/list', dict(threadId=thread_id, limit=100, sortDirection='asc', itemsView='notLoaded')):
                tid = turn.get('id', turn.get('turnId'))
                checked_id(tid)
                items = list(self._pages(rpc, 'thread/items/list', dict(threadId=thread_id, turnId=tid, limit=100, sortDirection='asc')))
                rows.append((turn, [item.get('item', item) for item in items]))
            # Growth from a live writer is normal; the snapshot is immutable.
            # A same-size rewrite is a coverage error, never silently trusted.
            if source.stat().st_size == len(payload) and hashlib.sha256(source.read_bytes()).digest() != before:
                raise JournalError('source_changed')
            return rows
        except JournalError:
            raise
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, AttributeError, subprocess.SubprocessError):
            raise JournalError('passive_read_unavailable') from None
        finally:
            try:
                if rpc: rpc.close()
            finally:
                resolved = private.resolve()
                if (resolved.is_relative_to(self.state_dir.resolve()) and resolved.parent == self.state_dir.resolve()
                        and resolved.name.startswith('passive-read-private-') and resolved.exists()):
                    shutil.rmtree(resolved)


class SourceRegistry:
    def __init__(self, store, state_dir, *, codex_home=None, claude_home=None, clock=None):
        self.store = store
        self.state_dir = private_directory(state_dir)
        self.codex_home = Path(codex_home or Path.home() / '.codex').absolute()
        self.claude_home = Path(claude_home or Path.home() / '.claude').absolute()
        self.clock = clock or time.time
        self._lock = threading.RLock()
        self._catalog = {}
        self._bindings = {}
        self._inflight = set()
        self._inflight_done = {}
        self._target_offset = 0
        self._closed = False
        # A root DACL does not revoke explicit grants on existing child files
        # on Windows. Repair only our named state/config/snapshot artifacts.
        for existing in self.state_dir.glob('bindings-*.tmp'):
            private_file(existing)
        hooks=self.state_dir/'hooks'
        if hooks.exists():
            private_directory(hooks)
            for existing in hooks.glob('hook-*.json'): private_file(existing)
        snapshots=self.state_dir/'snapshots'
        if snapshots.exists():
            private_directory(snapshots)
            for existing in snapshots.glob('passive-read-private-*'): _private_snapshot_tree(existing)
        path = self.state_dir / 'bindings.json'
        if path.exists():
            private_file(path)
            try:
                self._bindings = json.loads(path.read_text(encoding='utf8'))
                if not isinstance(self._bindings,dict) or any(not isinstance(value,dict) for value in self._bindings.values()):
                    raise JournalError('invalid_binding_state')
                # Restart requires explicit re-binding to the current daemon.
                for binding in self._bindings.values(): binding['active'] = False
            except (ValueError, TypeError): raise JournalError('invalid_binding_state') from None

    def _save(self):
        path = self.state_dir / 'bindings.json'
        if path.exists(): private_file(path)
        temporary = self.state_dir / ('bindings-' + uuid.uuid4().hex + '.tmp')
        with temporary.open('xb') as stream:
            private_file(temporary)
            stream.write(canonical_json(self._bindings))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)

    @staticmethod
    def _public(binding):
        return {key:binding[key] for key in ('binding_id','journal_id','provider','provider_version','provider_session_id','active')}

    def list_sources(self, provider, *, project_root=None):
        """Trusted-local metadata catalog. Never returns paths or message bodies."""
        if self._closed: raise JournalError('registry_closed')
        catalog = []
        try:
            if provider == 'codex':
                with closing(_read_db(self.codex_home / 'state_5.sqlite')) as db:
                    db.create_function('journal_project',1,_project_key)
                    query = 'SELECT * FROM threads' + (' WHERE journal_project(cwd)=?' if project_root is not None else '') + ' LIMIT 257'
                    rows = db.execute(query, (_project_key(project_root),) if project_root is not None else ()).fetchall()
                    for row in rows[:256]:
                        source = dict(row)
                        if not _root_source(source.get('source')): continue
                        try: _rollout_identity(source['rollout_path'],self.codex_home,source['id'])
                        except JournalError: continue
                        source.update(provider=provider, provider_session_id=source['id'], provider_version=source.get('cli_version'), path=source.get('rollout_path'))
                        catalog.append(source)
            elif provider == 'claude-code':
                count = 0
                for path in (self.claude_home / 'projects').glob('*/*.jsonl'):
                    if count >= 256: break
                    _plain_file(path)
                    with path.open('rb') as stream:
                        for _ in range(32):
                            line = stream.readline(1024 * 1024 + 1)
                            if not line or len(line) > 1024 * 1024: break
                            record = json.loads(line)
                            if not isinstance(record,dict): raise JournalError('invalid_provider_record')
                            if (record.get('sessionId') != path.stem or record.get('isSidechain')
                                    or record.get('agentId') or not record.get('cwd')): continue
                            if project_root is not None and _project_key(record['cwd']) != _project_key(project_root): continue
                            catalog.append(dict(provider=provider,provider_session_id=record['sessionId'],
                                provider_version=record.get('version'), path=str(path), cwd=record['cwd']))
                            count += 1
                            break
            else: raise JournalError('unsupported_provider')
        except JournalError: raise
        except (OSError, sqlite3.Error, ValueError, KeyError):
            raise JournalError('source_catalog_unavailable') from None
        public = []
        for source in catalog:
            ref = uuid.uuid4().hex
            self._catalog[ref] = {**source, 'expires':self.clock()+300}
            public.append(dict(source_ref=ref, provider=provider, provider_session_id=source['provider_session_id'],
                provider_version=source.get('provider_version'), supported=source.get('provider_version') in
                (SUPPORTED_CODEX if provider == 'codex' else SUPPORTED_CLAUDE)))
        self._catalog = {ref:value for ref,value in self._catalog.items() if value['expires']>=self.clock()}
        return public

    def bind(self, owner_scope, journal_id, source_ref, *, terminal_generation, is_current=None):
        with self._lock:
            if self._closed: raise JournalError('registry_closed')
            if is_current is not None and not is_current(): raise JournalError('terminal_generation_mismatch')
            manifest = self.store.status(owner_scope, journal_id)
            if manifest['process_generation'] != terminal_generation or manifest['terminal_state'] != 'running':
                raise JournalError('terminal_generation_mismatch')
            source = self._catalog.get(source_ref)
            if source is None or source['expires'] < self.clock(): raise JournalError('source_selection_expired')
            # Revalidate metadata after selection, before storing the binding.
            fresh = self.list_sources(source['provider'], project_root=source.get('cwd'))
            if not any(row['provider_session_id'] == source['provider_session_id'] and row['provider_version'] == source['provider_version'] for row in fresh):
                raise JournalError('source_identity_mismatch')
            if is_current is not None and not is_current(): raise JournalError('terminal_generation_mismatch')
            for old in self._bindings.values():
                if old['active'] and old['provider'] == source['provider'] and old['provider_session_id'] == source['provider_session_id']:
                    if old['journal_id'] == journal_id and old['terminal_generation'] == terminal_generation:
                        return self._public(old)
                    raise JournalError('source_already_bound')
                if old['active'] and old['journal_id'] == journal_id: raise JournalError('journal_already_bound')
            if source['provider_version'] not in (SUPPORTED_CODEX if source['provider']=='codex' else SUPPORTED_CLAUDE):
                raise JournalError('unsupported_provider_version')
            identity = uuid.uuid4().hex
            binding = {key:source[key] for key in ('provider','provider_version','provider_session_id','path')}
            binding.update(binding_id=identity, journal_id=journal_id, owner_scope=owner_scope,
                terminal_generation=terminal_generation, source_epoch=manifest['source_epoch'], active=True,
                capability=secrets.token_urlsafe(32), created_at=self.clock(), pending={}, seen=[], reason=None)
            # Explicit binding begins future capture. Historical import is not
            # implied by selecting a source. Existing in-flight turns remain
            # eligible for their later completion.
            binding['skip_turns'] = []
            binding['claude_offset'] = 0
            if source['provider']=='codex':
                try:
                    with closing(_read_db(self.codex_home/'thread_history_1.sqlite')) as history:
                        columns={row[1] for row in history.execute('PRAGMA table_info(thread_turns)')}
                        if {'turn_id','status','thread_id'}<=columns:
                            binding['skip_turns']=[row[0] for row in history.execute(
                                "SELECT turn_id FROM thread_turns WHERE thread_id=? AND status IN ('completed','failed','interrupted')",(source['provider_session_id'],))]
                except (OSError,sqlite3.Error): raise JournalError('unsupported_schema') from None
            else:
                transcript=_plain_file(source['path'])
                size=transcript.stat().st_size
                with transcript.open('rb') as stream:
                    start=max(0,size-1024*1024)
                    stream.seek(start)
                    tail=stream.read()
                newline=tail.rfind(b'\n')
                if newline<0 and start: raise JournalError('passive_read_limit')
                binding['claude_offset']=start+newline+1 if newline>=0 else 0
            if is_current is not None and not is_current(): raise JournalError('terminal_generation_mismatch')
            self.store.append_event(journal_id, dict(kind='binding', actor='system', origin='recorder', state='bound',
                binding_id=identity, provider_thread_id=source['provider_session_id'], metadata=dict(provider=source['provider'],provider_version=source['provider_version'])))
            self._bindings[identity] = binding
            try: self._save()
            except Exception:
                self._bindings.pop(identity,None)
                raise
            return self._public(binding)

    def status(self, journal_id):
        bindings = [b for b in self._bindings.values() if b['journal_id'] == journal_id]
        if not bindings: return dict(bound=False, supported=False, reason='unbound')
        binding = bindings[-1]
        expired=bool(not binding['active'] and binding['pending'] and binding.get('repair_until')
            and self.clock()>binding['repair_until'])
        return dict(bound=binding['active'] and not self._closed, provider=binding['provider'], provider_version=binding['provider_version'],
            binding_id=binding['binding_id'], supported=binding['provider']=='codex',
            reason='recorder_stopped' if self._closed else 'repair_expired' if expired else binding.get('reason') or ('claude_final_unproven' if binding['provider']=='claude-code' else None),
            healthy=not self._closed and binding['active'] and not binding.get('reason') and binding['provider']=='codex',
            input_provenance='explicit_only',pending_turns=len(binding['pending']),
            repair_until=binding.get('repair_until'))

    def reconciliation_targets(self):
        """Bounded trusted polling roster, including frozen repairs on restart.

        Old active bindings are suspended on restart and cannot silently adopt
        a new daemon. Only their already frozen, unexpired repair roster remains
        eligible. Rotation prevents large catalogs from starving later entries.
        """
        with self._lock:
            if self._closed: return []
            now=self.clock()
            eligible=[binding for binding in self._bindings.values() if binding['active']
                or binding['pending'] and isinstance(binding.get('repair_until'),(int,float))
                and binding['repair_until']>=now]
            if not eligible: return []
            start=self._target_offset%len(eligible)
            selected=(eligible[start:]+eligible[:start])[:256]
            self._target_offset=(start+len(selected))%len(eligible)
            return [dict(binding_id=binding['binding_id'],journal_id=binding['journal_id'],
                active=binding['active'],pending_turns=len(binding['pending']),
                repair_until=binding.get('repair_until')) for binding in selected]

    def _append(self, binding, rows):
        count = 0
        for event, text in rows:
            source = event['source_record_id']
            if source in binding['seen']: continue
            if binding['active']:
                self.store.append_event(binding['journal_id'], event, text)
            elif event['kind'] in {'assistant_final','turn_outcome'}:
                self.store.reconcile_event(binding['journal_id'], event, text)
            else: continue
            binding['seen'].append(source)
            count += 1
        return count

    async def reconcile(self, binding_id):
        existing=None
        with self._lock:
            if self._closed: raise JournalError('registry_closed')
            binding = self._bindings.get(binding_id)
            if binding is None: raise JournalError('binding_not_found')
            if not binding['active'] and not binding['pending']:
                return dict(recorded=0,**self.status(binding['journal_id']))
            if not binding['active'] and binding.get('repair_until') and self.clock()>binding['repair_until']:
                return dict(recorded=0,**self.status(binding['journal_id']))
            existing=self._inflight_done.get(binding_id)
            if existing is None:
                self._inflight.add(binding_id)
                self._inflight_done[binding_id]=asyncio.get_running_loop().create_future()
        if existing is not None:
            await _join_reconciliation(existing)
            return dict(recorded=0,**self.status(binding['journal_id']))
        pending_updates={}
        try:
            if binding['provider'] == 'codex':
                reader = CodexPassiveReader(self.codex_home, self.state_dir / 'snapshots')
                turns = await joined_io(reader.read, binding['provider_session_id'], expected_version=binding['provider_version'])
                rows = []
                for turn, items in turns:
                    tid = turn.get('id', turn.get('turnId'))
                    if tid in binding.get('skip_turns',[]): continue
                    if not binding['active'] and tid not in binding['pending']: continue
                    if turn.get('status') not in {'completed','failed','interrupted'}:
                        pending_updates[tid] = [_event(binding,tid,item['id'],'assistant_final')['source_record_id']
                            for item in items if item.get('type')=='agentMessage' and item.get('id')]
                    rows.extend(normalize_codex_turn(binding, turn, items))
            else:
                rows,claude_offset = await joined_io(self._claude_rows, binding)
            with self._lock:
                if self._closed: raise JournalError('registry_closed')
                if binding['active']:
                    binding['pending'].update(pending_updates)
                else:
                    rows=[(event,text) for event,text in rows if event.get('provider_turn_id') in binding['pending']]
                count = self._append(binding, rows)
                if binding['provider']=='claude-code': binding['claude_offset']=claude_offset
                # Only a successfully persisted, provider-terminal outcome
                # clears the roster, including a permitted post-EOF repair.
                # _append must finish first: a new/unfrozen final item raises
                # before this point and cannot discharge pending work.
                completed={event['provider_turn_id'] for event,_ in rows if event['kind']=='turn_outcome'}
                for tid in completed: binding['pending'].pop(tid,None)
                binding['reason'] = None
                self._save()
            return dict(recorded=count, **self.status(binding['journal_id']))
        except JournalError as error:
            binding['reason'] = error.code
            self._save()
            return dict(recorded=0, **self.status(binding['journal_id']))
        except asyncio.CancelledError:
            binding['reason']='reconcile_cancelled'
            self._save()
            raise
        except Exception:
            # A joining EOF caller must also see an unexpected source/storage
            # failure; completion of the Future never means capture succeeded.
            binding['reason']='reconcile_failed'
            try: self._save()
            except Exception: pass
            return dict(recorded=0,**self.status(binding['journal_id']))
        finally:
            with self._lock:
                self._inflight.discard(binding_id)
                done=self._inflight_done.pop(binding_id,None)
                if done is not None and not done.done(): done.set_result(None)

    def _claude_rows(self, binding):
        path = _plain_file(binding['path'])
        if not path.resolve().is_relative_to(self.claude_home.resolve()) or path.stat().st_size > COPY_LIMIT:
            raise JournalError('passive_read_limit')
        rows = []
        offset=binding.get('claude_offset',0)
        with path.open('rb') as stream:
            if path.stat().st_size<offset: raise JournalError('source_truncated')
            stream.seek(offset)
            while True:
                line = stream.readline(1024 * 1024 + 1)
                if not line: break
                if len(line)>1024*1024: raise JournalError('passive_read_limit')
                if not line.endswith(b'\n'): break  # Wait for the complete UTF-8/JSONL write.
                try: record = json.loads(line)
                except ValueError: raise JournalError('invalid_provider_record') from None
                if not isinstance(record,dict): raise JournalError('invalid_provider_record')
                if record.get('version', binding['provider_version']) != binding['provider_version']:
                    raise JournalError('provider_version_mismatch')
                rows.extend(normalize_claude_record(binding, record))
                offset=stream.tell()
        return rows,offset

    def prepare_hook_config(self, binding_id, endpoint, *, original_notifier=None):
        """Trusted host-only recipe; never part of the frontend binding result."""
        from .conversation_hooks import write_hook_config
        binding=self._bindings.get(binding_id)
        if self._closed or not binding or not binding['active']:
            raise JournalError('binding_not_found')
        return write_hook_config(self.state_dir/'hooks',binding_id=binding_id,
            capability=binding['capability'],terminal_generation=binding['terminal_generation'],
            endpoint=endpoint,original_notifier=original_notifier)

    def receive_hook(self, binding_id, capability, payload):
        with self._lock:
            binding = self._bindings.get(binding_id)
            if (binding is None or not isinstance(capability,str) or not hmac.compare_digest(capability,binding['capability'])
                    or not isinstance(payload,dict) or self._closed):
                raise JournalError('hook_denied')
            if len(canonical_json(payload)) > 1024*1024: raise JournalError('hook_too_large')
            root = payload.get('thread-id', payload.get('session_id'))
            if (root != binding['provider_session_id'] or payload.get('_terminal_generation') != binding['terminal_generation']
                    or payload.get('agent_id') or payload.get('isSidechain')):
                raise JournalError('hook_identity_mismatch')
            manifest = self.store.status(binding['owner_scope'], binding['journal_id'])
            if manifest['process_generation'] != binding['terminal_generation']: raise JournalError('hook_identity_mismatch')
            if binding['provider'] == 'codex':
                # No input-array replay and no notify-to-final promotion.
                if payload.get('type') != 'agent-turn-complete': raise JournalError('unsupported_hook')
                return dict(accepted=True, needs_reconcile=True, recorded=0)
            if not binding['active']: raise JournalError('hook_denied')
            count = self._append(binding, normalize_claude_record(binding,payload))
            self._save()
            return dict(accepted=True, recorded=count, final=False)

    def terminal_end(self, journal_id):
        with self._lock:
            # This is a trusted EOF barrier, not a way to invalidate a reader's
            # pending result. The manager must join reconciliation first and
            # treat any bounded-wait failure as incomplete capture.
            if any(binding['journal_id']==journal_id and binding['active']
                    and binding['binding_id'] in self._inflight for binding in self._bindings.values()):
                raise JournalError('reconcile_in_progress')
            for binding in self._bindings.values():
                if binding['journal_id'] != journal_id or not binding['active']: continue
                if binding['pending']:
                    ids = [item for values in binding['pending'].values() for item in values]
                    ids.extend(_event(binding,tid,tid,'turn_outcome')['source_record_id'] for tid in binding['pending'])
                    self.store.authorize_pending_repair(journal_id, binding_id=binding['binding_id'],
                        provider_thread_id=binding['provider_session_id'], turn_ids=list(binding['pending']),
                        deadline=self.clock()+86400, source_record_ids=ids)
                    binding['repair_until']=self.clock()+86400
                binding['active'] = False
            self._save()

    def close(self):
        # Stop observing only. EOF/terminal_end belongs to the trusted backend.
        self._closed = True
        for binding in self._bindings.values():
            if binding['active']: self.store.suspend(binding['journal_id'],reason='semantic_recorder_stopped')
        self._save()
