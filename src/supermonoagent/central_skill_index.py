"""Rank central catalog metadata locally; never transmit terminal context."""
from __future__ import annotations

from collections import OrderedDict
import hashlib
import json
import math
import re
import threading
import time

from .mylib_assistant import _terms, evolution_suggestions


ENDPOINT = 'https://117.50.48.22/mylib/mcp'
_HEX = re.compile(r'(?:[0-9a-f]{40}|[0-9a-f]{64})\Z')
_ID = re.compile(r'[0-9a-f]{64}\Z')
_CONTROL = re.compile(r'[\x00-\x1f\x7f-\x9f\ud800-\udfff]')


def _require(condition):
    if not condition:
        raise ValueError('Central catalog unavailable')


def _text(value, maximum, *, empty=False):
    _require(isinstance(value, str) and (empty or bool(value)) and len(value) <= maximum
             and not _CONTROL.search(value))
    return value


def _revision(value):
    _require(isinstance(value, str) and bool(_HEX.fullmatch(value)))
    return value


def _path(value):
    _text(value, 2048)
    _require(not value.startswith('/') and '\\' not in value and ':' not in value
             and all(part not in {'', '.', '..'} for part in value.split('/')))
    return value


def _card(value):
    _require(isinstance(value, dict) and set(value) == {'id', 'name', 'description', 'path', 'available'})
    _require(isinstance(value['id'], str) and bool(_ID.fullmatch(value['id'])))
    _require(type(value['available']) is bool)
    return {'id': value['id'], 'name': _text(value['name'], 256),
            'description': _text(value['description'], 1024, empty=True),
            'path': _path(value['path']), 'available': value['available']}


