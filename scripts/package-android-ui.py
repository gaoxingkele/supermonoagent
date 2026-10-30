"""Stage an immutable, hashed mobile UI bundle for the USB developer update path."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import tempfile

ROOT = Path(__file__).resolve().parents[1]
MOBILE_FILES = ("index.html", "app.js", "app.css", "mobile-core.js")
VENDOR_FILES = ("xterm.js", "xterm.css", "xterm-fit.js", "xterm-unicode11.js", "xterm-webgl.js", "terminal-client.js", "file-client.js", "file-upload.js")
AGENT_MODES = ("codex", "claude-code", "grok", "kimi", "pi-dev", "cursor-cli")
MAX_BYTES = 5 * 1024 * 1024


def package(output_root=None, revision=None):
    sources = {f"mobile/{name}": ROOT / "android/assets/mobile" / name for name in MOBILE_FILES}
    sources.update({f"mobile/vendor/{name}": ROOT / "src/supermonoagent/static" / name for name in VENDOR_FILES})
    sources.update({f"mobile/agent-modes/{name}.json": ROOT / "src/supermonoagent/static/agent-modes" / (name+".json") for name in AGENT_MODES})
    payloads = {path: source.read_bytes() for path, source in sources.items()}
    if sum(map(len, payloads.values())) > MAX_BYTES:
        raise ValueError("Mobile UI bundle exceeds the 5 MiB limit")
    hashes = {path: hashlib.sha256(data).hexdigest() for path, data in payloads.items()}
    fingerprint = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()[:8]
    revision = revision or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ") + "_" + fingerprint
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,96}", revision):
        raise ValueError("Invalid UI revision")
    output_root = Path(output_root) if output_root else ROOT / ".omx/android-ui"
    output_root.mkdir(parents=True, exist_ok=True)
    destination = output_root / revision
    if destination.exists():
        raise FileExistsError("UI revision already exists; use a new immutable revision")
    # Stage under this parent, then rename only after every byte and manifest is complete.
    with tempfile.TemporaryDirectory(prefix=".stage-", dir=output_root) as temporary:
        staged = Path(temporary) / revision
        staged.mkdir()
        for path, data in payloads.items():
            target = staged / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        manifest = "revision=" + revision + "\n" + "".join(f"file.{path}={hashes[path]}\n" for path in sorted(hashes))
        (staged / "manifest.properties").write_text(manifest, encoding="ascii")
        staged.rename(destination)
    return {"revision": revision, "directory": str(destination.resolve()), "bytes": sum(map(len, payloads.values()))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--revision")
    args = parser.parse_args()
    print(json.dumps(package(args.output_root, args.revision)))


if __name__ == "__main__":
    main()
