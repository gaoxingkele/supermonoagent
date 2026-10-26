"""Folder inventories must stay bounded and unambiguous on Windows hosts."""
import copy
import hashlib
import json

import pytest

from supermonoagent.file_folders import (
    MAX_MANIFEST_BYTES,
    manifest_digest,
    parse_manifest,
    validate_manifest,
)


DIGEST = hashlib.sha256(b'owned fixture').hexdigest()


def manifest(path=None, *, size=0):
    path = ['file.txt'] if path is None else path
    return {'version': 1, 'root': 'Owned folder',
            'directories': [path[:length] for length in range(1, len(path))],
            'files': [{'path': path, 'size': size, 'sha256': DIGEST}]}


def test_preserves_unicode_names_empty_directories_and_zero_length_files():
    value = manifest(['\u8bba\u6587', '\U0001f600.txt'])
    value['root'] = '\u9879\u76ee\U0001f680'
    value['directories'].append(['empty'])
    result = validate_manifest(value)
    assert result == {**value, 'directories': [['empty'], ['\u8bba\u6587']]}
    assert result is not value
    result['directories'][0].append('changed')
    result['files'][0]['path'].append('changed')
    assert value['directories'] == [['\u8bba\u6587'], ['empty']]
    assert value['files'][0]['path'] == ['\u8bba\u6587', '\U0001f600.txt']


def test_canonical_order_and_digest_ignore_inventory_order():
    value = {'version': 1, 'root': 'root', 'directories': [['z'], ['a']],
             'files': [{'path': ['z', 'same'], 'size': 1, 'sha256': DIGEST},
                       {'path': ['a', 'same'], 'size': 2, 'sha256': DIGEST}]}
    original = copy.deepcopy(value)
    result = validate_manifest(value)
    assert result['directories'] == [['a'], ['z']]
    assert [entry['path'] for entry in result['files']] == [['a', 'same'], ['z', 'same']]
    reversed_value = {**value, 'directories': list(reversed(value['directories'])),
                      'files': list(reversed(value['files']))}
    expected = hashlib.sha256(json.dumps(result, sort_keys=True, separators=(',', ':'),
                                        ensure_ascii=False).encode('utf-8')).hexdigest()
    assert manifest_digest(value) == manifest_digest(reversed_value) == expected
    assert value == original


def test_empty_root_inventory_and_empty_directory_only_are_valid():
    for directories in [[], [['empty']], [['empty'], ['empty', 'nested']]]:
        value = {'version': 1, 'root': 'root', 'directories': directories, 'files': []}
        assert validate_manifest(value) == value


@pytest.mark.parametrize('name', [
    '', '.', '..', '/root', '\\root', 'C:', 'C:relative', 'C:\\root', '//server',
    '\\\\server', '\\\\?\\C:\\x', 'part/name', 'part\\name', 'file:stream',
    'file\x00', 'file\n', 'file\x1f', 'file\x7f', 'tail.', 'tail ', '<bad>',
    'quo"te', 'pipe|', 'wild*', 'wild?', 'CON', 'con.txt', 'PRN', 'AUX', 'NUL',
    'CLOCK$', 'CONIN$', 'CONOUT$', 'COM1', 'COM9.ext', 'LPT1', 'LPT9.ext',
    'COM\u00b9', 'LPT\u00b2.txt', '\ud800', '\udfff', 'x' * 201,
    '\U0001f600' * 101, '.supermono-partials', '.SUPERMONO-PARTIALS',
    'file\u0085', 'file\u009f',
])
@pytest.mark.parametrize('location', ['root', 'directory', 'file'])
def test_rejects_unsafe_components_without_changing_them(name, location):
    value = manifest()
    if location == 'root':
        value['root'] = name
    elif location == 'directory':
        value['directories'] = [[name]]
    else:
        value['files'][0]['path'] = [name]
    original = copy.deepcopy(value)
    with pytest.raises(ValueError):
        validate_manifest(value)
    assert value == original


