"""End-to-end tests: a real SMTP server runs in-process, the script sends to it,
and we parse what arrived and compare the attachment byte-for-byte.

Run:  python3 -m unittest -v test_sendfile
"""
import email
import email.policy
import os
import socketserver
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "sendfile.py"


class FakeSMTPHandler(socketserver.StreamRequestHandler):
    """Minimal RFC 5321 server: EHLO, AUTH PLAIN/LOGIN, MAIL, RCPT, DATA, QUIT."""

    def reply(self, line):
        self.wfile.write(line.encode() + b"\r\n")

    def handle(self):
        srv = self.server
        self.reply("220 fake ESMTP ready")
        env = {"from": None, "to": []}
        while True:
            raw = self.rfile.readline()
            if not raw:
                return
            line = raw.decode().rstrip("\r\n")
            cmd = line.split(" ", 1)[0].upper()
            if cmd in ("EHLO", "HELO"):
                self.wfile.write(b"250-fake\r\n250-AUTH PLAIN LOGIN\r\n250 8BITMIME\r\n")
            elif cmd == "AUTH":
                import base64
                parts = line.split(" ")
                if parts[1].upper() == "PLAIN":
                    blob = parts[2] if len(parts) > 2 else None
                    if blob is None:
                        self.reply("334 ")
                        blob = self.rfile.readline().decode().strip()
                    _, user, pw = base64.b64decode(blob).decode().split("\0")
                else:  # LOGIN
                    self.reply("334 VXNlcm5hbWU6")
                    user = base64.b64decode(self.rfile.readline().strip()).decode()
                    self.reply("334 UGFzc3dvcmQ6")
                    pw = base64.b64decode(self.rfile.readline().strip()).decode()
                if (user, pw) == srv.creds:
                    self.reply("235 ok")
                else:
                    self.reply("535 bad credentials")
            elif cmd == "MAIL":
                env = {"from": line.split(":", 1)[1].strip(), "to": []}
                self.reply("250 ok")
            elif cmd == "RCPT":
                env["to"].append(line.split(":", 1)[1].strip())
                self.reply("250 ok")
            elif cmd == "DATA":
                if srv.fail_next > 0:  # simulate a temporary SMTP outage
                    srv.fail_next -= 1
                    self.reply("451 temporary failure")
                    continue
                self.reply("354 go")
                buf = []
                while True:
                    l = self.rfile.readline()
                    if l in (b".\r\n", b".\n", b""):
                        break
                    if l.startswith(b".."):
                        l = l[1:]  # undo dot-stuffing
                    buf.append(l)
                srv.received.append({**env, "data": b"".join(buf)})
                self.reply("250 queued")
            elif cmd in ("RSET", "NOOP"):
                self.reply("250 ok")
            elif cmd == "QUIT":
                self.reply("221 bye")
                return
            else:
                self.reply("502 not implemented")


class FakeSMTPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, creds=("user", "secret")):
        super().__init__(("127.0.0.1", 0), FakeSMTPHandler)
        self.creds = creds
        self.received = []
        self.fail_next = 0


class SendFileTest(unittest.TestCase):
    def setUp(self):
        self.server = FakeSMTPServer()
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.tmp = tempfile.TemporaryDirectory()
        # binary content incl. lines starting with "." to exercise dot-stuffing
        self.payload = os.urandom(50_000) + b"\r\n.\r\n..dots\r\n" + bytes(range(256))
        self.file = Path(self.tmp.name) / "capture.bin"
        self.file.write_bytes(self.payload)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def run_script(self, *args, **overrides):
        env = {
            "PATH": os.environ.get("PATH", ""),
            "SMTP_HOST": "127.0.0.1",
            "SMTP_PORT": str(self.server.server_address[1]),
            "SMTP_SECURITY": "none",
            "SMTP_USER": "user",
            "SMTP_PASS": "secret",
            "MAIL_FROM": "sender@example.com",
            "MAIL_TO": "a@example.com, b@example.com",
            "FILE_PATH": str(self.file),
        }
        env.update(overrides)
        return subprocess.run([sys.executable, str(SCRIPT), *args], env=env,
                              capture_output=True, text=True, timeout=60)

    def test_sends_file_intact(self):
        r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(len(self.server.received), 1)
        got = self.server.received[0]
        self.assertEqual(got["from"], "<sender@example.com>")
        self.assertEqual(got["to"], ["<a@example.com>", "<b@example.com>"])

        msg = email.message_from_bytes(got["data"], policy=email.policy.default)
        atts = list(msg.iter_attachments())
        self.assertEqual(len(atts), 1)
        self.assertEqual(atts[0].get_filename(), "capture.bin")
        self.assertEqual(atts[0].get_content(), self.payload)  # byte-for-byte
        self.assertIn("capture.bin", msg["Subject"])

    def test_check_mode_sends_nothing(self):
        r = self.run_script("--check")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.server.received, [])

    def test_bad_password_fails(self):
        r = self.run_script(SMTP_PASS="wrong")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("FAILED", r.stdout)
        self.assertEqual(self.server.received, [])

    def test_missing_file_fails(self):
        r = self.run_script(FILE_PATH=str(Path(self.tmp.name) / "nope.txt"))
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.server.received, [])

    def test_missing_config_fails(self):
        r = self.run_script(MAIL_TO="")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("MAIL_TO", r.stderr)


if __name__ == "__main__":
    unittest.main()
