import copy
import hashlib
import threading
import time

import pytest

from supermonoagent.central_skill_index import CentralSkillIndex


REV = 'a' * 40
SOURCE = 'b' * 40
CARD = {'id': 'c' * 64, 'name': 'debugging', 'description': 'debug Python failures',
        'path': 'skills/debug/SKILL.md', 'available': True}


class Local:
    def recommend(self, context, limit=5):
        return {'mode': 'local-keywords', 'skills': [], 'indexed': 2, 'available': True,
                'evolution': [], 'local_context': context}


class Remote:
    def __init__(self):
        self.calls = []
        self.revision = REV
        self.cards = [copy.deepcopy(CARD)]
        self.failure = False

    def catalog_status(self, *, cancel_event):
        self.calls.append(('status',))
        if self.failure:
            raise RuntimeError('private fixture value must not escape')
        return {'available': True, 'revision': self.revision, 'source_revision': SOURCE,
                'skill_count': len(self.cards)}

    def list_skills(self, cursor=None, limit=100, *, cancel_event):
        self.calls.append(('list', cursor, limit))
        return {'revision': self.revision, 'items': copy.deepcopy(self.cards),
                'total': len(self.cards), 'next_cursor': None}

    def read_skill(self, skill_id, revision=None, *, cancel_event):
        self.calls.append(('read', skill_id, revision))
        text = 'owned instructions'
        return {**CARD, 'skill_id': skill_id, 'revision': revision, 'text': text,
                'sha256': hashlib.sha256(text.encode()).hexdigest(), 'size': len(text),
                'truncated': False}

    def search(self, *args, **kwargs):
        pytest.fail('Private context must never be searched remotely')


def test_ranks_downloaded_metadata_locally_and_preview_pins_source():
    remote = Remote()
    index = CentralSkillIndex(Local(), remote)
    result = index.recommend('private fixture token debug Python')
    assert result['mode'] == 'central-mcp'
    assert result['indexed'] == 1
    assert result['skills'][0]['id'] == CARD['id']
    assert result['skills'][0]['revision'] == REV
    assert result['skills'][0]['source'] == CARD['path']
    assert result['library']['source_revision'] == SOURCE
    assert remote.calls == [('status',), ('list', None, 100)]
    preview = index.read_skill({'skill_id': CARD['id'], 'revision': REV})
    assert preview['text'] == 'owned instructions'
    assert preview['source'] == CARD['path']
    index.close()


def test_ttl_failed_refresh_stale_cache_and_backoff():
    now = [0]
    remote = Remote()
    index = CentralSkillIndex(Local(), remote, ttl=10, clock=lambda: now[0])
    assert not index.recommend('debug')['library']['stale']
    assert not index.recommend('debug')['library']['stale']
    assert len(remote.calls) == 2
    now[0] = 11
    remote.failure = True
    stale = index.recommend('debug')
    assert stale['mode'] == 'central-mcp' and stale['library']['stale']
    assert stale['library']['central_status'] == 'unavailable'
    count = len(remote.calls)
    assert index.recommend('debug')['library']['stale']
    assert len(remote.calls) == count
    now[0] = 42
    remote.failure = False
    assert not index.recommend('debug')['library']['stale']
    index.close()


def test_first_failure_returns_explicit_local_without_exception_body():
    remote = Remote()
    remote.failure = True
    index = CentralSkillIndex(Local(), remote)
    result = index.recommend('debug')
    assert result['mode'] == 'local-keywords'
    assert result['library'] == {'kind': 'local', 'central_status': 'unavailable'}
    assert 'private fixture value' not in str(result)
    index.close()


def test_pagination_must_be_complete_pinned_and_unique():
    remote = Remote()
    remote.cards = [CARD, {**CARD, 'id': 'd' * 64, 'path': 'other/SKILL.md'}]
    def page(cursor=None, limit=100, *, cancel_event):
        remote.calls.append(('list', cursor, limit))
        offset = 0 if cursor is None else 1
        return {'revision': REV, 'items': [remote.cards[offset]], 'total': 2,
                'next_cursor': 'page_two' if cursor is None else None}
    remote.list_skills = page
    index = CentralSkillIndex(Local(), remote)
    assert index.recommend('Python')['indexed'] == 2
    assert remote.calls == [('status',), ('list', None, 100), ('list', 'page_two', 100)]
    index.close()


@pytest.mark.parametrize('malice', [
    'cross_revision', 'short_count', 'loop', 'duplicate_id', 'duplicate_path',
    'absolute', 'traversal', 'backslash', 'control', 'unknown_field',
    'bad_id', 'bad_available', 'long_name', 'long_description', 'bad_total',
    'too_many', 'bad_source', 'empty_page', 'bad_cursor'])
