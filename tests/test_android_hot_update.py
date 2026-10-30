"""Validate complete USB UI snapshots using the actual Java loader, without Android stubs."""
import importlib.util
import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("package_android_ui", ROOT / "scripts/package-android-ui.py")
PACKAGER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PACKAGER)


class AndroidHotUpdateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        javac = shutil.which("javac") or next(
            (str(path) for path in (ROOT / ".omx/tools/jdk").glob("*/bin/javac.exe")), None
        )
        if not javac:
            raise unittest.SkipTest("JDK unavailable")
        cls.java = str(pathlib.Path(javac).with_name("java.exe" if javac.endswith(".exe") else "java"))
        cls.classes = tempfile.TemporaryDirectory()
        source = pathlib.Path(cls.classes.name) / "HotCheck.java"
        source.write_text('''
import dev.supermonoagent.mobile.HotAssets;
import java.io.*;
import java.nio.file.*;
import java.util.Arrays;
public class HotCheck {
 public static void main(String[] args) throws Exception {
  HotAssets assets = HotAssets.load(args[0].equals("NULL") ? null : new File(args[0]));
  if (!args[1].equals(assets.getRevision())) throw new AssertionError("Wrong selected revision: " + assets.getRevision());
  if (assets.open("../secret") != null || assets.open("mobile/unknown.js") != null) throw new AssertionError("Unexpected asset");
  InputStream css = assets.open("mobile/app.css");
  if (args[1].isEmpty()) { if (css != null) throw new AssertionError("Invalid update used"); return; }
  if (css == null) throw new AssertionError("Missing verified asset");
  byte[] before = css.readAllBytes(); css.close();
  if (args.length > 2) {
   Files.write(new File(args[0], "ui-updates/" + args[1] + "/mobile/app.css").toPath(), "tampered later".getBytes("UTF-8"));
   try (InputStream after = assets.open("mobile/app.css")) {
    if (!Arrays.equals(before, after.readAllBytes())) throw new AssertionError("Mutable update leaked into snapshot");
   }
  }
 }
}
''', encoding="utf-8")
        package = ROOT / "android/src/dev/supermonoagent/mobile"
        subprocess.run([javac, "-encoding", "UTF-8", "-d", cls.classes.name,
                        str(package / "ConnectionEndpoint.java"), str(package / "LocalAssets.java"),
                        str(package / "HotAssets.java"), str(source)], check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.classes.cleanup()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.external = pathlib.Path(self.temporary.name)
        self.updates = self.external / "ui-updates"
        self.bundle = PACKAGER.package(self.updates, "test_ui_1")
        self.directory = pathlib.Path(self.bundle["directory"])
        self.pointer = self.updates / "active.txt"
        self.pointer.write_text("test_ui_1\n", encoding="ascii")

    def check(self, revision="", snapshot=False, directory=None):
        arguments = [self.java, "-cp", self.classes.name, "HotCheck",
                     str(directory or self.external), revision]
        if snapshot:
            arguments.append("snapshot")
        result = subprocess.run(arguments, capture_output=True, timeout=8)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))

    def test_complete_package_loads_and_keeps_immutable_snapshot(self):
        self.assertEqual(len(list(self.directory.rglob("*.js"))), 9)
        self.assertEqual(len((self.directory / "manifest.properties").read_text().splitlines()), 19)
        self.assertEqual(len(list((self.directory / "mobile/agent-modes").glob("*.json"))), 6)
        for name in PACKAGER.AGENT_MODES:
            packaged = self.directory / "mobile/agent-modes" / (name + ".json")
            shared = ROOT / "src/supermonoagent/static/agent-modes" / (name + ".json")
            self.assertEqual(packaged.read_bytes(), shared.read_bytes())
        self.check("test_ui_1", snapshot=True)
        # A subsequent instance detects the changed bytes and safely uses bundled assets.
        self.check()

    def test_legacy_twelve_file_usb_override_falls_back_without_mixing_apk_profiles(self):
        manifest=self.directory/"manifest.properties"
        legacy="\n".join(line for line in manifest.read_text().splitlines() if not line.startswith("file.mobile/agent-modes/"))+"\n"
        self.assertEqual(len(legacy.splitlines()),13)
        manifest.write_text(legacy,encoding="ascii")
        self.check()
        self.assertEqual(self.pointer.read_text(),"test_ui_1\n")
        self.assertEqual(manifest.read_text(),legacy)

    def test_missing_partial_and_tampered_updates_fall_back(self):
        self.pointer.unlink()
        self.check()
        self.pointer.write_text("test_ui_1", encoding="ascii")
        (self.directory / "mobile/app.js").unlink()
        self.check()

    def test_traversal_and_mismatched_revision_are_rejected(self):
        for pointer in ("../test_ui_1", "/test_ui_1", "test_ui_1/../../", "x" * 129):
            with self.subTest(pointer=pointer):
                self.pointer.write_text(pointer, encoding="ascii")
                self.check()
        self.pointer.write_text("test_ui_1", encoding="ascii")
        manifest = self.directory / "manifest.properties"
        manifest.write_text(manifest.read_text().replace("revision=test_ui_1", "revision=different"), encoding="ascii")
        self.check()

    def test_unknown_files_and_oversized_files_are_rejected(self):
        manifest = self.directory / "manifest.properties"
        original = manifest.read_text()
        manifest.write_text(original.replace("file.mobile/app.css=", "file.mobile/../secret="), encoding="ascii")
        self.check()
        manifest.write_text(original, encoding="ascii")
        (self.directory / "mobile/app.css").write_bytes(b"x" * (PACKAGER.MAX_BYTES + 1))
        self.check()

    def test_symbolic_link_cannot_escape_update_directory(self):
        asset = self.directory / "mobile/app.css"
        outside = self.external / "outside.css"
        outside.write_bytes(asset.read_bytes())
        asset.unlink()
        try:
            asset.symlink_to(outside)
        except OSError:
            self.skipTest("Creating symbolic links is unavailable on this Windows account")
        self.check()

    def test_missing_external_directory_falls_back(self):
        self.check(directory="NULL")

    def test_packager_rejects_unsafe_or_reused_revisions(self):
        for revision in ("../escape", "", "a/b", "x" * 97):
            if not revision:  # Empty means generate a fresh timestamp revision.
                continue
            with self.subTest(revision=revision), self.assertRaises(ValueError):
                PACKAGER.package(self.updates, revision)
        with self.assertRaises(FileExistsError):
            PACKAGER.package(self.updates, "test_ui_1")


if __name__ == "__main__":
    unittest.main()