class CentralSkillIndex:
    def __init__(self, local_index, remote_client, *, ttl=300, budget=15, clock=time.monotonic):
        for value in (ttl, budget):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError('Invalid catalog timing')
        self._local, self._remote = local_index, remote_client
        self._ttl, self._budget, self._clock = ttl, budget, clock
        self._lock = threading.RLock()
        self._cache = OrderedDict()
        self._current = None
        self._expires = self._retry_at = 0
        self._refresh = None
        self._preview = None
        self._closed = False

    def _download(self, cancel):
        status = self._remote.catalog_status(cancel_event=cancel)
        _require(not cancel.is_set() and isinstance(status, dict) and status.get('available') is True)
        revision, source = _revision(status.get('revision')), _revision(status.get('source_revision'))
        count = status.get('skill_count')
        _require(type(count) is int and 0 <= count <= 4096)
        cards, ids, paths, cursors, cursor, size = [], set(), set(), set(), None, 0
        for _ in range(64):
            _require(not cancel.is_set())
            page = self._remote.list_skills(cursor=cursor, limit=100, cancel_event=cancel)
            _require(not cancel.is_set() and isinstance(page, dict)
                     and set(page) == {'revision', 'items', 'next_cursor', 'total'})
            _require(page['revision'] == revision and type(page['total']) is int and page['total'] == count)
            _require(isinstance(page['items'], list) and len(page['items']) <= 100)
            for raw in page['items']:
                item = _card(raw)
                _require(item['id'] not in ids and item['path'] not in paths)
                ids.add(item['id']); paths.add(item['path']); cards.append(item)
                size += len(json.dumps(item, ensure_ascii=False).encode('utf-8'))
                _require(len(cards) <= count and size <= 8 * 1024 * 1024)
            cursor = page['next_cursor']
            if cursor is None:
                _require(len(cards) == count)
                return {'revision': revision, 'source_revision': source, 'cards': tuple(cards)}
            _require(bool(page['items']) and len(cards) < count)
            _text(cursor, 512)
            _require(bool(re.fullmatch(r'[A-Za-z0-9_-]+', cursor)) and cursor not in cursors)
            cursors.add(cursor)
        raise ValueError('Central catalog unavailable')

    def _refresh_worker(self, job):
        cancel, done = job
        snapshot = None
        try:
            snapshot = self._download(cancel)
        except Exception:
            pass  # Never surface remote response bodies, paths or credentials.
        finally:
            with self._lock:
                if snapshot is not None and not cancel.is_set() and not self._closed:
                    revision = snapshot['revision']
                    self._cache[revision] = snapshot
                    self._cache.move_to_end(revision)
                    while len(self._cache) > 2:
                        self._cache.popitem(last=False)
                    self._current = revision
                    self._expires = self._clock() + self._ttl
                    self._retry_at = 0
                elif not self._closed:
                    self._retry_at = self._clock() + 30
                if self._refresh is job:
                    self._refresh = None
            done.set()

    def _snapshot(self):
        with self._lock:
            snapshot = self._cache.get(self._current)
            if self._closed:
                return None, 'closed', True
            if snapshot is not None and self._clock() < self._expires:
                return snapshot, 'available', False
            if self._refresh is not None:
                return snapshot, 'busy', True
            if self._clock() < self._retry_at:
                return snapshot, 'unavailable', True
            job = (threading.Event(), threading.Event())
            self._refresh = job
            threading.Thread(target=self._refresh_worker, args=(job,), daemon=True,
                             name='central-skill-catalog').start()
        if not job[1].wait(self._budget):
            job[0].set()
            with self._lock:
                self._retry_at = self._clock() + 30
        with self._lock:
            if self._closed:
                return None, 'closed', True
            snapshot = self._cache.get(self._current)
            stale = job[0].is_set() or snapshot is None or self._clock() >= self._expires
            return snapshot, 'unavailable' if stale else 'available', stale

    def recommend(self, context, limit=5):
        if not isinstance(context, str) or len(context) > 32768:
            raise ValueError('Context must be text of at most 32768 characters')
        if type(limit) is not int or not 1 <= limit <= 10:
            raise ValueError('Limit must be between 1 and 10')
        snapshot, status, stale = self._snapshot()
        if snapshot is None:
            result = dict(self._local.recommend(context, limit=limit))
            result['library'] = {'kind': 'local', 'central_status': status}
            return result
        query, ranked = _terms(context), []
        for card in snapshot['cards']:
            matched = query & _terms(card['name'] + ' ' + card['description'])
            if card['available'] and matched:
                ranked.append({key: card[key] for key in ('name', 'description', 'path', 'id')})
                ranked[-1].update(source=card['path'], revision=snapshot['revision'], score=len(matched),
                                  reason='匹配上下文：' + '、'.join(sorted(matched)[:8]))
        ranked.sort(key=lambda card: (-card['score'], card['name'], card['path']))
        return {'mode': 'central-mcp', 'skills': ranked[:limit], 'indexed': len(snapshot['cards']),
                'available': True, 'evolution': evolution_suggestions(context),
                'library': {'kind': 'central', 'endpoint': ENDPOINT, 'revision': snapshot['revision'],
                            'source_revision': snapshot['source_revision'], 'stale': stale,
                            'central_status': status}}

    def read_skill(self, request):
        try:
            _require(isinstance(request, dict) and set(request) == {'skill_id', 'revision'})
            skill_id, revision = request['skill_id'], _revision(request['revision'])
            _require(isinstance(skill_id, str) and bool(_ID.fullmatch(skill_id)))
            with self._lock:
                _require(not self._closed and self._preview is None)
                snapshot = self._cache.get(revision)
                _require(snapshot is not None)
                card = next((c for c in snapshot['cards'] if c['id'] == skill_id and c['available']), None)
                _require(card is not None)
                job = (threading.Event(), threading.Event())
                self._preview = job
            result = []

            def fetch():
                try:
                    raw = self._remote.read_skill(skill_id, revision=revision, cancel_event=job[0])
                    _require(isinstance(raw, dict) and raw.get('id') == skill_id
                             and raw.get('skill_id') == skill_id and raw.get('revision') == revision
                             and raw.get('path') == card['path'] and raw.get('available') is True)
                    _require(isinstance(raw.get('sha256'), str) and bool(_ID.fullmatch(raw['sha256'])))
                    text, size, truncated = raw.get('text'), raw.get('size'), raw.get('truncated')
                    _require(isinstance(text, str) and len(text) <= 65536
                             and len(text.encode('utf-8')) <= 65536 and type(size) is int
                             and 0 <= size <= 64 * 1024 * 1024 and type(truncated) is bool
                             and (not truncated or size > len(text.encode('utf-8'))))
                    if not truncated:
                        _require(len(text.encode('utf-8')) == size
                                 and hashlib.sha256(text.encode('utf-8')).hexdigest() == raw['sha256'])
                    result.append({'id': skill_id, 'skill_id': skill_id, 'revision': revision,
                                   'source_revision': snapshot['source_revision'], 'source': card['path'],
                                   'path': card['path'], 'name': card['name'], 'description': card['description'],
                                   'text': text, 'sha256': raw['sha256'], 'size': size, 'truncated': truncated})
                except Exception:
                    pass
                finally:
                    with self._lock:
                        if self._preview is job:
                            self._preview = None
                    job[1].set()

            threading.Thread(target=fetch, daemon=True, name='central-skill-preview').start()
            if not job[1].wait(self._budget):
                job[0].set()
            with self._lock:
                _require(not self._closed and not job[0].is_set() and bool(result))
                return result[0]
        except Exception:
            raise ValueError('Central skill preview unavailable') from None

    def close(self):
        with self._lock:
            self._closed = True
            for job in (self._refresh, self._preview):
                if job is not None:
                    job[0].set()
                    job[1].set()
            self._cache.clear()
            self._current = None
        close = getattr(self._remote, 'close', None)
        if close:
            try:
                close()
            except Exception:
                pass
