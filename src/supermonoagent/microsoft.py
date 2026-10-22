"""Launch the shared-session bridge in Microsoft Intelligent Terminal."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

INSTALL_URL = "https://github.com/microsoft/intelligent-terminal"


def find_terminal(executable: str | None = None, environ=None) -> str | None:
    environ = os.environ if environ is None else environ
    explicit = executable or environ.get("SUPERMONO_WTAI")
    if explicit:
        path = Path(explicit).expanduser()
        if path.is_file():
            return str(path.resolve())
        raise ValueError(f"Intelligent Terminal executable not found: {path}")
    return shutil.which("wtai.exe")


def launch_command(executable: str, bridge_args: list[str], python: str | None = None) -> list[str]:
    if any(";" in arg or arg == "--token" or arg.startswith("--token=") for arg in bridge_args):
        raise ValueError("Use a token file/environment and omit semicolons (terminal command separators)")
    entry = ["native-attach"] if getattr(sys, "frozen", False) else ["-m", "supermonoagent.native_attach"]
    return [executable, "-w", "SuperMonoAgent", "new-tab", "--title", "SuperMonoAgent shared session",
            str(Path(python or sys.executable).absolute()), *entry, *bridge_args]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", help="Explicit wtai.exe path; also SUPERMONO_WTAI")
    parser.add_argument("--diagnose", action="store_true", help="Print machine-readable discovery without launching")
    parser.add_argument("--install", action="store_true", help="Print official installation instructions; changes nothing")
    args, bridge_args = parser.parse_known_args()
    if args.install:
        print(f"Install the signed MSIX from the official Microsoft Intelligent Terminal releases.\n{INSTALL_URL}/releases\nThen enable the wtai.exe app execution alias, or set SUPERMONO_WTAI to its executable.")
        return
    try:
        executable = find_terminal(args.exe)
        if args.diagnose:
            print(json.dumps({"available": bool(executable), "executable": executable, "python": sys.executable,
                              "install_url": INSTALL_URL, "adapter": "supermonoagent.native_attach", "shell_owner": "python-daemon"}))
            raise SystemExit(0 if executable else 1)
        if not executable:
            raise ValueError(f"Microsoft Intelligent Terminal wtai.exe is unavailable. Run --install for official instructions: {INSTALL_URL}")
        # Listing belongs in this console and never needs a new terminal window.
        if "--list" in bridge_args:
            entry = ["native-attach"] if getattr(sys, "frozen", False) else ["-m", "supermonoagent.native_attach"]
            subprocess.run([sys.executable, *entry, *bridge_args], check=True)
            return
        if not any(arg in bridge_args for arg in ("--session", "--shell")) and not any(arg.startswith(("--session=", "--shell=")) for arg in bridge_args):
            bridge_args.extend(["--shell", "powershell"])
        subprocess.run(launch_command(executable, bridge_args), check=True)
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        raise SystemExit(str(exc)) from None


if __name__ == "__main__":
    main()
