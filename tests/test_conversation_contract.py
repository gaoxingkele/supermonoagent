import copy
import json

import pytest

from supermonoagent.conversation_contract import (
    JournalError, batch_digest, canonical_json, decode_json,
    normalize_destination, validate_receipt,
)


def fixture_batch():
    batch = {
        'destination_id': 'dest1', 'owner_scope': 'owner1', 'external_room': 'office',
        'host_installation_id': 'host1', 'journal_id': 'journal1', 'source_epoch': 'epoch1',
        'batch_id': 'batch1', 'first_seq': 1, 'last_seq': 2,
        'events': [{'event_id': 'e1', 'seq': 1}, {'event_id': 'e2', 'seq': 2}], 'contents': [],
    }
    batch['batch_sha256'] = batch_digest(batch)
    return batch


def receipt_for(batch):
    return {**{key: value for key, value in batch.items() if key not in {'events', 'contents'}},
            'through_seq': batch['last_seq'], 'last_event_id': 'e2', 'commit_id': 'commit1'}


def test_canonical_bytes_preserve_private_unicode_and_are_order_independent():
    value = {'text': '输入\n```python\nprint("example-token")\n```😀', 'number': 2}
    assert canonical_json(value) == canonical_json(dict(reversed(list(value.items()))))
    assert json.loads(canonical_json(value)) == value
    with pytest.raises(JournalError):
        canonical_json({'bad': float('nan')})


def test_wire_decoder_rejects_duplicate_keys_and_oversized_or_invalid_content():
    assert decode_json(b'{"action":"status"}') == {'action': 'status'}
    for value in [b'{"action":"read","action":"write"}', b'\xff', b'[]', b'x' * 196609]:
        with pytest.raises(JournalError):
            decode_json(value)


def test_destination_is_normalized_without_auth_fields_or_arbitrary_paths():
    value = {'destination_id': 'dest1', 'authority': 'https://RELAY.example:443/',
             'service_path': '/journals/ws', 'owner_scope': 'owner1', 'external_room': 'office',
             'host_installation_id': 'host1', 'source_epoch': 'epoch1'}
    assert normalize_destination(value)['authority'] == 'https://relay.example'
    for changes in [{'authority': 'http://public.example'}, {'authority': 'https://user:secret@relay.example'},
                    {'authority': 'https://relay.example/other'}, {'service_path': '/a/../journals'},
                    {'token': 'must-not-be-stored'}]:
        with pytest.raises(JournalError):
            normalize_destination({**value, **changes})


def test_receipt_accepts_only_exact_pending_scope_content_and_commit_barrier():
    batch = fixture_batch()
    validate_receipt(batch, receipt_for(batch))
    for key in ['destination_id', 'owner_scope', 'external_room', 'host_installation_id',
                'journal_id', 'source_epoch', 'batch_id', 'batch_sha256', 'last_event_id']:
        receipt = receipt_for(batch)
        receipt[key] = 'different'
        with pytest.raises(JournalError):
            validate_receipt(batch, receipt)
    for key in ['first_seq', 'last_seq', 'through_seq']:
        receipt = receipt_for(batch)
        receipt[key] += 1
        with pytest.raises(JournalError):
            validate_receipt(batch, receipt)
    receipt = receipt_for(batch)
    receipt['first_seq'] = True
    with pytest.raises(JournalError):
        validate_receipt(batch, receipt)
    changed = copy.deepcopy(batch)
    changed['contents'].append({'content_id': 'missing', 'bytes': 2, 'sha256': '0' * 64})
    with pytest.raises(JournalError):
        validate_receipt(changed, receipt_for(batch))
