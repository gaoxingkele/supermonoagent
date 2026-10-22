"""Verify the shared launcher asset and Windows Explorer icon resource path."""

import importlib.util
import shutil
import struct
import subprocess
import json
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ICON = ROOT / "src/supermonoagent/static/app-icon.ico"
PNG = ICON.with_suffix(".png")
SIZES = [16, 24, 32, 48, 64, 128, 256]


def icon_frames():
    data = ICON.read_bytes()
    reserved, kind, count = struct.unpack_from("<HHH", data)
    if reserved != 0 or kind != 1:
        raise ValueError("Not a Windows icon")
    entries = []
    for index in range(count):
        width, height, _, _, planes, depth, length, offset = struct.unpack_from("<BBBBHHII", data, 6 + index * 16)
        entries.append((width or 256, height or 256, planes, depth, data[offset:offset + length]))
    return entries


class DesktopIconTest(unittest.TestCase):
    def test_all_taskbar_and_dpi_sizes_have_valid_rgba_frames(self):
        frames = icon_frames()
        self.assertEqual([frame[0] for frame in frames], SIZES)
        for width, height, planes, depth, image in frames:
            self.assertEqual((width, planes, depth), (height, 1, 32))
            self.assertEqual(image[:8], b"\x89PNG\r\n\x1a\n")
            self.assertEqual(struct.unpack_from(">II", image, 16), (width, height))
            self.assertEqual(image[24:26], b"\x08\x06")  # 8-bit RGBA, no palette loss.
        self.assertEqual(frames[-1][-1], PNG.read_bytes())

    def test_build_embeds_icon_and_hashes_packaged_icon_assets(self):
        script = (ROOT / "scripts/build-windows.ps1").read_text(encoding="utf8")
        self.assertIn("'--icon', $appIcon", script)
        self.assertIn("static/app-icon.ico", script)
        self.assertIn("'--add-data', \"$repoRoot/src/supermonoagent/static:supermonoagent/static\"", script)
        self.assertIn("'.ico', '.png'", script)
        self.assertIn('scripts/create-desktop-shortcuts.vbs', script)

    @unittest.skipUnless(sys.platform == "win32" and importlib.util.find_spec("PyInstaller"),
                         "requires Windows packaging environment")
    def test_actual_pe_resource_embedding_preserves_all_seven_frames(self):
        from PyInstaller.utils.win32 import icon

        with tempfile.TemporaryDirectory(prefix="supermono-icon-pe-") as directory:
            executable = Path(directory) / "icon-fixture.exe"
            shutil.copyfile(sys.executable, executable)
            with patch.dict(icon.config.CONF, {"workpath": directory}):
                icon.CopyIcons(str(executable), str(ICON))
            module = icon.win32api.LoadLibraryEx(str(executable), 0, icon.LOAD_LIBRARY_AS_DATAFILE)
            try:
                groups = icon.win32api.EnumResourceNames(module, icon.RT_GROUP_ICON)
                group = icon.win32api.LoadResource(module, icon.RT_GROUP_ICON, groups[0])
                self.assertEqual(struct.unpack_from("<HHH", group), (0, 1, len(SIZES)))
                for index, (width, height, _, _, expected) in enumerate(icon_frames()):
                    entry = struct.unpack_from("<BBBBHHIH", group, 6 + index * 14)
                    self.assertEqual((entry[0] or 256, entry[1] or 256), (width, height))
                    embedded = icon.win32api.LoadResource(module, icon.RT_ICON, entry[-1])
                    self.assertEqual(bytes(embedded), expected)
            finally:
                icon.win32api.FreeLibrary(module)


@unittest.skipUnless(sys.platform == "win32", "requires Windows Script Host")
class DesktopShortcutTest(unittest.TestCase):
    def package(self, root):
        root.mkdir()
        (root / "SuperMonoAgent").mkdir()
        (root / "SuperMonoAgent/SuperMonoAgent.exe").write_bytes(b"MZ-test-fixture-never-executed")
        for name in ("01-Browser.vbs", "02-Orca-Workspace.vbs"):
            (root / name).write_text("WScript.Quit 0\n", encoding="ascii")
        helper = root / "create-desktop-shortcuts.vbs"
        shutil.copyfile(ROOT / "scripts/create-desktop-shortcuts.vbs", helper)
        return helper

    def read_shortcuts(self, destination):
        # Only the explicitly supplied temporary directory is queried; never Desktop.
        code = "$s=New-Object -ComObject WScript.Shell; @(Get-ChildItem -LiteralPath '" + str(destination).replace("'", "''") + "' -Filter '*.lnk' | ForEach-Object {$l=$s.CreateShortcut($_.FullName); @{name=$_.Name; target=$l.TargetPath; arguments=$l.Arguments; icon=$l.IconLocation; cwd=$l.WorkingDirectory; style=$l.WindowStyle}}) | ConvertTo-Json -Compress"
        output = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", code],
                                capture_output=True, check=True, timeout=15)
        # Powershell's console code page can differ; fixture paths here use ASCII.
        return json.loads(output.stdout.decode(errors="replace"))

    def test_helper_creates_correct_icons_and_can_relocate_without_touching_desktop(self):
        with tempfile.TemporaryDirectory(prefix="supermono-links-") as directory:
            base = Path(directory)
            destination = base / "isolated shortcuts"
            destination.mkdir()
            for folder in ("first package", "relocated package"):
                package = base / folder
                helper = self.package(package)
                subprocess.run(["cscript.exe", "//nologo", str(helper), "/destination:" + str(destination)],
                               check=True, capture_output=True, timeout=15)
                shortcuts = self.read_shortcuts(destination)
                self.assertEqual(len(shortcuts), 2)
                for shortcut in shortcuts:
                    launcher = "01-Browser.vbs" if "Browser" in shortcut["name"] else "02-Orca-Workspace.vbs"
                    self.assertTrue(shortcut["target"].lower().endswith("\\system32\\wscript.exe"))
                    self.assertEqual(shortcut["arguments"], '//nologo "' + str(package / launcher) + '"')
                    self.assertEqual(shortcut["icon"], str(package / "SuperMonoAgent/SuperMonoAgent.exe") + ",0")
                    self.assertEqual(shortcut["cwd"], str(package))
                    self.assertEqual(shortcut["style"], 7)

    def test_helper_does_not_overwrite_an_unrelated_same_name_shortcut(self):
        with tempfile.TemporaryDirectory(prefix="supermono-links-") as directory:
            base = Path(directory)
            destination = base / "isolated shortcuts"
            destination.mkdir()
            helper = self.package(base / "package")
            conflict = destination / "SuperMonoAgent Browser.lnk"
            code = "$s=New-Object -ComObject WScript.Shell; $l=$s.CreateShortcut('" + str(conflict).replace("'", "''") + "'); $l.TargetPath='C:\\Windows\\notepad.exe'; $l.Description='Unrelated'; $l.Save()"
            subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", code],
                           capture_output=True, check=True, timeout=15)
            before = conflict.read_bytes()
            result = subprocess.run(["cscript.exe", "//nologo", str(helper), "/destination:" + str(destination)],
                                    capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 3)
            self.assertEqual(conflict.read_bytes(), before)
            self.assertFalse((destination / "SuperMonoAgent Workspace.lnk").exists())


if __name__ == "__main__":
    unittest.main()
