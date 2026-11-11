"""Bounded journal wire contracts shared by source and private mirror service."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from urllib.parse import urlsplit

PROTOCOL = 'sma-journal-1'
SCHEMA_VERSION = 1
OP_JOURNAL = 24
CAPABILITY = 'conversation_journal'
PAGE_DEFAULT = 50
PAGE_MAX = 100
MAX_ENVELOPE = 192 * 1024
CONTENT_CHUNK = 48 * 1024
PREVIEW_CHARS = 1024
MIRROR_BATCH_EVENTS = 50
EVENT_KINDS = frozenset({'raw_output', 'bootstrap', 'user_input', 'assistant_candidate',
                         'assistant_final', 'turn_outcome', 'capture_gap', 'status', 'binding'})
DESTINATION_FIELDS = frozenset({'destination_id', 'authority', 'service_path', 'owner_scope',
                               'external_room', 'host_installation_id', 'source_epoch'})
BATCH_IDENTITY = ('destination_id', 'owner_scope', 'external_room', 'host_installation_id',
                  'journal_id', 'source_epoch', 'batch_id', 'first_seq', 'last_seq', 'batch_sha256')


class JournalError(ValueError):
    """Fixed public code; never interpolate a payload, credential or OS error."""
    def __init__(self, code='invalid_request'):
        self.code = code if isinstance(code, str) and re.fullmatch(r'[a-z][a-z0-9_]{0,63}', code) else 'invalid_request'
        super().__init__(self.code)


def checked_id(value, maximum=128):
    if (not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,' + str(maximum-1) + r'}', value)
            or '..' in value):
        raise JournalError()
    return value


def checked_label(value, maximum=128, *, empty=False):
    if (not isinstance(value, str) or len(value) > maximum or not empty and not value
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise JournalError()
    return value


def checked_integer(value, minimum=0, maximum=2**53-1):
    if type(value) is not int or not minimum <= value <= maximum:
        raise JournalError()
    return value


def canonical_json(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise JournalError() from None


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise JournalError()
        result[key] = value
    return result


def _invalid_constant(_value):
    raise JournalError()


def decode_json(raw):
    if not isinstance(raw, (str, bytes)):
        raise JournalError()
    try:
        encoded = raw.encode('utf-8') if isinstance(raw, str) else raw
        if len(encoded) > MAX_ENVELOPE:
            raise JournalError('frame_too_large')
        value = json.loads(encoded.decode('utf-8'), object_pairs_hook=_unique,
                           parse_constant=_invalid_constant)
        if not isinstance(value, dict):
            raise JournalError()
        return value
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise JournalError() from None


def normalize_destination(value):
    if not isinstance(value, dict) or set(value) != DESTINATION_FIELDS:
        raise JournalError('invalid_destination')
    result = dict(value)
    for key in ['destination_id', 'owner_scope', 'host_installation_id', 'source_epoch']:
        checked_id(result[key])
    checked_label(result['external_room'])
    path = result['service_path']
    if not isinstance(path, str) or not re.fullmatch(r'/[A-Za-z0-9/_-]{1,255}', path) or '//' in path:
        raise JournalError('invalid_destination')
    try:
        uri = urlsplit(result['authority'])
        scheme = {'wss': 'https', 'ws': 'http'}.get(uri.scheme, uri.scheme)
        host, port = uri.hostname, uri.port
        if (scheme not in {'http', 'https'} or not host or uri.username is not None or uri.password is not None
                or uri.path not in {'', '/'} or uri.query or uri.fragment or any(c.isspace() for c in host)
                or port is not None and not 1 <= port <= 65535):
            raise JournalError('invalid_destination')
        loopback = host.lower() == 'localhost'
        try:
            loopback = loopback or ipaddress.ip_address(host).is_loopback
        except ValueError:
            pass
        if scheme == 'http' and not loopback:
            raise JournalError('invalid_destination')
        host = host.lower()
        if ':' in host:
            host = '[' + host + ']'
        default = 443 if scheme == 'https' else 80
        result['authority'] = scheme + '://' + host + (':' + str(port) if port is not None and port != default else '')
    except (ValueError, TypeError, AttributeError):
        raise JournalError('invalid_destination') from None
    return result


def batch_digest(batch):
    if not isinstance(batch, dict):
        raise JournalError('invalid_batch')
    return hashlib.sha256(canonical_json({key: value for key, value in batch.items() if key != 'batch_sha256'})).hexdigest()


def validate_receipt(batch, receipt):
    if (not isinstance(batch, dict) or not isinstance(receipt, dict)
            or set(receipt) != set(BATCH_IDENTITY) | {'through_seq', 'last_event_id', 'commit_id'}):
        raise JournalError('invalid_receipt')
    if batch.get('batch_sha256') != batch_digest(batch):
        raise JournalError('invalid_receipt')
    if any(receipt.get(key) != batch.get(key) for key in BATCH_IDENTITY):
        raise JournalError('invalid_receipt')
    events = batch.get('events')
    if (not isinstance(events, list) or not events or receipt['through_seq'] != batch['last_seq']
            or receipt['last_event_id'] != events[-1].get('event_id')):
        raise JournalError('invalid_receipt')
    first, last = checked_integer(batch['first_seq'], 1), checked_integer(batch['last_seq'], 1)
    for key in ['first_seq', 'last_seq', 'through_seq']:
        checked_integer(receipt[key], 1)
    if last-first+1 != len(events) or any(type(event.get('seq')) is not int or event['seq'] != first+index for index, event in enumerate(events)):
        raise JournalError('invalid_receipt')
    checked_id(receipt['commit_id'])
