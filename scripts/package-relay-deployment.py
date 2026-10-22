"""Package relay source/config templates without local accounts, keys or test certificates."""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile


def package(root, destination):
    files = set()
    for directory in ('src/supermonoagent', 'deploy/relay'):
        for path in (root / directory).rglob('*'):
            if path.is_file() and '__pycache__' not in path.parts:
                if directory == 'src/supermonoagent' and path.suffix not in {'.py', '.js', '.css', '.json', '.ico', '.png'}:
                    continue
                files.add(path)
    for name in ('pyproject.toml', 'scripts/check-relay-tls.py',
                 'scripts/package-ui-update.py', 'scripts/publish-ui-update.py',
                 'scripts/ui_update_release.py', 'docs/ui-hot-updates.md',
                 'docs/relay-account-setup.md', 'docs/protocol.md', 'docs/architecture.md'):
        files.add(root / name)
    if (root / 'LICENSE').is_file():
        files.add(root / 'LICENSE')
    hashes = {}
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(files):
            name = path.relative_to(root).as_posix()
            data = path.read_bytes()
            hashes[name] = hashlib.sha256(data).hexdigest()
            archive.writestr(name, data)
        archive.writestr('SOURCE-MANIFEST.json', json.dumps(hashes, indent=2))
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    destination.with_suffix(destination.suffix + '.sha256').write_text(digest + '  ' + destination.name + '\n', encoding='ascii')
    with zipfile.ZipFile(destination) as archive:
        for name, expected in hashes.items():
            assert hashlib.sha256(archive.read(name)).hexdigest() == expected
    return {'archive': str(destination), 'source_files': len(hashes), 'sha256': digest}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('dist/relay-account-deployment.zip'))
    args = parser.parse_args()
    print(json.dumps(package(Path(__file__).resolve().parents[1], args.output.resolve())))
