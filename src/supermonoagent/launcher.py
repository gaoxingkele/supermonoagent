"""One shared executable for the browser, Orca-style workspace and native clients."""
from __future__ import annotations

import argparse
import importlib
import os
import sys

MODES = {
    "frontend": "supermonoagent.frontend",
    "host": "supermonoagent.daemon",
    "relay": "supermonoagent.relay",
    "browser": "supermonoagent.desktop",
    "workspace": "supermonoagent.desktop",
    "microsoft": "supermonoagent.microsoft",
    "native-attach": "supermonoagent.native_attach",
    "notifications": "supermonoagent.notification_bridge",
    "relay-bridge": "supermonoagent.relay_bridge",
    "console-snapshot": "supermonoagent.console_snapshot",
}


def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description=__doc__, epilog="Use MODE --help for that client's options. No mode opens the workspace.")
    parser.add_argument("mode", choices=list(MODES), nargs="?", default="workspace")
    if args and args[0] in {"-h", "--help"}:
        parser.print_help()
        return
    if args and args[0].startswith("-"):
        mode, remaining = "workspace", args
    else:
        mode = parser.parse_args(args[:1]).mode
        remaining = args[1:] if args else []
    module = importlib.import_module(MODES[mode])
    if mode == "microsoft" and not os.environ.get("SUPERMONO_URL") and not any(
        value in {"--diagnose", "--install", "--help", "-h", "--url"} or value.startswith("--url=")
        for value in remaining
    ):
        desktop = importlib.import_module("supermonoagent.desktop")
        desktop.connection_url(desktop.argument_parser().parse_args([]))
    if mode == "browser":
        module.main(remaining, browser=True)
    elif mode == "workspace":
        module.main(remaining)
    else:
        original = sys.argv
        try:
            sys.argv = [original[0], *remaining]
            module.main()
        finally:
            sys.argv = original


if __name__ == "__main__":
    main()
