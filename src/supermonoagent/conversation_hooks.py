"""Observational, process-scoped hook helper. Nothing is installed by importing.

Secrets live in an owner-private file, never provider argv. A failed ingestion
must not block the CLI or erase the original Codex notifier's one invocation.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler
import uuid

from .conversation_contract import JournalError, canonical_json, checked_id
from .conversation_sources import private_directory, private_file, check_private_file
from .security import loopback

MAX_HOOK_BYTES = 1024 * 1024


def hook_argv(config_file,provider):
    """Prepare the helper invocation; payload delivery stays provider-owned."""
    if provider not in {'codex','claude'}:
        raise JournalError('invalid_hook_provider')
    prefix=[sys.executable,'journal-hook'] if getattr(sys,'frozen',False) else [
        sys.executable,'-m','supermonoagent.conversation_hooks']
    return [*prefix,'--config-file',str(Path(config_file)),provider]


def _endpoint(value):
    try: parts = urlsplit(value)
    except (ValueError, TypeError): raise JournalError('invalid_hook_endpoint') from None
    if (parts.scheme != 'http' or not loopback(parts.hostname or '') or parts.username
            or parts.password or parts.query or parts.fragment or not parts.port):
        raise JournalError('invalid_hook_endpoint')
    return value


def write_hook_config(state_dir, *, binding_id, capability, terminal_generation, endpoint, original_notifier=None):
    checked_id(binding_id)
    checked_id(terminal_generation, 512)
    endpoint = _endpoint(endpoint)
    if not isinstance(capability,str) or not capability or len(capability)>512:
        raise JournalError('invalid_hook_config')
    original = original_notifier or []
    if (not isinstance(original,list) or len(original)>64 or any(not isinstance(arg,str)
            or '\0' in arg or len(arg)>32768 for arg in original)):
        raise JournalError('invalid_hook_config')
    directory = private_directory(state_dir)
    for existing in directory.glob('hook-*.json'):
        private_file(existing)
    path = directory / ('hook-' + uuid.uuid4().hex + '.json')
    with path.open('xb') as stream:
        private_file(path)
        stream.write(canonical_json(dict(binding_id=binding_id,capability=capability,
            terminal_generation=terminal_generation,endpoint=endpoint,original_notifier=original)))
        stream.flush()
        os.fsync(stream.fileno())
    return path


def claude_settings(command, *, existing=None):
    """Prepare an additional --settings layer; retain all existing hook lists."""
    if not isinstance(command,str) or not command or '\0' in command:
        raise JournalError('invalid_hook_command')
    settings = copy.deepcopy(existing or {})
    hooks = settings.setdefault('hooks',{})
    if not isinstance(hooks,dict): raise JournalError('invalid_hook_settings')
    for event in ('UserPromptSubmit','Stop'):
        values = hooks.setdefault(event,[])
        if not isinstance(values,list): raise JournalError('invalid_hook_settings')
        values.append(dict(hooks=[dict(type='command',command=command)]))
    return settings


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def _post(endpoint, body):
    # Ignore ambient proxies. Never follow a redirect with a binding capability.
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    request = Request(_endpoint(endpoint), data=canonical_json(body),
        headers={'Content-Type':'application/json'}, method='POST')
    with opener.open(request, timeout=2) as response:
        return response.status == 200


def _spawn(argv):
    subprocess.Popen(argv, stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))


def run_hook(config_file, provider, raw_payload, *, post=None, spawn=None):
    config = None
    try:
        path = check_private_file(config_file)
        if path.stat().st_size>16384: return False
        config = json.loads(path.read_bytes())
        if provider not in {'codex','claude'}: return False
        if not isinstance(raw_payload,str) or len(raw_payload.encode('utf8'))>MAX_HOOK_BYTES: return False
        payload = json.loads(raw_payload)
        if not isinstance(payload,dict): return False
        payload['_terminal_generation'] = config['terminal_generation']
        (post or _post)(_endpoint(config['endpoint']), dict(binding_id=config['binding_id'],
            capability=config['capability'],payload=payload))
        return True
    except Exception:
        # Intentional fail-open provider hook: diagnostics never contain bodies,
        # config paths, authentication material, or provider exception strings.
        return False
    finally:
        if (provider == 'codex' and isinstance(config,dict) and config.get('original_notifier')
                and isinstance(raw_payload,str)):
            try: (spawn or _spawn)([*config['original_notifier'],raw_payload])
            except Exception: pass


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config-file', type=Path, required=True)
    parser.add_argument('provider', choices=['codex','claude'])
    parser.add_argument('payload', nargs='?')
    args = parser.parse_args(argv)
    # Codex appends one final argv; Claude sends JSON on stdin.
    raw = args.payload if args.provider == 'codex' else sys.stdin.buffer.read(MAX_HOOK_BYTES+1).decode('utf8',errors='replace')
    run_hook(args.config_file,args.provider,raw)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
