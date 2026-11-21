"""Validate a source-owned, revocable assignment before handling cockpit OP24."""
import asyncio
import hashlib
import json
import os
import stat
import time
from urllib.parse import urlsplit

from websockets.asyncio.client import connect

from .conversation_contract import JournalError


TARGET = ('window_id', 'session_id', 'process_generation', 'journal_id')
BINDING = ('project', 'task_id', 'attempt_id', 'host_installation_id', 'tool', 'role', 'account')


def feedback_request(action, params):
    binding = params.get('feedback')
    if not isinstance(binding, dict) or set(binding) != set(BINDING):
        raise JournalError('invalid_feedback_binding')
    if any(not isinstance(binding[k], str) or not 1 <= len(binding[k]) <= 256
           or any(ord(c) < 32 for c in binding[k]) for k in BINDING):
        raise JournalError('invalid_feedback_binding')
    if not all(isinstance(params.get(k),str) and params[k] for k in (*TARGET, 'nonce')):
        raise JournalError('invalid_feedback_target')
    managed = {key: params[key] for key in (*TARGET, 'nonce')}
    if action == 'submit_cockpit_feedback':
        text = params.get('text')
        if not isinstance(text, str) or not text.strip() or len(text.encode('utf-8')) > 65536:
            raise JournalError('invalid_prompt')
        if hashlib.sha256(text.encode('utf-8')).hexdigest() != params.get('text_sha256'):
            raise JournalError('feedback_changed')
        if type(params.get('expires_at')) != int or not time.time() <= params['expires_at'] <= time.time() + 180:
            raise JournalError('feedback_expired')
        managed['text'] = text
        return 'submit_managed', managed, binding
    if action == 'cockpit_feedback_status':
        return 'managed_status', managed, binding
    raise JournalError('unknown_feedback_action')


def _private_config(path):
    mode = path.lstat().st_mode
    if not stat.S_ISREG(mode) or (os.name != 'nt' and mode & 0o077):
        raise ValueError("private_source_config_required")
    with path.open('rb') as file:
        contents = file.read(128 * 1024 + 1)
    if len(contents)>128*1024:
        raise ValueError("oversize_source_config")
    return json.loads(contents)


def assignment_current(path, installation_id, owner_scope, target, binding, expires_at=None):
    if installation_id != binding.get('host_installation_id') or (expires_at is not None and time.time() > expires_at):
        return False
    try:
        items = _private_config(path)['bindings']
        if not isinstance(items, list) or len(items) > 256:
            return False
        matches = [item for item in items if isinstance(item, dict)
                   and item.get('owner_scope') == owner_scope
                   and all(item.get(k) == target.get(k) and target.get(k) for k in TARGET)]
        return len(matches) == 1 and all(matches[0].get(k) == binding[k] for k in BINDING)
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        return False


async def authorize_at_hub(path, binding, params, lookup, *, observation=None):
    """Claim once at the authority after rechecking the current human and source grants."""
    class PinnedSocket(connect):
        def process_redirect(self,exc):
            return exc

    async def check():
        authority = _private_config(path)['authority']
        uri = urlsplit(authority['endpoint'])
        if (uri.path!='/hub/feedback' or uri.query or uri.fragment or uri.username is not None
                or uri.password is not None or not uri.hostname or not (uri.scheme=='wss'
                or uri.scheme=='ws' and uri.hostname in {'127.0.0.1','localhost','::1'})):
            return False
        target = {**binding,'window_id':params['window_id'],'terminal_session_id':params['session_id'],
                  'generation':params['process_generation'],'journal_id':params['journal_id']}
        fields = {'target':target,'nonce':params['nonce']}
        if observation is not None:
            action='source_observed'
            fields['observation']=observation
        elif not lookup:
            action='source_submit'
            fields['text_sha256'] = hashlib.sha256(params['text'].encode('utf-8')).hexdigest()
        else:
            action='source_status'
        async with PinnedSocket(uri.geturl(), origin=authority['origin'], proxy=None,open_timeout=3,
                                close_timeout=1,max_size=4096) as socket:
            await socket.send(json.dumps({'op':action,
                                          'token':authority['token'],'params':fields}))
            reply=json.loads(await socket.recv())
            field='recorded' if observation is not None else 'authorized'
            return reply.get('ok') is True and reply.get('result')=={field:True,'nonce':fields['nonce']}
    try:
        return await asyncio.wait_for(check(),5)
    except Exception:
        return False
