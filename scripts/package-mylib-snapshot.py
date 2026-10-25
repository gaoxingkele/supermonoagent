"""Export and verify a version-controlled mylib snapshot without running its code."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from supermonoagent.mylib_catalog import (CatalogError, CatalogStore, build_snapshot,
                                         publish_snapshot, verify_snapshot)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build")
    build.add_argument("repo")
    build.add_argument("destination")
    build.add_argument("--revision")
    build.add_argument("--submodule-map", help="Operator-private JSON map of Gitlink paths to repository roots")
    verify = commands.add_parser("verify")
    verify.add_argument("snapshot")
    publish = commands.add_parser("publish")
    publish.add_argument("store")
    publish.add_argument("snapshot")
    publish.add_argument("--expected-revision", help="Active commit, or 'none' for an empty store")
    activate = commands.add_parser("activate")
    activate.add_argument("store")
    activate.add_argument("revision")
    activate.add_argument("--expected-revision")
    args = parser.parse_args()
    try:
        if args.command == "build":
            mappings = None
            if args.submodule_map:
                raw = Path(args.submodule_map).read_bytes()
                if len(raw) > 1024 * 1024:
                    raise CatalogError("Submodule map exceeds its limit")
                mappings = json.loads(raw)
                if not isinstance(mappings, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in mappings.items()):
                    raise CatalogError("Submodule map is invalid")
            result = build_snapshot(args.repo, args.destination, args.revision, submodule_repos=mappings)
            result = {"revision": result["revision"], "file_count": len(result["files"]),
                      "skill_count": len(result["skills"]), "excluded_count": len(result["excluded"]),
                      "total_bytes": result["total_bytes"], "sources": result["sources"]}
        elif args.command == "verify":
            value = verify_snapshot(args.snapshot)
            result = {"revision": value["revision"], "file_count": len(value["files"]),
                      "skill_count": len(value["skills"]), "total_bytes": value["total_bytes"]}
        else:
            options = {} if args.expected_revision is None else {"expected_revision": None if args.expected_revision == "none" else args.expected_revision}
            result = (publish_snapshot(args.store, args.snapshot, **options) if args.command == "publish"
                      else CatalogStore(args.store).activate(args.revision, **options))
        print(json.dumps({"ok": True, **result}, ensure_ascii=False))
        return 0
    except CatalogError as error:
        print(json.dumps({"ok": False, "error": str(error), "code": error.code, "details": error.details}))
        return 1
    except (OSError, ValueError):
        print(json.dumps({"ok": False, "error": "Snapshot operation failed"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
