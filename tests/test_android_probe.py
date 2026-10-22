"""Exercise the actual Android probe over loopback WebSocket, using a desktop JDK."""
import base64
import hashlib
import json
import pathlib
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
import unittest
from urllib.parse import parse_qs, urlsplit

ROOT = pathlib.Path(__file__).resolve().parents[1]


def read_exact(stream, length):
    result = bytearray()
    while len(result) < length:
        part = stream.read(length - len(result))
        if not part:
            raise EOFError("peer closed")
        result.extend(part)
    return bytes(result)


def receive(stream):
    first, second = read_exact(stream, 2)
    assert first & 0x80 and second & 0x80, "client frame must be final and masked"
    length = second & 127
    if length == 126:
        length = struct.unpack("!H", read_exact(stream, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", read_exact(stream, 8))[0]
    mask = read_exact(stream, 4)
    body = read_exact(stream, length)
    return first & 15, bytes(value ^ mask[index % 4] for index, value in enumerate(body))


def send(connection, opcode, body):
    header = bytes([128 | opcode])
    if len(body) < 126:
        header += bytes([len(body)])
    elif len(body) < 65536:
        header += b"\x7e" + struct.pack("!H", len(body))
    else:
        header += b"\x7f" + struct.pack("!Q", len(body))
    connection.sendall(header + body)


def protocol(opcode, value):
    body = bytes([opcode]) + json.dumps(value).encode()
    return struct.pack("<I", len(body)) + body


class AndroidProbeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        javac = shutil.which("javac") or next(
            (str(path) for path in (ROOT / ".omx/tools/jdk").glob("*/bin/javac.exe")), None
        )
        if not javac:
            raise unittest.SkipTest("JDK unavailable")
        cls.java = str(pathlib.Path(javac).with_name("java.exe" if javac.endswith(".exe") else "java"))
        cls.temporary = tempfile.TemporaryDirectory()
        source = pathlib.Path(cls.temporary.name) / "ProbeCheck.java"
        source.write_text('''
import dev.supermonoagent.mobile.ConnectionProbe;
public class ProbeCheck {
 public static void main(String[] args) throws Exception {
  System.out.write(ConnectionProbe.test(args[0], args[1], args[2]).getBytes("UTF-8"));
 }
}
''', encoding="utf-8")
        package = ROOT / "android/src/dev/supermonoagent/mobile"
        subprocess.run([javac, "-encoding", "UTF-8", "-d", cls.temporary.name,
                        str(package / "ConnectionEndpoint.java"), str(package / "ConnectionProbe.java"), str(source)],
                       check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def run_probe(self, behavior="success", code="", token='private-"\\\n中文'):
        errors = []
        captured = {}
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen()
            server.settimeout(10)
            port = server.getsockname()[1]

            def serve():
                try:
                    with server.accept()[0] as connection:
                        connection.settimeout(8)
                        stream = connection.makefile("rb")
                        captured["path"] = stream.readline().decode().split()[1]
                        headers = {}
                        while True:
                            line = stream.readline()
                            if line == b"\r\n":
                                break
                            key, value = line.decode().split(":", 1)
                            headers[key.lower()] = value.strip()
                        if behavior == "404":
                            connection.sendall(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
                            return
                        accept = base64.b64encode(hashlib.sha1((headers["sec-websocket-key"] +
                                                               "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest())
                        if behavior == "bad-accept":
                            accept = b"invalid"
                        connection.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                                           b"Connection: Upgrade\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\n")
                        if behavior == "bad-accept":
                            return
                        if code:
                            opcode, body = receive(stream)
                            self.assertEqual(opcode, 1)
                            captured["relay"] = json.loads(body)
                            if behavior == "missing-host":
                                send(connection, 8, struct.pack("!H", 4004))
                                return
                            send(connection, 1, b'{"op":"ready"}')
                        opcode, body = receive(stream)
                        self.assertEqual(opcode, 2)
                        self.assertEqual(struct.unpack("<I", body[:4])[0], len(body) - 4)
                        self.assertEqual(body[4], 1)
                        captured["hello"] = json.loads(body[5:])
                        if behavior == "rejected":
                            send(connection, 8, struct.pack("!H", 4001))
                            return
                        if behavior == "fragment":
                            connection.sendall(b"\x02\x01x")
                            return
                        if behavior == "oversized":
                            connection.sendall(b"\x82\x7f" + struct.pack("!Q", 262145))
                            return
                        if behavior == "timeout":
                            stream.read(1)  # wait until the bounded client disconnects
                            return
                        send(connection, 9, b"ping")
                        self.assertEqual(receive(stream), (10, b"ping"))
                        send(connection, 2, protocol(10, {"test": "heartbeat"}))
                        pong_opcode, pong = receive(stream)
                        self.assertEqual((pong_opcode, pong[4]), (2, 11))
                        state = {"windows": [], "capabilities": {"multiplex": behavior != "unsupported"}}
                        if behavior == "spoof-capability":
                            state = {"windows": [], "capabilities": [{"multiplex": True}]}
                        send(connection, 2, protocol(2, state))
                        if behavior == "success":
                            self.assertEqual(receive(stream), (8, struct.pack("!H", 1000)))
                except Exception as error:
                    errors.append(error)

            thread = threading.Thread(target=serve, daemon=True)
            thread.start()
            run = subprocess.run([self.java, "-Dfile.encoding=UTF-8", "-cp", self.temporary.name, "ProbeCheck",
                                  f"http://127.0.0.1:{port}/prefix/", code, token],
                                 check=True, capture_output=True, encoding="utf-8", timeout=12)
            thread.join(10)
            self.assertFalse(thread.is_alive(), "probe server did not finish")
            if errors:
                raise errors[0]
        parsed = urlsplit(captured["path"])
        self.assertEqual(parsed.path, "/prefix/ws")
        self.assertEqual(parse_qs(parsed.query), {})
        self.assertNotIn(token, run.stdout)
        return run.stdout, captured

    def test_direct_real_handshake_and_masked_ping(self):
        result, captured = self.run_probe()
        self.assertIn("测试成功", result)
        self.assertEqual(captured["hello"], {"token": 'private-"\\\n中文', "multiplex": True, "reliable": True})

    def test_relay_registration_and_host_authentication(self):
        result, captured = self.run_probe(code='room-"\\中文')
        self.assertIn("测试成功", result)
        self.assertEqual(captured["relay"], {"op": "phone", "code": 'room-"\\中文',
                                             "token": 'private-"\\\n中文', "reliable": True})

    def test_invalid_endpoint_rejected_before_network_access(self):
        for endpoint in ("javascript:alert(1)", "https://user:secret@example.com", "http://127.0.0.1:0"):
            run = subprocess.run([self.java, "-cp", self.temporary.name, "ProbeCheck", endpoint, "", "secret"],
                                 check=True, capture_output=True, encoding="utf-8", timeout=3)
            self.assertIn("地址无效", run.stdout)
            self.assertNotIn("secret", run.stdout)

    def test_failures_report_actionable_errors(self):
        for behavior, expected in [("404", "404"), ("bad-accept", "握手"), ("rejected", "认证失败"),
                                   ("unsupported", "不兼容"), ("fragment", "不支持"), ("oversized", "过大"),
                                   ("spoof-capability", "不兼容"), ("timeout", "超时")]:
            with self.subTest(behavior=behavior):
                result, _ = self.run_probe(behavior)
                self.assertIn("测试失败", result)
                self.assertIn(expected, result)
        result, _ = self.run_probe("missing-host", code="absent")
        self.assertIn("在线主机", result)


if __name__ == "__main__":
    unittest.main()
