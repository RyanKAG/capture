"""End-to-end tests for the API and the client: a real HTTP server plus an in-process fake SMTP server.

Run:  python3 -m unittest -v
"""
import email
import email.policy
import hashlib
import json
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import server
from test_sendfile import FakeSMTPServer

sys.path.insert(0, str(Path(__file__).resolve().parent / "client"))
import client  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + os.urandom(20_000)
JPEG = b"\xff\xd8\xff\xe0" + os.urandom(20_000)


def multipart(fields):
    """fields: {name: (filename, bytes)} for files, or {name: "text"} for plain form fields."""
    boundary = uuid.uuid4().hex
    out = b""
    for name, value in fields.items():
        if isinstance(value, str):
            out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n"
                    f"{value}\r\n").encode()
            continue
        filename, data = value
        out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; "
                f"filename=\"{filename}\"\r\nContent-Type: application/octet-stream\r\n\r\n").encode()
        out += data + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return f"multipart/form-data; boundary={boundary}", out


class Base(unittest.TestCase):
    extra_env = {}

    def setUp(self):
        import tempfile
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.smtp = FakeSMTPServer(creds=("user@example.com", "secret"))
        threading.Thread(target=self.smtp.serve_forever, daemon=True).start()
        self.api = server.make_server({
            "HOST": "127.0.0.1", "PORT": "0",
            "SMTP_HOST": "127.0.0.1", "SMTP_PORT": str(self.smtp.server_address[1]),
            "SMTP_SECURITY": "none", "SMTP_USER": "user@example.com", "SMTP_PASS": "secret",
            "MAIL_FROM": "api@example.com", "TRUSTED_SENDERS": "me@example.com, boss@example.com",
            "MAX_UPLOAD_MB": "1",
            "WATCH_EMAIL": "false", "SETTINGS_FILE": os.path.join(self.tmpdir.name, "settings.json"),
            **self.extra_env,
        })
        self.api.retry_delay = 0
        threading.Thread(target=self.api.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.api.server_address[1]}"

    def tearDown(self):
        for s in (self.api, self.smtp):
            s.shutdown()
            s.server_close()

    def emailed(self, i=0):
        return email.message_from_bytes(self.smtp.received[i]["data"], policy=email.policy.default)

    def emailed_attachment(self, i=0):
        return next(self.emailed(i).iter_attachments())

    def request(self, to="me@example.com", subject="status", message_id="<req1@mail.gmail.com>"):
        """Simulate a trusted 'status' email arriving; returns the request id."""
        return self.api.trigger.fire(to, subject, message_id)


class ServerTest(Base):
    def post(self, fields, path="/send", with_request=True):
        if with_request and "requests" not in fields:
            fields = {"requests": str(self.request()), **fields}
        ctype, body = multipart(fields)
        req = urllib.request.Request(self.url + path, data=body, method="POST",
                                     headers={"Content-Type": ctype})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_image_is_emailed_intact_and_hash_returned(self):
        status, body = self.post({"file": ("cat.png", PNG)})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["sha256"], hashlib.sha256(PNG).hexdigest())
        att = self.emailed_attachment()
        self.assertEqual(att.get_filename(), "cat.png")
        self.assertEqual(att.get_content_type(), "image/png")
        self.assertEqual(att.get_content(), PNG)
        self.assertEqual(self.smtp.received[0]["to"], ["<me@example.com>"])

    def test_reply_goes_only_to_requester_in_same_thread(self):
        rid = self.request(to="boss@example.com", subject="Status", message_id="<abc@mail.gmail.com>")
        status, body = self.post({"requests": str(rid), "client": "office-pc", "file": ("s.png", PNG)})
        self.assertEqual(status, 200, body)
        self.assertEqual(len(self.smtp.received), 1)
        self.assertEqual(self.smtp.received[0]["to"], ["<boss@example.com>"])
        msg = self.emailed()
        self.assertEqual(msg["To"], "boss@example.com")
        self.assertEqual(msg["Subject"], "Re: Status")
        self.assertEqual(msg["In-Reply-To"], "<abc@mail.gmail.com>")
        self.assertIn("office-pc", msg.get_body(("plain",)).get_content())

    def test_upload_without_request_is_not_emailed(self):
        status, body = self.post({"file": ("cat.png", PNG)}, with_request=False)
        self.assertEqual(status, 400)
        self.assertIn("no valid pending request", body["error"])
        self.assertEqual(self.smtp.received, [])

    def test_unknown_or_expired_request_is_not_emailed(self):
        self.assertEqual(self.post({"requests": "999", "file": ("cat.png", PNG)})[0], 400)
        rid = self.request()
        self.api.trigger.requests[rid]["time"] -= server.REQUEST_TTL + 1
        self.assertEqual(self.post({"requests": str(rid), "file": ("cat.png", PNG)})[0], 400)
        self.assertEqual(self.smtp.received, [])

    def test_two_requesters_each_get_their_own_email(self):
        a = self.request(to="me@example.com")
        b = self.request(to="boss@example.com")
        status, body = self.post({"requests": f"{a},{b}", "file": ("cat.png", PNG)})
        self.assertEqual(status, 200, body)
        self.assertEqual(sorted(r["to"][0] for r in self.smtp.received),
                         ["<boss@example.com>", "<me@example.com>"])
        self.assertTrue(all(len(r["to"]) == 1 for r in self.smtp.received))  # nobody sees the other

    def test_resending_same_screenshot_does_not_email_twice(self):
        rid = self.request()
        for _ in range(2):
            self.assertEqual(self.post({"requests": str(rid), "file": ("cat.png", PNG)})[0], 200)
        self.assertEqual(len(self.smtp.received), 1)

    def test_manual_trigger_only_to_trusted(self):
        def trig(to):
            req = urllib.request.Request(self.url + "/trigger", data=json.dumps({"to": to}).encode(),
                                         method="POST", headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    return r.status
            except urllib.error.HTTPError as e:
                return e.code
        self.assertEqual(trig("stranger@evil.com"), 403)
        self.assertEqual(trig(""), 403)
        self.assertEqual(trig("BOSS@example.com"), 200)
        self.assertEqual(self.api.trigger.requests[self.api.trigger.seq]["to"], "boss@example.com")

    def test_no_auth_required(self):
        status, _ = self.post({"file": ("x.jpg", JPEG)})
        self.assertEqual(status, 200)

    def test_jpeg_without_filename_gets_default_name(self):
        status, body = self.post({"file": ("", JPEG)})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["filename"], "image.jpg")

    def test_path_in_filename_is_stripped(self):
        status, body = self.post({"file": ("../../etc/evil.png", PNG)})
        self.assertEqual(body["filename"], "evil.png")

    def test_rejects_non_image(self):
        status, body = self.post({"file": ("cat.png", b"#!/bin/sh\necho hi\n")})
        self.assertEqual(status, 400)
        self.assertEqual(self.smtp.received, [])

    def test_rejects_missing_file_field(self):
        status, _ = self.post({"other": ("cat.png", PNG)})
        self.assertEqual(status, 400)

    def test_rejects_too_large(self):
        status, _ = self.post({"file": ("big.png", PNG + os.urandom(2 * 2**20))})
        self.assertEqual(status, 413)
        self.assertEqual(self.smtp.received, [])

    def test_smtp_failure_returns_502(self):
        self.smtp.fail_next = 99
        status, body = self.post({"file": ("cat.png", PNG)})
        self.assertEqual(status, 502)
        self.assertFalse(body["ok"])

    def test_nothing_written_to_disk(self):
        """Pointing the working dir and temp dir at an empty folder, a send leaves it empty."""
        import tempfile
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"TMPDIR": d}):
            cwd = os.getcwd()
            os.chdir(d)
            try:
                self.assertEqual(self.post({"file": ("cat.png", PNG)})[0], 200)
            finally:
                os.chdir(cwd)
            self.assertEqual(os.listdir(d), [])

    def test_health(self):
        with urllib.request.urlopen(self.url + "/health", timeout=10) as r:
            self.assertEqual(json.loads(r.read()), {"ok": True})


