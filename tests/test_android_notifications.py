"""Run the APK's actual metadata WebSocket transport against adversarial peers."""
import base64
import hashlib
import pathlib
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def exact(stream, size):
    result = b""
    while len(result) < size:
        part = stream.read(size - len(result))
        if not part:
            raise EOFError()
        result += part
    return result


def masked(stream):
    first, second = exact(stream, 2)
    assert second & 128, "client must mask every frame"
    length = second & 127
    if length == 126:
        length = struct.unpack("!H", exact(stream, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", exact(stream, 8))[0]
    mask = exact(stream, 4)
    body = exact(stream, length)
    return first & 15, bytes(value ^ mask[n % 4] for n, value in enumerate(body))


class AndroidNotificationsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        javac = shutil.which("javac") or next((str(p) for p in (ROOT / ".omx/tools/jdk").glob("*/bin/javac.exe")), None)
        if not javac:
            raise unittest.SkipTest("JDK unavailable")
        cls.java = str(pathlib.Path(javac).with_name("java.exe" if javac.endswith(".exe") else "java"))
        cls.temp = tempfile.TemporaryDirectory()
        runner = pathlib.Path(cls.temp.name) / "EventCheck.java"
        runner.write_text('''
import dev.supermonoagent.mobile.EventSocket;
public class EventCheck {
 public static void main(String[] args) throws Exception {
  try (EventSocket socket = new EventSocket(args[0])) {
   if (args.length > 1 && args[1].equals("idle")) {
    java.lang.reflect.Field field = EventSocket.class.getDeclaredField("socket"); field.setAccessible(true);
    ((java.net.Socket) field.get(socket)).setSoTimeout(100);
   }
   socket.protocol(18, "{\\"replay\\":false}");
   EventSocket.Message result = socket.receive();
   System.out.print(result.opcode + ":" + new String(result.body, "UTF-8"));
  } catch (EventSocket.Closed closed) { System.out.print("closed:" + closed.code); }
  catch (java.io.IOException rejected) { System.out.print("rejected"); }
 }
}''', encoding="utf8")
        package = ROOT / "android/src/dev/supermonoagent/mobile"
        subprocess.run([javac, "-encoding", "UTF-8", "-d", cls.temp.name,
                        str(package / "ConnectionEndpoint.java"), str(package / "EventSocket.java"), str(runner)],
                       check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def exercise(self, mode):
        errors, frames, requests = [], [], []
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0)); server.listen(); server.settimeout(10)
            def serve():
                try:
                    connection, _ = server.accept()
                    with connection:
                        connection.settimeout(5)
                        stream = connection.makefile("rb")
                        request = b""
                        while not request.endswith(b"\r\n\r\n"):
                            request += exact(stream, 1)
                        requests.append(request)
                        key = next(line.split(b":", 1)[1].strip() for line in request.split(b"\r\n") if line.lower().startswith(b"sec-websocket-key:"))
                        accept = base64.b64encode(hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest())
                        if mode == "bad-upgrade":
                            accept = b"invalid"
                        connection.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: keep-alive, Upgrade\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\n")
                        if mode == "bad-upgrade":
                            return
                        frames.append(masked(stream))
                        if mode == "fragments":
                            connection.sendall(b"\x02\x02ab\x89\x01p\x80\x02cd")
                            frames.append(masked(stream))
                        elif mode == "idle":
                            frames.append(masked(stream))
                            connection.sendall(b"\x8a\x00\x82\x02ok")
                        elif mode == "large":
                            connection.sendall(b"\x82\x7f" + struct.pack("!Q", 256 * 1024 + 1))
                        elif mode == "masked-server":
                            connection.sendall(b"\x82\x80")
                        elif mode == "continuation":
                            connection.sendall(b"\x80\x00")
                        elif mode == "auth":
                            connection.sendall(b"\x88\x02" + struct.pack("!H", 4003))
                except Exception as error:
                    errors.append(error)
            thread = threading.Thread(target=serve); thread.start()
            result = subprocess.run([self.java, "-cp", self.temp.name, "EventCheck", f"http://127.0.0.1:{server.getsockname()[1]}/relay", mode],
                                    check=True, capture_output=True, timeout=15)
            thread.join(7)
            self.assertFalse(thread.is_alive()); self.assertFalse(errors, errors)
        self.assertIn(b"GET /relay/ws HTTP/1.1", requests[0])
        self.assertNotIn(b"token", requests[0])
        if frames:
            self.assertEqual(frames[0], (2, struct.pack("<I", 17) + b'\x12{"replay":false}'))
        return result.stdout.decode(), frames

    def test_fragmentation_and_interleaved_ping(self):
        result, frames = self.exercise("fragments")
        self.assertEqual(result, "2:abcd")
        self.assertEqual(frames[1], (10, b"p"))

    def test_authentication_close_is_distinct(self):
        self.assertEqual(self.exercise("auth")[0], "closed:4003")

    def test_idle_connection_uses_ping_instead_of_reconnecting(self):
        result, frames = self.exercise("idle")
        self.assertEqual(result, "2:ok")
        self.assertEqual(frames[1], (9, b""))

    def test_rejects_invalid_frames_and_upgrade(self):
        for mode in ("bad-upgrade", "large", "masked-server", "continuation"):
            with self.subTest(mode=mode):
                self.assertEqual(self.exercise(mode)[0], "rejected")
