"""Package an Android-only signed UI feed from the exact assets in a signed APK.

Does not publish files or touch the shared Windows/Android channel.
"""
import argparse
import base64
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def canonical(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), sort_keys=True).encode('utf-8')


def package(apk, output, revision, private_key):
    if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', revision):
        raise ValueError('Use the APK version as the UI revision')
    java = (ROOT / 'android/src/dev/supermonoagent/mobile/RemoteUpdates.java').read_text('utf-8')
    allowlist = re.findall(r'"([^"]+)"', java.split('FILES=')[1].split('));', 1)[0])
    native_api = int(re.search(r'NATIVE_API=(\d+)', java)[1])
    with zipfile.ZipFile(apk) as source:
        files = {name: source.read('assets/' + name) for name in allowlist}
        trust = json.loads(source.read('assets/update-trust.json'))
    if len(trust['keys']) != 1:
        raise ValueError('An explicit signing key selection is required')
    key_id = next(iter(trust['keys']))
    if not private_key.is_file():
        raise ValueError('Existing UI signing key is required')
    openssl = next((str(Path(path)) for path in [shutil.which('openssl'),
                   'D:/Program Files/Git/usr/bin/openssl.exe',
                   'C:/Program Files/Git/usr/bin/openssl.exe'] if path and Path(path).is_file()), None)
    if not openssl:
        raise ValueError('OpenSSL is required')
    output.mkdir(parents=True, exist_ok=True)
    destination = output / 'releases' / revision
    if destination.exists():
        raise ValueError('Immutable release already exists')
    now = int(time.time())
    sequence = time.time_ns() // 1000
    if (output / 'manifest.json').exists():
        old = json.loads(base64.b64decode(json.loads((output / 'manifest.json').read_bytes())['payload']))
        sequence = max(sequence, old['sequence'] + 1)
    with tempfile.TemporaryDirectory(prefix='.android-ui-', dir=output) as temporary:
        stage = Path(temporary)
        archive = stage / 'android.zip'
        with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as bundle:
            for name, data in sorted(files.items()):
                info = zipfile.ZipInfo(name, (2026, 1, 1, 0, 0, 0))
                info.external_attr = 0o100644 << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                bundle.writestr(info, data)
        data = archive.read_bytes()
        artifact = {'url': 'releases/' + revision + '/android.zip', 'native_api': native_api,
                    'size': len(data), 'sha256': hashlib.sha256(data).hexdigest(),
                    'files': {name: {'size': len(value), 'sha256': hashlib.sha256(value).hexdigest()}
                              for name, value in files.items()}}
        payload = canonical({'schema': 1, 'channel': 'stable', 'revision': revision, 'sequence': sequence,
                             'created': now, 'expires': now + 30 * 86400, 'artifacts': {'android': artifact}})
        (stage / 'payload').write_bytes(payload)
        subprocess.run([openssl, 'dgst', '-sha256', '-sign', str(private_key), '-out', str(stage / 'signature'), str(stage / 'payload')], check=True, capture_output=True)
        public = base64.b64decode(trust['keys'][key_id], validate=True)
        (stage / 'public.der').write_bytes(public)
        subprocess.run([openssl, 'pkey', '-pubin', '-inform', 'DER', '-in', str(stage / 'public.der'), '-out', str(stage / 'public.pem')], check=True, capture_output=True)
        subprocess.run([openssl, 'dgst', '-sha256', '-verify', str(stage / 'public.pem'), '-signature', str(stage / 'signature'), str(stage / 'payload')], check=True, capture_output=True)
        envelope = canonical({'schema': 1, 'key_id': key_id, 'payload': base64.b64encode(payload).decode(),
                              'signature': base64.b64encode((stage / 'signature').read_bytes()).decode()})
        (stage / 'manifest.json').write_bytes(envelope)
        destination.mkdir(parents=True)
        shutil.copyfile(archive, destination / 'android.zip')
        shutil.copyfile(stage / 'manifest.json', destination / 'manifest.json')
        shutil.copyfile(stage / 'manifest.json', output / 'manifest.json')
    return {'revision': revision, 'sequence': sequence, 'directory': str(destination), 'android_only': True}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apk', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=ROOT / '.omx/android-ui-releases')
    parser.add_argument('--revision', required=True)
    parser.add_argument('--private-key', type=Path, default=ROOT / '.omx/update-signing/development-private.pem')
    args = parser.parse_args()
    print(json.dumps(package(args.apk, args.output, args.revision, args.private_key)))