class ClientTest(Base):
    def setUp(self):
        super().setUp()
        self.sleeps = mock.patch("client.time.sleep").start()  # no real waiting in tests
        self.addCleanup(mock.patch.stopall)

    def fake_capture(self, fmt="png"):
        self.captures = getattr(self, "captures", 0) + 1
        return PNG, "screenshot_test.png"

    def run_client(self, url=None, retries=3):
        return client.run_once(url or self.url, retries=retries, capture=self.fake_capture,
                               requests=[self.request()], client_name="pc1")

    def test_client_sends_and_verifies(self):
        self.assertTrue(self.run_client())
        self.assertEqual(len(self.smtp.received), 1)
        self.assertEqual(self.emailed_attachment().get_content(), PNG)

    def test_client_retries_until_smtp_recovers(self):
        # Server makes 3 SMTP attempts per request, so 4 failures = request 1 fails (502),
        # request 2 fails on its first SMTP try and then succeeds on its own retry.
        self.smtp.fail_next = 4
        with mock.patch("client.post_image", wraps=client.post_image) as posts:
            self.assertTrue(self.run_client(retries=5))
        self.assertEqual(posts.call_count, 2)         # first POST got 502, second succeeded
        self.assertEqual(len(self.smtp.received), 1)  # exactly one email, no duplicates
        self.assertEqual(self.captures, 1)            # same screenshot resent, not a new one

    def test_client_gives_up_after_retries(self):
        self.smtp.fail_next = 999
        self.assertFalse(self.run_client(retries=3))
        self.assertEqual(self.smtp.received, [])

    def test_client_stops_on_permanent_error(self):
        with mock.patch("client.post_image", wraps=client.post_image) as posts:
            ok = client.run_once(self.url, retries=5, capture=self.fake_capture, requests=[12345])
        self.assertFalse(ok)
        self.assertEqual(posts.call_count, 1)  # 400 = request unknown; resending can't help

    def test_client_retries_when_server_down(self):
        self.assertFalse(self.run_client("http://127.0.0.1:1", retries=2))
        self.assertEqual(self.sleeps.call_count, 1)

    def test_client_rejects_wrong_checksum(self):
        """A server that says ok but reports a different hash is treated as a failure."""
        class Liar(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                body = json.dumps({"ok": True, "sha256": "0" * 64}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        liar = ThreadingHTTPServer(("127.0.0.1", 0), Liar)
        threading.Thread(target=liar.serve_forever, daemon=True).start()
        try:
            ok = self.run_client(f"http://127.0.0.1:{liar.server_address[1]}", retries=2)
        finally:
            liar.shutdown()
            liar.server_close()
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