@pytest.mark.parametrize('mutate', [
    lambda v: v.update(version=True), lambda v: v.update(version=1.0),
    lambda v: v.update(version=2), lambda v: v.update(extra=True),
    lambda v: v.pop('root'), lambda v: v.update(root=42),
    lambda v: v.update(directories=()), lambda v: v.update(files={}),
    lambda v: v['directories'].append('directory'),
    lambda v: v['directories'].append([]),
    lambda v: v['files'][0].update(path='file.txt'),
    lambda v: v['files'][0].update(path=()),
    lambda v: v['files'][0].update(path=[]),
    lambda v: v['files'][0].update(path=[4]),
    lambda v: v['files'][0].update(size=True),
    lambda v: v['files'][0].update(size=1.0),
    lambda v: v['files'][0].update(size=-1),
    lambda v: v['files'][0].update(size=512 * 1024 * 1024 + 1),
    lambda v: v['files'][0].update(sha256=DIGEST.upper()),
    lambda v: v['files'][0].update(sha256='g' * 64),
    lambda v: v['files'][0].update(sha256='0' * 63),
    lambda v: v['files'][0].update(sha256=True),
    lambda v: v['files'][0].update(absolute_path='C:\\secret'),
    lambda v: v['files'][0].pop('size'),
])
def test_requires_exact_json_schema(mutate):
    value = manifest()
    mutate(value)
    with pytest.raises(ValueError):
        validate_manifest(value)


@pytest.mark.parametrize('value', [None, [], 'json', True, 1, {'version': 1}])
def test_rejects_non_manifest_top_level(value):
    with pytest.raises(ValueError):
        validate_manifest(value)


@pytest.mark.parametrize('directories,paths', [
    ([['same'], ['same']], []), ([['same'], ['SAME']], []),
    ([['\u00e9'], ['e\u0301']], []), ([['Stra\u00dfe'], ['STRASSE']], []),
    ([], [['same'], ['same']]), ([], [['same'], ['SAME']]),
    ([], [['\u00e9'], ['e\u0301']]),
    ([['same']], [['same']]), ([['same']], [['SAME']]),
    ([], [['file'], ['file', 'nested']]),
    ([['child', 'nested']], []), ([], [['missing', 'file']]),
    ([['Dir']], [['dir', 'file']]),
    ([['Dir'], ['dir', 'nested']], []),
])
def test_rejects_aliases_missing_parents_and_prefix_conflicts(directories, paths):
    value = {'version': 1, 'root': 'root', 'directories': directories,
             'files': [{'path': p, 'size': 0, 'sha256': DIGEST} for p in paths]}
    with pytest.raises(ValueError):
        validate_manifest(value)


def test_limits_file_directory_count_and_aggregate_bytes():
    value = manifest()
    value['files'] = [{'path': [f'{n}.bin'], 'size': 32 * 1024 * 1024,
                       'sha256': DIGEST} for n in range(32)]
    assert len(validate_manifest(value)['files']) == 32
    value['files'][0]['size'] += 1
    with pytest.raises(ValueError):
        validate_manifest(value)
    value['files'][0]['size'] -= 1
    value['files'].append({'path': ['extra'], 'size': 0, 'sha256': DIGEST})
    with pytest.raises(ValueError):
        validate_manifest(value)
    value = {'version': 1, 'root': 'root', 'files': [],
             'directories': [[f'd{n}'] for n in range(256)]}
    assert len(validate_manifest(value)['directories']) == 256
    value['directories'].append(['extra'])
    with pytest.raises(ValueError):
        validate_manifest(value)


def test_depth_path_and_utf16_component_boundaries():
    assert validate_manifest(manifest(['x'] * 16))
    with pytest.raises(ValueError):
        validate_manifest(manifest(['x'] * 17))
    assert validate_manifest(manifest(['\U0001f600' * 100]))
    assert validate_manifest(manifest(['x' * 200] * 5 + ['f']))
    with pytest.raises(ValueError):
        validate_manifest(manifest(['x' * 200] * 6 + ['f']))


