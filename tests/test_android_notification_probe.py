import importlib.util
from pathlib import Path
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import patch


spec = importlib.util.spec_from_file_location("notification_probe", Path(__file__).resolve().parents[1] / "scripts/test-android-notifications.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class NotificationProbeTest(unittest.TestCase):
    def test_only_fixture_package_title_and_result_channel_match(self):
        fixture = "NotifyProbe-1234-locked"
        unrelated = f"  NotificationRecord(secret pkg=other.app)\n channel=terminal-results\n android.title={fixture}\n"
        monitor = f"  NotificationRecord(id=4100 pkg={probe.PACKAGE})\n channel=terminal-monitor\n android.title=SuperMonoAgent\n"
        self.assertIsNone(probe.fixture_notification(unrelated + monitor, fixture))
        target = f"  NotificationRecord(id=42 pkg={probe.PACKAGE})\n channel=terminal-results\n android.title={fixture}\n"
        result = probe.fixture_notification(unrelated + monitor + target, fixture)
        self.assertEqual(result, {"package": probe.PACKAGE, "fixture_title": fixture,
                                 "channel": "terminal-results", "notification_found": True})
        self.assertNotIn("secret", str(result))

    def test_fake_has_no_command_execution_and_controlled_output(self):
        fake = probe.ControlledFakePty()
        with self.assertRaises(AssertionError):
            fake.write(b"command")
        fake.emit()
        self.assertIn(b"state=working", fake.read(4096))
        self.assertIn(b"state=idle", fake.read(4096))
        fake.close()
        with self.assertRaises(probe.PtyClosed):
            fake.read(4096)

    def test_vault_cancel_does_not_touch_save_or_other_apps(self):
        class Device:
            def __init__(self, nodes):
                self.nodes, self.calls = nodes, []

            def native_nodes(self):
                return self.nodes

            def run(self, *args):
                self.calls.append(args)

        save = ET.Element("node", {"package": "com.hihonor.keychain", "text": "Save",
                          "resource-id": "android:id/button1", "bounds": "[0,0][50,50]"})
        other = ET.Element("node", {"package": "other.app", "text": "Cancel",
                           "resource-id": "android:id/button3", "bounds": "[0,0][50,50]"})
        cancel = ET.Element("node", {"package": "com.hihonor.keychain", "text": "Cancel",
                            "resource-id": "android:id/button3", "bounds": "[50,100][150,200]"})
        device = Device([save, other])
        self.assertFalse(probe.dismiss_password_vault(device))
        self.assertEqual(device.calls, [])
        device = Device([save, other, cancel])
        self.assertTrue(probe.dismiss_password_vault(device))
        self.assertEqual(device.calls, [("shell", "input", "tap", "100", "150")])

    def test_late_vault_is_cancelled_between_dialog_and_enable(self):
        def node(package, text, kind="android.widget.Button", **attributes):
            return ET.Element("node", {"package": package, "text": text, "class": kind,
                              "bounds": "[0,0][100,100]", **attributes})

        class Device:
            def __init__(self):
                self.snapshots = iter([
                    [node(probe.PACKAGE, "任务完成通知")],
                    [node("com.hihonor.keychain", "Save", **{"resource-id": "android:id/button1"}),
                     node("com.hihonor.keychain", "Cancel", **{"resource-id": "android:id/button3"})],
                    [node(probe.PACKAGE, "启用通知"),
                     node(probe.PACKAGE, "legacy", "android.widget.CheckBox", checked="false")],
                    [node(probe.PACKAGE, "启用通知"),
                     node(probe.PACKAGE, "legacy", "android.widget.CheckBox", checked="true")]])
                self.actions = []

            def native_nodes(self):
                return next(self.snapshots)

            def native_tap(self, nodes, text):
                self.actions.append(text)

            def run(self, *args):
                self.actions.append(args)

        device = Device()
        with patch.object(probe.time, "sleep"):
            probe.notification_action(device, "启用通知")
        self.assertEqual(device.actions, ["任务完成通知", ("shell", "input", "tap", "50", "50"),
                                         "legacy", "启用通知"])
