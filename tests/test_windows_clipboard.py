import base64
import hashlib
from pathlib import Path
import struct
import zlib

import pytest

from supermonoagent.windows_clipboard import ClipboardFiles, dib_to_png


def dib(width=2, height=1, bits=24, compression=0, pixels=None):
    stride = ((width * bits + 31) // 32) * 4
    if pixels is None:
        pixels = bytes(stride * abs(height))
    return struct.pack('<IiiHHIIiiII', 40, width, height, 1, bits, compression, len(pixels), 0, 0, 0, 0) + pixels


def png_pixels(data):
    assert data[:8] == b'\x89PNG\r\n\x1a\n'
    index, compressed = 8, bytearray()
    while index < len(data):
        size = struct.unpack_from('>I', data, index)[0]
        kind = data[index + 4:index + 8]
        payload = data[index + 8:index + 8 + size]
        if kind == b'IDAT':
            compressed.extend(payload)
        index += 12 + size
    return zlib.decompress(compressed)


def test_24bit_dib_swaps_bgr_and_handles_row_padding():
    png = dib_to_png(dib(pixels=b'\x00\x00\xff\x00\xff\x00\x00\x00'))
    assert png_pixels(png) == b'\x00\xff\x00\x00\x00\xff\x00'


def test_bottom_up_and_top_down_32bit_are_opaque_rgb():
    bottom = b'\xff\x00\x00\x00'  # Blue; reserved high byte is not alpha.
    top = b'\x00\x00\xff\x00'
    assert png_pixels(dib_to_png(dib(1, 2, 32, pixels=bottom + top))) == b'\0\xff\0\0\0\0\0\xff'
    assert png_pixels(dib_to_png(dib(1, -2, 32, pixels=top + bottom))) == b'\0\xff\0\0\0\0\0\xff'


def masked_dib(header=108, height=-2, compression=3, alpha=0xFF000000,
               masks=(0x00FF0000, 0x0000FF00, 0x000000FF), pixels=None):
    if pixels is None:
        pixels = b'\x00\x00\xff\x80\xff\x00\x00\x40'  # Red 50%, blue 25%.
    content = bytearray(dib(1, height, 32, compression, pixels=pixels)[:40])
    struct.pack_into('<I', content, 0, header)
    if header == 40:
        content.extend(struct.pack('<III', *masks))
    else:
        content.extend(bytes(header - 40))
        struct.pack_into('<IIII', content, 40, *masks, alpha)
        struct.pack_into('<I', content, 56, 0x73524742)  # LCS_sRGB
    return bytes(content) + pixels


@pytest.mark.parametrize('header', [108, 124], ids=['v4', 'v5'])
@pytest.mark.parametrize('compression', [0, 3], ids=['rgb', 'bitfields'])
@pytest.mark.parametrize('height', [-2, 2], ids=['top-down', 'bottom-up'])
def test_v4_v5_standard_bgra_preserves_alpha(header, compression, height):
    pixels = b'\x00\x00\xff\x80\xff\x00\x00\x40'
    if height > 0:
        pixels = pixels[4:] + pixels[:4]
    png = dib_to_png(masked_dib(header, height, compression, pixels=pixels))
    assert png[25] == 6  # PNG RGBA color type.
    assert png_pixels(png) == b'\0\xff\0\0\x80\0\0\0\xff\x40'


@pytest.mark.parametrize('header', [40, 108, 124])
def test_standard_masks_without_alpha_are_opaque_rgb(header):
    png = dib_to_png(masked_dib(header=header, alpha=0))
    assert png[25] == 2
    assert png_pixels(png) == b'\0\xff\0\0\0\0\0\xff'


@pytest.mark.parametrize('header', [108, 124])
def test_v4_v5_implicit_bi_rgb_masks_are_opaque(header):
    png = dib_to_png(masked_dib(header, compression=0, alpha=0, masks=(0, 0, 0)))
    assert png[25] == 2


@pytest.mark.parametrize('data', [masked_dib(40)[:50], masked_dib(108)[:105],
    masked_dib(124)[:120], masked_dib(108, masks=(0xFF, 0xFF00, 0xFF0000)),
    masked_dib(124, alpha=0xF0000000), masked_dib(108, compression=3, masks=(0, 0, 0)),
    struct.pack('<I', 56) + bytes(60)],
    ids=['truncated-external-masks', 'truncated-v4', 'truncated-v5', 'nonstandard-rgb',
         'nonstandard-alpha', 'missing-bitfield-masks', 'unknown-header'])
def test_unsupported_or_truncated_masked_dib_rejected(data):
    with pytest.raises(ValueError):
        dib_to_png(data)


@pytest.mark.parametrize('height', [-2, 2])
def test_external_masks_obey_bitmap_orientation(height):
    pixels = b'\x00\x00\xff\x00\xff\x00\x00\x00'
    if height > 0:
        pixels = pixels[4:] + pixels[:4]
    assert png_pixels(dib_to_png(masked_dib(40, height, pixels=pixels))) == b'\0\xff\0\0\0\0\0\xff'


@pytest.mark.parametrize('offset,value', [(56, 0x4D424544), (112, 124), (116, 100)],
                         ids=['embedded-profile', 'profile-offset', 'profile-size'])
def test_color_profiles_requiring_conversion_are_rejected(offset, value):
    data = bytearray(masked_dib(124))
    struct.pack_into('<I', data, offset, value)
    with pytest.raises(ValueError, match='color profile'):
        dib_to_png(bytes(data))


def test_image_pixel_cap_applies_to_v5():
    data = bytearray(masked_dib(124))
    struct.pack_into('<ii', data, 4, 100000, 100000)
    with pytest.raises(ValueError):
        dib_to_png(bytes(data))


@pytest.mark.parametrize('data', [b'', dib(bits=16), dib(compression=3), dib()[:-1],
    dib(width=0), dib(height=0), struct.pack('<I', 124) + bytes(120)],
    ids=['empty', 'depth', 'bitfields', 'truncated', 'zero-width', 'zero-height', 'v5'])
def test_unsupported_dib_rejected(data):
    with pytest.raises(ValueError):
        dib_to_png(data)


def test_file_snapshot_hash_chunks_and_handles_release(tmp_path):
    source = tmp_path / 'office.pdf'
    payload = b'binary\0' * 20000
    source.write_bytes(payload)
    clipboard = ClipboardFiles(getter=lambda: [source])
    snapshot = clipboard.snapshot()
    assert snapshot['kind'] == 'files'
    item = snapshot['items'][0]
    assert item['name'] == 'office.pdf'
    assert item['size'] == len(payload) and item['sha256'] == hashlib.sha256(payload).hexdigest()
    assert 'path' not in item
    first = clipboard.read(item['id'], 0)
    second = clipboard.read(item['id'], first['next'])
    assert base64.b64decode(first['data']) + base64.b64decode(second['data']) == payload
    assert first['eof'] is False and second['eof'] is True
    stream = clipboard._items[item['id']].stream
    clipboard.release(item['id'])
    assert stream.closed
    with pytest.raises(ValueError):
        clipboard.read(item['id'], 0)


def test_snapshot_preserves_published_ids_until_release_or_close(tmp_path):
    source = tmp_path / 'word.docx'
    source.write_bytes(b'word')
    clipboard = ClipboardFiles(getter=lambda: [source])
    first = clipboard.snapshot()['items'][0]['id']
    stream = clipboard._items[first].stream
    second = clipboard.snapshot()['items'][0]['id']
    assert first != second and not stream.closed
    assert base64.b64decode(clipboard.read(first, 0)['data']) == b'word'
    second_stream = clipboard._items[second].stream
    clipboard.close()
    assert stream.closed and second_stream.closed
    with pytest.raises(ValueError):
        clipboard.read(second, 0)


@pytest.mark.parametrize('budget', ['count', 'bytes', 'invalid_batch', 'cancelled'])
def test_failed_snapshot_only_rolls_back_new_handles(tmp_path, budget):
    source = tmp_path/'source.pdf'
    source.write_bytes(b'owned')
    clipboard = ClipboardFiles(getter=lambda: [source], max_sources=2, max_total_size=10)
    first = clipboard.snapshot()['items'][0]['id']
    original = clipboard._items[first].stream
    clipboard._getter = lambda: [source, source]
    if budget == 'bytes':
        clipboard.max_sources = 10
        clipboard.max_total_size = 12
    elif budget == 'invalid_batch':
        clipboard._getter = lambda: [source, tmp_path]
    elif budget == 'cancelled':
        checks = []
        def cancelled():
            checks.append(1)
            return len(checks) >= 7
    with pytest.raises(ValueError):
        clipboard.snapshot(**({'cancelled': cancelled} if budget == 'cancelled' else {}))
    assert set(clipboard._items) == {first}
    assert not original.closed
    assert base64.b64decode(clipboard.read(first, 0)['data']) == b'owned'
    clipboard.release(first)
    assert original.closed and not clipboard._items
    clipboard._getter = lambda: [source]
    assert len(clipboard.snapshot()['items']) == 1
    clipboard.close()


def test_default_source_budget_retains_all_128_and_release_restores_capacity(tmp_path):
    source = tmp_path/'tiny.pdf'
    source.write_bytes(b'x')
    clipboard = ClipboardFiles(getter=lambda: [source]*32)
    assert clipboard.max_sources == 128 and clipboard.max_total_size == 1024**3
    published = [clipboard.snapshot() for _ in range(4)]
    with pytest.raises(ValueError, match='budget'):
        clipboard.snapshot()
    assert len(clipboard._items) == 128
    assert base64.b64decode(clipboard.read(published[0]['items'][0]['id'], 0)['data']) == b'x'
    for item in published[0]['items']:
        clipboard.release(item['id'])
    assert len(clipboard.snapshot()['items']) == 32
    streams = [item.stream for item in clipboard._items.values()]
    clipboard.close()
    assert all(stream.closed for stream in streams) and not clipboard._items


def test_next_getter_captures_b_before_a_hash_lock_releases(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import threading
    import supermonoagent.windows_clipboard as module
    paths = []
    for name in ['a', 'b', 'c']:
        path = tmp_path/name
        path.write_bytes(name.encode())
        paths.append(path)
    current = [paths[0]]
    hashing_a, release_hash, captured_b = threading.Event(), threading.Event(), threading.Event()
    real_hash = hashlib.sha256
    class GateHash:
        def __init__(self):
            self.inner = real_hash()
        def update(self, data):
            if threading.current_thread().name == 'clipboard-A':
                hashing_a.set()
                assert release_hash.wait(3)
            self.inner.update(data)
        def hexdigest(self):
            return self.inner.hexdigest()
    monkeypatch.setattr(module, 'hashlib', SimpleNamespace(sha256=GateHash))
    def getter():
        value = list(current)
        if threading.current_thread().name == 'clipboard-B':
            captured_b.set()
        return value
    clipboard = ClipboardFiles(getter=getter)
    results, errors = {}, []
    def snapshot(key):
        try:
            results[key] = clipboard.snapshot()['items'][0]
        except BaseException as error:
            errors.append(error)
    first = threading.Thread(target=snapshot, args=('A',), name='clipboard-A')
    second = threading.Thread(target=snapshot, args=('B',), name='clipboard-B')
    try:
        first.start()
        assert hashing_a.wait(2)
        current[:] = [paths[1]]
        second.start()
        # No sleeps: B must capture while A is demonstrably still hashing.
        assert captured_b.wait(1), 'B getter blocked by A long hash lock'
        current[:] = [paths[2]]
    finally:
        release_hash.set()
        first.join(3)
        if second.ident is not None:
            second.join(3)
    try:
        assert not first.is_alive() and not second.is_alive() and not errors
        assert base64.b64decode(clipboard.read(results['A']['id'], 0)['data']) == b'a'
        assert base64.b64decode(clipboard.read(results['B']['id'], 0)['data']) == b'b'
        assert results['B']['sha256'] == real_hash(b'b').hexdigest()
    finally:
        clipboard.close()


def test_pending_image_captures_respect_budget_before_getter(monkeypatch):
    from types import SimpleNamespace
    import threading
    import supermonoagent.windows_clipboard as module
    entered,release=threading.Event(),threading.Event()
    captured=[];results=[];errors=[]
    real_hash=hashlib.sha256
    class Gated:
        def __init__(self):self.inner=real_hash()
        def update(self,data):
            entered.set();assert release.wait(3);self.inner.update(data)
        def hexdigest(self):return self.inner.hexdigest()
    monkeypatch.setattr(module,'hashlib',SimpleNamespace(sha256=Gated))
    def getter():captured.append(16);return ('fixture.png',b'x'*16)
    clipboard=ClipboardFiles(getter=getter,max_file_size=16,max_sources=1,max_total_size=32)
    def snapshot():
        try:results.append(clipboard.snapshot())
        except ValueError as error:errors.append(str(error))
    first=threading.Thread(target=snapshot)
    followers=[threading.Thread(target=snapshot) for _ in range(3)]
    first.start()
    try:
        assert entered.wait(1)
        for thread in followers:thread.start()
        for thread in followers:thread.join(.3)
        assert captured==[16], 'Unregistered images allocated outside budget'
        assert all(not thread.is_alive() for thread in followers)
    finally:
        release.set();first.join(3)
        for thread in followers:
            if thread.ident is not None:thread.join(3)
        clipboard.close()
    assert len(results)==1 and len(errors)==3


@pytest.mark.parametrize('kind',['png-budget','dib-budget','one-bit','compressed'])
def test_native_image_admission_rejects_before_full_copy_or_conversion(monkeypatch, kind):
    from types import SimpleNamespace
    import supermonoagent.windows_clipboard as module
    data = b'owned PNG bytes' if kind=='png-budget' else dib(bits=1 if kind=='one-bit' else 24,compression=4 if kind=='compressed' else 0)
    reads=[];closed=[];unlocked=[];converted=[]
    fake=SimpleNamespace(string_at=lambda pointer,size:reads.append(size) or data[:size])
    user=SimpleNamespace(RegisterClipboardFormatW=lambda _:99,OpenClipboard=lambda _:True,
        CloseClipboard=lambda:closed.append(True) or True,IsClipboardFormatAvailable=lambda n:n==(99 if kind=='png-budget' else 8),GetClipboardData=lambda _:123)
    kernel=SimpleNamespace(GlobalSize=lambda _:len(data),GlobalLock=lambda _:456,GlobalUnlock=lambda _:unlocked.append(True))
    monkeypatch.setattr(module,'_native_apis',lambda:(fake,user,kernel,None))
    monkeypatch.setattr(module,'dib_to_png',lambda data:converted.append(True) or b'unexpected conversion')
    clipboard=ClipboardFiles(max_total_size=4096)
    with pytest.raises(ValueError):clipboard.snapshot()
    assert not converted and closed==[True] and unlocked==[True]
    assert reads==[] if kind=='png-budget' else reads==[min(len(data),124)]
    assert clipboard._items=={} and clipboard._reservations=={}


def test_close_invalidates_pending_getter_without_freeing_its_live_reservation():
    import threading
    entered,release=threading.Event(),threading.Event();errors=[]
    def getter():entered.set();assert release.wait(2);return ('owned.png',b'x'*16)
    clipboard=ClipboardFiles(getter=getter,max_file_size=16,max_total_size=32,max_sources=1)
    def snapshot():
        try:clipboard.snapshot()
        except ValueError as error:errors.append(str(error))
    task=threading.Thread(target=snapshot);task.start()
    try:
        assert entered.wait(1)
        clipboard.close()
        assert len(clipboard._reservations)==1
        with pytest.raises(ValueError,match='budget'):clipboard.snapshot()
    finally:release.set();task.join(3)
    assert errors==['Clipboard snapshot cancelled'] and not clipboard._reservations and not clipboard._items
    clipboard._getter=lambda:('new.png',b'n'*16)
    assert len(clipboard.snapshot()['items'])==1
    clipboard.close()


def test_failed_close_keeps_charge_and_close_attempts_other_handles(tmp_path):
    path=tmp_path/'file.pdf';path.write_bytes(b'owned')
    clipboard=ClipboardFiles(getter=lambda:[path,path],max_total_size=10,max_sources=2)
    items=clipboard.snapshot()['items'];first,second=[clipboard._items[i['id']].stream for i in items]
    class Flaky:
        fail=True
        def close(self):
            if self.fail:raise OSError('owned close failure')
            first.close()
        def __getattr__(self,key):return getattr(first,key)
    flaky=Flaky();clipboard._items[items[0]['id']].stream=flaky
    with pytest.raises(OSError):clipboard.close()
    assert not first.closed and second.closed and len(clipboard._items)==1
    with clipboard._budget_lock:assert clipboard._used_budget()==(1,5)
    with pytest.raises(ValueError,match='budget'):clipboard.snapshot()
    flaky.fail=False;clipboard.close()
    assert first.closed and not clipboard._items and not clipboard._reservations


def test_many_concurrent_images_have_bounded_pending_and_published_bytes(monkeypatch):
    from types import SimpleNamespace
    import threading
    import supermonoagent.windows_clipboard as module
    entered,release=threading.Event(),threading.Event();errors=[];results=[]
    real_hash=hashlib.sha256
    class Gated:
        def __init__(self):self.inner=real_hash()
        def update(self,data):entered.set();assert release.wait(3);self.inner.update(data)
        def hexdigest(self):return self.inner.hexdigest()
    monkeypatch.setattr(module,'hashlib',SimpleNamespace(sha256=Gated))
    tracked={'live':0,'peak':0};track_lock=threading.Lock()
    class Image(bytes):
        def __new__(cls):
            value=super().__new__(cls,bytearray([120]*16))
            with track_lock:
                tracked['live']+=16
                with clipboard._budget_lock:
                    buffers=sum(item.size for item in clipboard._items.values())
                tracked['peak']=max(tracked['peak'],tracked['live']+buffers)
            return value
        def __del__(self):
            with track_lock:tracked['live']-=16
    clipboard=ClipboardFiles(getter=lambda:('owned.png',Image()),max_file_size=16,max_total_size=64)
    def snapshot():
        try:results.append(clipboard.snapshot())
        except ValueError as error:errors.append(str(error))
    first=threading.Thread(target=snapshot);first.start();followers=[]
    try:
        assert entered.wait(1)
        followers=[threading.Thread(target=snapshot) for _ in range(32)]
        for task in followers:task.start()
        # All admission paths run independently of the blocked hash; quotas
        # include the unfinished captured image and registered BytesIO copy.
        for task in followers:task.join(.01)
        with clipboard._budget_lock:
            count,size=clipboard._used_budget()
            assert count<=128 and size<=64
    finally:
        release.set();first.join(3)
        for task in followers:task.join(3)
    assert all(not task.is_alive() for task in [first,*followers])
    assert len(results)<=4 and len(results)+len(errors)==33
    assert not clipboard._reservations
    clipboard.close()
    assert tracked['peak']<=64 and tracked['live']==0


def test_cancelled_hash_returns_both_captured_and_registered_image_reservations(monkeypatch):
    from types import SimpleNamespace
    import threading
    import supermonoagent.windows_clipboard as module
    entered,release,cancelled=threading.Event(),threading.Event(),threading.Event();errors=[]
    real_hash=hashlib.sha256
    class Gated:
        def __init__(self):self.inner=real_hash()
        def update(self,data):entered.set();assert release.wait(2);self.inner.update(data)
        def hexdigest(self):return self.inner.hexdigest()
    monkeypatch.setattr(module,'hashlib',SimpleNamespace(sha256=Gated))
    clipboard=ClipboardFiles(getter=lambda:('owned.png',b'x'*16),max_file_size=16,max_total_size=32)
    def snapshot():
        try:clipboard.snapshot(cancelled=cancelled.is_set)
        except ValueError as error:errors.append(str(error))
    task=threading.Thread(target=snapshot);task.start()
    try:
        assert entered.wait(1)
        with clipboard._budget_lock:assert clipboard._used_budget()==(1,32)
        cancelled.set()
    finally:release.set();task.join(3)
    assert errors==['Clipboard snapshot cancelled'] and not clipboard._items and not clipboard._reservations


def test_default_native_png_admission_transfers_peak_to_retained_stream(monkeypatch):
    from types import SimpleNamespace
    import supermonoagent.windows_clipboard as module
    data=dib_to_png(dib());reads=[]
    fake=SimpleNamespace(string_at=lambda pointer,size:reads.append(size) or data[:size])
    user=SimpleNamespace(RegisterClipboardFormatW=lambda _:99,OpenClipboard=lambda _:True,
        CloseClipboard=lambda:True,IsClipboardFormatAvailable=lambda n:n==99,GetClipboardData=lambda _:123)
    kernel=SimpleNamespace(GlobalSize=lambda _:len(data),GlobalLock=lambda _:456,GlobalUnlock=lambda _:True)
    monkeypatch.setattr(module,'_native_apis',lambda:(fake,user,kernel,None))
    clipboard=ClipboardFiles(max_total_size=1024*1024)
    card=clipboard.snapshot()['items'][0]
    assert card['size']==len(data) and reads==[len(data)]
    assert base64.b64decode(clipboard.read(card['id'],0)['data'])==data
    with clipboard._budget_lock:assert clipboard._used_budget()==(1,len(data))
    assert not clipboard._reservations
    clipboard.close()


@pytest.mark.parametrize('kind',['bad-png','bad-masks'])
def test_failed_native_capture_does_not_retain_image_bytes_in_exception_frames(monkeypatch,kind):
    from types import SimpleNamespace
    import supermonoagent.windows_clipboard as module
    data=(b'Z'*65536 if kind=='bad-png' else masked_dib(108,masks=(0xFF,0xFF00,0xFF0000)).ljust(65536,b'Z'))
    fake=SimpleNamespace(string_at=lambda pointer,size:bytes(bytearray(data[:size])))
    user=SimpleNamespace(RegisterClipboardFormatW=lambda _:99,OpenClipboard=lambda _:True,CloseClipboard=lambda:True,
        IsClipboardFormatAvailable=lambda n:n==(99 if kind=='bad-png' else 8),GetClipboardData=lambda _:123)
    kernel=SimpleNamespace(GlobalSize=lambda _:len(data),GlobalLock=lambda _:456,GlobalUnlock=lambda _:True)
    monkeypatch.setattr(module,'_native_apis',lambda:(fake,user,kernel,None))
    clipboard=ClipboardFiles(max_total_size=1024*1024)
    with pytest.raises(ValueError) as caught:clipboard.snapshot()
    retained=caught.value
    assert retained.__context__ is None and retained.__cause__ is None
    tb=retained.__traceback__
    while tb:
        if tb.tb_frame.f_code.co_filename==module.__file__:
            assert not any(isinstance(value,(bytes,bytearray)) and len(value)>=32768
                           for value in tb.tb_frame.f_locals.values())
        tb=tb.tb_next
    with clipboard._budget_lock:assert clipboard._used_budget()==(0,0)


def test_image_source_and_empty_clipboard():
    clipboard = ClipboardFiles(getter=lambda: ('screenshot.png', b'png fixture'))
    result = clipboard.snapshot()
    assert result['kind'] == 'image'
    assert base64.b64decode(clipboard.read(result['items'][0]['id'], 0)['data']) == b'png fixture'
    clipboard.close()
    assert ClipboardFiles(getter=lambda: []).snapshot() == {'kind': 'empty', 'items': []}


def test_limits_errors_release_previously_opened_files(tmp_path):
    source = tmp_path / 'large.pdf'
    source.write_bytes(b'12345')
    clipboard = ClipboardFiles(getter=lambda: [source], max_file_size=4)
    with pytest.raises(ValueError):
        clipboard.snapshot()
    assert clipboard._items == {}
    with pytest.raises(ValueError):
        ClipboardFiles(getter=lambda: [source] * 33).snapshot()
    with pytest.raises(ValueError):
        ClipboardFiles(getter=lambda: [tmp_path]).snapshot()


def test_bad_offsets_and_modified_files_rejected(tmp_path):
    source = tmp_path / 'mutable.txt'
    source.write_bytes(b'12345')
    clipboard = ClipboardFiles(getter=lambda: [source])
    item = clipboard.snapshot()['items'][0]
    for offset in [-1, True, 6, '0']:
        with pytest.raises(ValueError):
            clipboard.read(item['id'], offset)
    source.write_bytes(b'changed')
    with pytest.raises(ValueError, match='changed'):
        clipboard.read(item['id'], 0)
    clipboard.close()


def test_native_image_clipboard_is_released_before_conversion(monkeypatch):
    from types import SimpleNamespace
    import supermonoagent.windows_clipboard as module
    state = {'closed': False, 'unlocked': False}
    data = dib()
    user = SimpleNamespace(RegisterClipboardFormatW=lambda _: 0,
        OpenClipboard=lambda _: True, CloseClipboard=lambda: state.update(closed=True),
        IsClipboardFormatAvailable=lambda kind: kind == 8,
        GetClipboardData=lambda _: 123)
    kernel = SimpleNamespace(GlobalSize=lambda _: len(data), GlobalLock=lambda _: 456,
        GlobalUnlock=lambda _: state.update(unlocked=True))
    monkeypatch.setattr(module, '_native_apis', lambda: (SimpleNamespace(string_at=lambda *_: data), user, kernel, None))
    def convert(value):
        assert state['closed'] and state['unlocked']
        assert value == data
        return b'converted'
    monkeypatch.setattr(module, 'dib_to_png', convert)
    assert module.clipboard_sources() == ('screenshot.png', b'converted')


def test_native_format_beacon_does_not_open_clipboard(monkeypatch):
    from types import SimpleNamespace
    import supermonoagent.windows_clipboard as module
    user = SimpleNamespace(RegisterClipboardFormatW=lambda _: 49152,
        IsClipboardFormatAvailable=lambda kind: kind == 15)
    monkeypatch.setattr(module, '_native_apis', lambda: (None, user, None, None))
    assert module.has_clipboard_sources()


def test_native_v5_format_is_detected_and_read_without_losing_alpha(monkeypatch):
    from types import SimpleNamespace
    import supermonoagent.windows_clipboard as module
    requested, closed = [], []
    data = masked_dib(124)
    user = SimpleNamespace(RegisterClipboardFormatW=lambda _: 0,
        OpenClipboard=lambda _: True, CloseClipboard=lambda: closed.append(True),
        IsClipboardFormatAvailable=lambda kind: kind in (8, 17),
        GetClipboardData=lambda kind: requested.append(kind) or 123)
    kernel = SimpleNamespace(GlobalSize=lambda _: len(data), GlobalLock=lambda _: 456,
        GlobalUnlock=lambda _: None)
    monkeypatch.setattr(module, '_native_apis', lambda: (SimpleNamespace(string_at=lambda *_: data), user, kernel, None))
    assert module.has_clipboard_sources()
    name, png = module.clipboard_sources()
    assert requested == [17] and closed == [True]
    assert name == 'screenshot.png' and png[25] == 6
    assert png_pixels(png) == b'\0\xff\0\0\x80\0\0\0\xff\x40'


def test_native_file_list_returns_only_clipboard_paths_and_closes(monkeypatch, tmp_path):
    import ctypes
    from types import SimpleNamespace
    import supermonoagent.windows_clipboard as module
    paths = [tmp_path / 'image.png', tmp_path / 'document.docx']
    closed = []
    def query(_handle, index, buffer, _length):
        if index == 0xFFFFFFFF:
            return len(paths)
        value = str(paths[index])
        if buffer is not None:
            buffer.value = value
        return len(value)
    user = SimpleNamespace(RegisterClipboardFormatW=lambda _: 0,
        OpenClipboard=lambda _: True, CloseClipboard=lambda: closed.append(True),
        IsClipboardFormatAvailable=lambda kind: kind == 15,
        GetClipboardData=lambda _: 123)
    monkeypatch.setattr(module, '_native_apis', lambda: (ctypes, user, None, SimpleNamespace(DragQueryFileW=query)))
    assert module.clipboard_sources() == paths
    assert closed == [True]


def test_empty_file_reads_eof_and_user_supplied_strings_are_not_paths(tmp_path):
    source = tmp_path / 'empty.txt'
    source.write_bytes(b'')
    clipboard = ClipboardFiles(getter=lambda: [source])
    item = clipboard.snapshot()['items'][0]
    assert clipboard.read(item['id'], 0) == {'data': '', 'offset': 0, 'next': 0, 'eof': True}
    clipboard.close()
    with pytest.raises(ValueError):
        ClipboardFiles(getter=lambda: [str(source)]).snapshot()


def test_cancelled_snapshot_does_not_call_getter_and_preserves_previous_handles(tmp_path):
    source = tmp_path / 'fixture.pdf'
    source.write_bytes(b'fixture')
    calls = []
    clipboard = ClipboardFiles(getter=lambda: calls.append(True) or [source])
    old = clipboard.snapshot()['items'][0]['id']
    stream = clipboard._items[old].stream
    with pytest.raises(ValueError, match='cancelled'):
        clipboard.snapshot(cancelled=lambda: True)
    assert calls == [True] and not stream.closed and set(clipboard._items) == {old}
    assert base64.b64decode(clipboard.read(old, 0)['data']) == b'fixture'
    clipboard.close()
    assert stream.closed


def test_cancellation_immediately_after_getter_opens_no_sources(tmp_path, monkeypatch):
    source = tmp_path / 'fixture.pdf'
    source.write_bytes(b'fixture')
    stopped = [False]
    def getter():
        stopped[0] = True
        return [source]
    def unexpected_open(*_args, **_kwargs):
        pytest.fail('Cancelled snapshot must not open sources')
    monkeypatch.setattr(Path, 'open', unexpected_open)
    clipboard = ClipboardFiles(getter=getter)
    with pytest.raises(ValueError, match='cancelled'):
        clipboard.snapshot(cancelled=lambda: stopped[0])
    assert clipboard._items == {}


@pytest.mark.parametrize('between_sources', [False, True], ids=['during-hash-chunk', 'between-sources'])
def test_snapshot_cancellation_closes_batch_handles_and_stops_reads(tmp_path, monkeypatch, between_sources):
    from supermonoagent.windows_clipboard import CHUNK_SIZE
    first, second = tmp_path / 'first.pdf', tmp_path / 'second.pdf'
    first.write_bytes(b'first')
    second.write_bytes(b'x' * (CHUNK_SIZE * 3))
    original_open, opened = Path.open, []
    stopped = [False]

    class Tracked:
        def __init__(self, path):
            self.path = path
            self.stream = original_open(path, 'rb')
            self.reads = 0
            self.exhausted = False
        def fileno(self):
            return self.stream.fileno()
        def read(self, size):
            self.reads += 1
            value = self.stream.read(size)
            self.exhausted = not value
            if self.path == second:
                stopped[0] = True
            return value
        def seek(self, offset):
            return self.stream.seek(offset)
        def close(self):
            self.stream.close()

    def open_source(path, *_args, **_kwargs):
        stream = Tracked(path)
        opened.append(stream)
        return stream

    monkeypatch.setattr(Path, 'open', open_source)
    clipboard = ClipboardFiles(getter=lambda: [first, second])
    def cancelled():
        if between_sources:
            return bool(opened and opened[0].exhausted and opened[0].stream.tell() == 0)
        return stopped[0]
    with pytest.raises(ValueError, match='cancelled'):
        clipboard.snapshot(cancelled=cancelled)
    assert clipboard._items == {}
    assert all(item.stream.closed for item in opened)
    if between_sources:
        assert len(opened) == 1
    else:
        assert len(opened) == 2 and opened[1].reads == 1