def test_both_wire_and_canonical_manifest_byte_budgets():
    value = {'version': 1, 'root': 'root', 'directories': [], 'files': []}
    raw = json.dumps(value, separators=(',', ':')).encode()
    assert parse_manifest(raw + b' ' * (MAX_MANIFEST_BYTES - len(raw))) == value
    with pytest.raises(ValueError):
        parse_manifest(raw + b' ' * (MAX_MANIFEST_BYTES + 1 - len(raw)))
    value['directories'] = [[f'{n:04}' + '\U0001f600' * 98] for n in range(256)]
    with pytest.raises(ValueError):
        validate_manifest(value)


@pytest.mark.parametrize('raw', [
    b'{"version":1,"version":1,"root":"r","directories":[],"files":[]}',
    b'{"version":1,"root":"r","directories":[],"files":[{"path":["a"],"size":0,"size":0,"sha256":"' + DIGEST.encode() + b'"}]}',
    b'{"version":1,"root":"r","directories":[],"files":[],"extra":NaN}',
    b'{"version":1,"root":"r","directories":[],"files":[],"extra":Infinity}',
    b'[]', b'null', b'{} trailing', b'\xff', b'\xef\xbb\xbf{}',
    b'[' * 1500 + b']' * 1500, b'{', b'',
])
def test_parser_rejects_ambiguous_or_invalid_wire_json(raw):
    with pytest.raises(ValueError):
        parse_manifest(raw)


@pytest.mark.parametrize('raw', ['{}', bytearray(b'{}'), memoryview(b'{}'), None])
def test_parser_requires_bytes(raw):
    with pytest.raises(ValueError):
        parse_manifest(raw)


def test_parser_and_direct_validator_have_identical_canonical_output():
    value = manifest(['\u4e2d\u6587', '\U0001f680.txt'])
    assert parse_manifest(json.dumps(value, ensure_ascii=False).encode()) == validate_manifest(value)
    assert parse_manifest(json.dumps(value, ensure_ascii=True).encode()) == validate_manifest(value)


def test_digest_does_not_hash_an_unvalidated_object():
    value = manifest()
    value['files'][0]['path'] = ['..']
    with pytest.raises(ValueError):
        manifest_digest(value)


def test_cyclic_or_arbitrary_objects_cannot_trigger_serializers():
    value = manifest()
    value['directories'].append(value)
    with pytest.raises(ValueError):
        validate_manifest(value)
    class DictSubclass(dict):
        def items(self):
            raise AssertionError('Custom serializer must not run')
    with pytest.raises(ValueError):
        validate_manifest(DictSubclass(manifest()))


def test_json_key_subclasses_are_not_accepted():
    class Key(str):
        pass
    value = manifest()
    value[Key('root')] = value.pop('root')
    with pytest.raises(ValueError):
        validate_manifest(value)
    value = manifest()
    value['files'][0][Key('size')] = value['files'][0].pop('size')
    with pytest.raises(ValueError):
        validate_manifest(value)


def test_exact_single_file_and_aggregate_boundaries():
    value = manifest(size=512 * 1024 * 1024)
    value['files'].append({'path': ['other.bin'], 'size': 512 * 1024 * 1024, 'sha256': DIGEST})
    assert sum(entry['size'] for entry in validate_manifest(value)['files']) == 1024 * 1024 * 1024
    value['files'].append({'path': ['extra'], 'size': 1, 'sha256': DIGEST})
    with pytest.raises(ValueError):
        validate_manifest(value)


def test_fullmatch_does_not_accept_hash_suffix_or_newline():
    for digest in [DIGEST + '\n', DIGEST + '0', ' ' + DIGEST]:
        value = manifest()
        value['files'][0]['sha256'] = digest
        with pytest.raises(ValueError):
            validate_manifest(value)


def test_digest_changes_for_manifest_content_and_root():
    value = manifest()
    digest = manifest_digest(value)
    value['root'] = 'another root'
    assert manifest_digest(value) != digest
    value['root'] = 'Owned folder'
    value['files'][0]['size'] = 1
    assert manifest_digest(value) != digest