def test_malicious_or_incomplete_catalog_never_replaces_complete_cache(malice):
    now = [0]
    remote = Remote()
    index = CentralSkillIndex(Local(), remote, ttl=1, clock=lambda: now[0])
    index.recommend('debug')
    original = remote.list_skills
    status = remote.catalog_status
    def invalid(cursor=None, limit=100, *, cancel_event):
        result = original(cursor, limit, cancel_event=cancel_event)
        card = result['items'][0]
        if malice == 'cross_revision': result['revision'] = 'e' * 40
        elif malice == 'short_count': result['items'] = []
        elif malice == 'loop': result.update(total=2, next_cursor='loop')
        elif malice == 'duplicate_id': result['items'].append({**card, 'path': 'other/SKILL.md'})
        elif malice == 'duplicate_path': result['items'].append({**card, 'id': 'd' * 64})
        elif malice == 'absolute': card['path'] = '/private/SKILL.md'
        elif malice == 'traversal': card['path'] = 'skills/../private/SKILL.md'
        elif malice == 'backslash': card['path'] = 'skills\\private\\SKILL.md'
        elif malice == 'control': card['name'] = 'debug\x00private'
        elif malice == 'unknown_field': card['secret'] = 'private fixture value'
        elif malice == 'bad_id': card['id'] = 'not-id'
        elif malice == 'bad_available': card['available'] = 1
        elif malice == 'long_name': card['name'] = 'x' * 257
        elif malice == 'long_description': card['description'] = 'x' * 1025
        elif malice == 'bad_total': result['total'] = True
        elif malice == 'empty_page': result.update(items=[], next_cursor='more')
        elif malice == 'bad_cursor': result['next_cursor'] = '../private'
        return result
    def invalid_status(*, cancel_event):
        result = status(cancel_event=cancel_event)
        if malice in {'loop', 'duplicate_id', 'duplicate_path'}: result['skill_count'] = 2
        if malice == 'too_many': result['skill_count'] = 4097
        if malice == 'bad_source': result['source_revision'] = '../private'
        return result
    remote.list_skills, remote.catalog_status = invalid, invalid_status
    now[0] = 2
    result = index.recommend('debug')
    assert result['library']['stale']
    assert result['library']['revision'] == REV
    assert result['indexed'] == 1
    assert 'private fixture value' not in str(result)
    index.close()


def test_busy_calls_do_not_wait_or_duplicate_refresh_and_close_cancels():
    remote = Remote()
    entered, release = threading.Event(), threading.Event()
    original = remote.catalog_status
    def block(*, cancel_event):
        entered.set()
        while not release.wait(.005):
            if cancel_event.is_set():
                return None
        return original(cancel_event=cancel_event)
    remote.catalog_status = block
    index = CentralSkillIndex(Local(), remote, budget=2)
    results = []
    caller = threading.Thread(target=lambda: results.append(index.recommend('debug')))
    caller.start()
    assert entered.wait(1)
    start = time.monotonic()
    busy = index.recommend('private debug')
    assert time.monotonic() - start < .2
    assert busy['library']['central_status'] == 'busy'
    index.close()
    caller.join(1)
    assert not caller.is_alive()
    assert results[0]['library']['central_status'] == 'closed'
    release.set()
    assert not remote.calls


def test_budget_bounds_even_noncooperative_remote_and_discards_late_result():
    remote = Remote()
    release, entered = threading.Event(), threading.Event()
    original = remote.catalog_status
    def block(*, cancel_event):
        entered.set()
        release.wait(2)
        return original(cancel_event=cancel_event)
    remote.catalog_status = block
    index = CentralSkillIndex(Local(), remote, budget=.03)
    start = time.monotonic()
    assert index.recommend('debug')['library']['kind'] == 'local'
    assert entered.is_set() and time.monotonic() - start < .5
    release.set()
    for _ in range(100):
        if index._refresh is None: break
        time.sleep(.005)
    assert index.recommend('debug')['library']['kind'] == 'local'
    assert not any(call[0] == 'list' for call in remote.calls)
    index.close()


def test_preview_requires_cached_id_revision_and_keeps_only_last_two_revisions():
    now = [0]
    remote = Remote()
    index = CentralSkillIndex(Local(), remote, ttl=1, clock=lambda: now[0])
    index.recommend('debug')
    with pytest.raises(ValueError, match='preview unavailable'):
        index.read_skill({'skill_id': 'd' * 64, 'revision': REV})
    with pytest.raises(ValueError):
        index.read_skill({'skill_id': CARD['id'], 'revision': REV, 'path': '../private'})
    now[0], remote.revision = 2, 'd' * 40
    index.recommend('debug')
    assert index.read_skill({'skill_id': CARD['id'], 'revision': REV})['revision'] == REV
    now[0], remote.revision = 4, 'e' * 40
    index.recommend('debug')
    with pytest.raises(ValueError):
        index.read_skill({'skill_id': CARD['id'], 'revision': REV})
    index.close()


