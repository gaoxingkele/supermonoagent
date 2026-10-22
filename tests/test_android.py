"""Run the actual dependency-free Java endpoint validator, without Android stubs."""
import pathlib
import shutil
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET
import importlib.util
import io
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[1]


class AndroidEndpointTest(unittest.TestCase):
    def test_apk_dex_append_preserves_readable_asset_headers(self):
        spec = importlib.util.spec_from_file_location("append_apk_dex", ROOT / "scripts/append-apk-dex.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temporary:
            apk = pathlib.Path(temporary) / "unsigned.apk"
            dex = pathlib.Path(temporary) / "classes.dex"
            dex.write_bytes(b"test-dex")
            with zipfile.ZipFile(apk, "w") as archive:
                archive.writestr("assets/mobile/index.html", "<h1>mobile</h1>", compress_type=zipfile.ZIP_DEFLATED)
            before = apk.read_bytes()
            with zipfile.ZipFile(io.BytesIO(before)) as archive:
                asset_end = archive.start_dir
            module.append_dex(apk, dex)
            self.assertEqual(apk.read_bytes()[:asset_end], before[:asset_end])
            with zipfile.ZipFile(apk) as archive:
                self.assertEqual(archive.read("assets/mobile/index.html"), b"<h1>mobile</h1>")
                self.assertEqual(archive.read("classes.dex"), b"test-dex")
            with self.assertRaises(ValueError):
                module.append_dex(apk, dex)

    def test_resizable_activity_preserves_fold_configuration(self):
        manifest = ET.parse(ROOT / "android/AndroidManifest.xml")
        activity = manifest.find("application/activity")
        android = "{http://schemas.android.com/apk/res/android}"
        self.assertEqual(activity.get(android + "resizeableActivity"), "true")
        self.assertIsNone(activity.get(android + "screenOrientation"))
        changes = set(activity.get(android + "configChanges").split("|"))
        self.assertTrue({"orientation", "screenSize", "smallestScreenSize", "screenLayout", "density"} <= changes)

    def test_native_host_security_and_lifecycle_contract(self):
        activity = (ROOT / "android/src/dev/supermonoagent/mobile/MainActivity.java").read_text(encoding="utf-8")
        for expected in ("setTextZoom(100)", "setSupportZoom(false)", "setAllowFileAccess(false)",
                         "setAllowContentAccess(false)", "MIXED_CONTENT_NEVER_ALLOW", "WindowInsets.Type.ime()",
                         "setDecorFitsSystemWindows(false)", 'lifecycle("pause")', 'lifecycle("resume")',
                         "onRenderProcessGone", "Uri.encode(pageConnection.toString())", "UpdateDocument.asset(base,request.getUrl().toString(),revision)",
                         "request.isForMainFrame()", "UpdateDocument.action(base,web.getUrl()", "generation==documentGeneration"):
            self.assertIn(expected, activity)
        for unsafe in ("addJavascriptInterface(", ".proceed()", 'putString("token"', "setAllowUniversalAccessFromFileURLs(true)"):
            self.assertNotIn(unsafe, activity)
        script = (ROOT / "scripts/build-android.ps1").read_text(encoding="utf-8")
        self.assertIn("'-A', $stagedAssets", script)
        self.assertIn("packaged-assets.json", script)
        self.assertIn("append-apk-dex.py", script)
        self.assertIn("$currentVersion.Build + 1", script)
        self.assertIn("getPackageInfo(getPackageName(), 0)", activity)
        self.assertIn("ConnectionProbe.test(endpoint, relayCode, accessToken)", activity)
        self.assertIn('getAssets().open("CHANGELOG.txt")', activity)

    def test_packaged_asset_policy(self):
        javac = shutil.which("javac") or next(
            (str(path) for path in (ROOT / ".omx/tools/jdk").glob("*/bin/javac.exe")), None
        )
        if not javac:
            self.skipTest("JDK unavailable")
        java = str(pathlib.Path(javac).with_name("java.exe" if javac.endswith(".exe") else "java"))
        harness = r'''
import dev.supermonoagent.mobile.LocalAssets;
public class AssetCheck {
 public static void main(String[] args) {
  String base = "https://relay.example.com";
  if (!"http://host".equals(LocalAssets.originForEndpoint("HTTP://HOST:80/path/"))) throw new AssertionError();
  if (!"https://host".equals(LocalAssets.originForEndpoint("https://HOST:443/path"))) throw new AssertionError();
  if (!"https://host:8443".equals(LocalAssets.originForEndpoint("https://HOST:8443"))) throw new AssertionError();
  if (!"http://[::1]:9123".equals(LocalAssets.originForEndpoint("http://[::1]:9123/path"))) throw new AssertionError();
  if (!"http://[::1]".equals(LocalAssets.originForEndpoint("http://[::1]:80"))) throw new AssertionError();
  if (!"mobile/index.html".equals(LocalAssets.assetPath(base, base + "/mobile/index.html#secret"))) throw new AssertionError();
  if (!"mobile/vendor/xterm.js".equals(LocalAssets.assetPath(base, base + "/mobile/vendor/xterm.js"))) throw new AssertionError();
  if (!"mobile/index.html".equals(LocalAssets.assetPath("http://127.0.0.1:9123", "http://127.0.0.1:9123/mobile/index.html"))) throw new AssertionError();
  if (!"mobile/index.html".equals(LocalAssets.assetPath("http://192.168.1.20:9000", "http://192.168.1.20:9000/mobile/index.html"))) throw new AssertionError();
  if (!"mobile/index.html".equals(LocalAssets.assetPath("http://[::1]:9123", "http://[::1]:9123/mobile/index.html"))) throw new AssertionError();
  if (LocalAssets.assetPath("http://127.0.0.1:9123", "http://127.0.0.1:9124/mobile/index.html") != null) throw new AssertionError();
  if (LocalAssets.assetPath("https://user@relay.example.com", "https://user@relay.example.com/mobile/index.html") != null) throw new AssertionError();
  if (LocalAssets.assetPath(base + "/prefix", base + "/mobile/index.html") != null) throw new AssertionError();
  String[] rejected = {"https://evil.test/mobile/index.html", "http://relay.example.com/mobile/index.html", "https://user@relay.example.com/mobile/index.html",
    base+":443/mobile/index.html", base+"/mobile/../secret", base+"/mobile/%2e%2e/secret",
    base+"/mobile/%252e%252e/secret", base+"/mobile/vendor/../../secret", base+"/mobile/vendor%2fxterm.js",
    base+"/mobile/other.js", base+"/mobile/index.html?secret=1", base+"/secret", "file:///mobile/index.html"};
  for (String url : rejected) if (LocalAssets.assetPath(base, url) != null) throw new AssertionError(url);
  if (!LocalAssets.isDocument(base, base+"/mobile/index.html#config") || LocalAssets.isDocument(base, base+"/mobile/app.js")) throw new AssertionError();
  if (!"text/javascript".equals(LocalAssets.mime("mobile/vendor/xterm.js"))) throw new AssertionError();
 }
}
'''
        with tempfile.TemporaryDirectory() as temporary:
            source = pathlib.Path(temporary) / "AssetCheck.java"
            source.write_text(harness, encoding="utf-8")
            policy = ROOT / "android/src/dev/supermonoagent/mobile/LocalAssets.java"
            endpoint = ROOT / "android/src/dev/supermonoagent/mobile/ConnectionEndpoint.java"
            subprocess.run([javac, "-encoding", "UTF-8", "-d", temporary, str(policy), str(endpoint), str(source)], check=True, capture_output=True)
            subprocess.run([java, "-cp", temporary, "AssetCheck"], check=True, capture_output=True)

    def test_server_url_boundary(self):
        javac = shutil.which("javac") or next(
            (str(path) for path in (ROOT / ".omx/tools/jdk").glob("*/bin/javac.exe")), None
        )
        if not javac:
            self.skipTest("Portable JDK not bootstrapped; run scripts/build-android.ps1 -Bootstrap")
        java = str(pathlib.Path(javac).with_name("java.exe" if javac.endswith(".exe") else "java"))
        harness = r'''
import dev.supermonoagent.mobile.ConnectionEndpoint;
public class EndpointCheck {
    public static void main(String[] args) {
        String[] accepted = {"http://localhost:9123", "https://relay.example.com/agent/", "http://[::1]:9123"};
        String[] rejected = {"http://example.com", "http://192.168.1.8:8817", "https://@example.com", "", "javascript:alert(1)", "file:///tmp/test", "https://user:secret@example.com",
            "https://example.com?token=secret", "https://example.com/#fragment", "http://example.com:0",
            "http://example.com:65536", "http://example.com:invalid", "http://example.com\\@evil.example",
            "https:///workspace", "//example.com", "https://example.com/%xx"};
        for (String url : accepted) ConnectionEndpoint.normalize(url);
        for (String url : rejected) {
            boolean blocked = false;
            try { ConnectionEndpoint.normalize(url); } catch (IllegalArgumentException expected) { blocked = true; }
            if (!blocked) throw new AssertionError("Accepted unsafe endpoint: " + url);
        }
    }
}
'''
        with tempfile.TemporaryDirectory() as temporary:
            directory = pathlib.Path(temporary)
            source = directory / "EndpointCheck.java"
            source.write_text(harness, encoding="utf-8")
            validator = ROOT / "android/src/dev/supermonoagent/mobile/ConnectionEndpoint.java"
            subprocess.run([javac, "-encoding", "UTF-8", "-d", temporary, str(validator), str(source)], check=True, capture_output=True)
            subprocess.run([java, "-cp", temporary, "EndpointCheck"], check=True, capture_output=True)


if __name__ == "__main__":
    unittest.main()