@pytest.mark.parametrize('field,value', [('id', 'd' * 64), ('skill_id', 'd' * 64),
    ('revision', 'd' * 40), ('path', 'other/SKILL.md'), ('sha256', 'not-hash'),
    ('sha256', '0' * 64), ('text', 'x' * 65537), ('size', True), ('truncated', 1)],
    ids=['id', 'skill-id', 'revision', 'path', 'sha-format', 'sha-mismatch', 'text-limit', 'size-type', 'flag-type'])
def test_malicious_preview_is_rejected_without_leaking_body(field, value):
    remote = Remote()
    original = remote.read_skill
    def invalid(*args, **kwargs):
        result = original(*args, **kwargs)
        result[field] = value
        result['private'] = 'fixture sensitive exception content'
        return result
    remote.read_skill = invalid
    index = CentralSkillIndex(Local(), remote)
    index.recommend('debug')
    with pytest.raises(ValueError) as error:
        index.read_skill({'skill_id': CARD['id'], 'revision': REV})
    assert str(error.value) == 'Central skill preview unavailable'
    index.close()


def test_evolution_observations_remain_local_and_context_validation_precedes_network():
    remote = Remote()
    index = CentralSkillIndex(Local(), remote)
    with pytest.raises(ValueError): index.recommend('x' * 32769)
    with pytest.raises(ValueError): index.recommend('debug', True)
    assert not remote.calls
    result = index.recommend('Error: owned failure\nError: second failure')
    assert result['evolution'][0]['status'] == 'unverified'
    assert all('owned failure' not in str(call) for call in remote.calls)
    index.close()


@pytest.mark.parametrize('boundary', ['bytes', 'pages', 'cursor-loop'])
def test_catalog_global_bounds_and_cursor_loop(boundary):
    remote = Remote()
    count = 2800 if boundary == 'bytes' else 100
    remote.cards = [{**CARD, 'id': hashlib.sha256(str(n).encode()).hexdigest(),
                     'path': ('s' * 2000 + '/' if boundary == 'bytes' else 'skills/') + str(n) + '/SKILL.md',
                     'name': 'd' * 256, 'description': 'x' * 1024}
                    for n in range(count)]
    page_calls = []
    def page(cursor=None, limit=100, *, cancel_event):
        page_calls.append(cursor)
        offset = int(cursor or 0)
        end = min(count, offset + (100 if boundary == 'bytes' else 1))
        next_cursor = str(end) if end < count else None
        if boundary == 'cursor-loop': next_cursor = '1'
        return {'revision': REV, 'total': count, 'items': copy.deepcopy(remote.cards[offset:end]),
                'next_cursor': next_cursor}
    remote.list_skills = page
    index = CentralSkillIndex(Local(), remote)
    result = index.recommend('debug')
    assert result['library']['kind'] == 'local'
    assert len(page_calls) <= 64
    index.close()


@pytest.mark.parametrize('operation', ['close', 'budget'])
def test_preview_cancellation_and_busy_are_bounded(operation):
    remote = Remote()
    index = CentralSkillIndex(Local(), remote, budget=.05 if operation == 'budget' else 2)
    index.recommend('debug')
    entered, cancelled = threading.Event(), threading.Event()
    def block(*args, cancel_event, **kwargs):
        entered.set()
        if cancel_event.wait(3): cancelled.set()
        raise RuntimeError('private fixture content')
    remote.read_skill = block
    errors = []
    def preview():
        try:
            index.read_skill({'skill_id': CARD['id'], 'revision': REV})
        except ValueError as error:
            errors.append(str(error))
    caller = threading.Thread(target=preview)
    caller.start()
    assert entered.wait(1)
    with pytest.raises(ValueError, match='preview unavailable'):
        index.read_skill({'skill_id': CARD['id'], 'revision': REV})
    if operation == 'close': index.close()
    caller.join(1)
    assert not caller.is_alive() and cancelled.wait(1)
    assert errors == ['Central skill preview unavailable']
    index.close()


def test_unavailable_cards_are_indexed_but_not_recommended_or_previewed():
    remote = Remote()
    remote.cards[0]['available'] = False
    index = CentralSkillIndex(Local(), remote)
    result = index.recommend('debug Python')
    assert result['indexed'] == 1 and result['skills'] == []
    with pytest.raises(ValueError):
        index.read_skill({'skill_id': CARD['id'], 'revision': REV})
    index.close()
