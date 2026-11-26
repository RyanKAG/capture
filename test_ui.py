"""Tests for the settings web UI and its JSON API.

Run:  python3 -m unittest -v
"""
import base64
import json
import os
import stat
import threading
import unittest
import urllib.error
import urllib.request

import server
from test_sendfile import FakeSMTPServer
from test_server import PNG, Base, multipart


def call(url, body=None, auth=None, method=None):
    headers = {}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if auth:
        headers["Authorization"] = "Basic " + base64.b64encode(f"admin:{auth}".encode()).decode()
    req = urllib.request.Request(url, data=data, headers=headers, method=method or ("POST" if data else "GET"))
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if r.headers.get_content_type() == "application/json" else raw)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def send_image(url, request_id=None):
    fields = {"file": ("cat.png", PNG)}
    if request_id is not None:
        fields = {"requests": str(request_id), **fields}
    ctype, body = multipart(fields)
    req = urllib.request.Request(url + "/send", data=body, method="POST", headers={"Content-Type": ctype})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


class SettingsUITest(Base):
    def test_page_served(self):
        status, html = call(self.url + "/")
        self.assertEqual(status, 200)
        self.assertIn(b"Capture &amp; Mail", html)

    def test_password_never_sent_to_browser(self):
        status, d = call(self.url + "/api/settings")
        self.assertEqual(status, 200)
        self.assertEqual(d["values"]["SMTP_PASS"], "")
        self.assertTrue(d["values"]["SMTP_PASS_set"])
        self.assertEqual(d["values"]["SMTP_USER"], "user@example.com")
        self.assertNotIn("secret", json.dumps(d["values"]))

    def test_save_applies_immediately_and_persists(self):
        status, d = call(self.url + "/api/settings", {"values": {"TRUSTED_SENDERS": " New@Example.com,x@y.com "}})
        self.assertEqual(status, 200, d)
        self.assertEqual(self.api.trusted, {"new@example.com", "x@y.com"})
        self.assertEqual(call(self.url + "/trigger", {"to": "me@example.com"})[0], 403)  # no longer trusted
        self.assertEqual(call(self.url + "/trigger", {"to": "new@example.com"})[0], 200)
        path = self.api.settings_path
        self.assertEqual(json.load(open(path))["TRUSTED_SENDERS"], "new@example.com, x@y.com")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_empty_password_keeps_existing_and_spaces_are_stripped(self):
        call(self.url + "/api/settings", {"values": {"SMTP_PASS": ""}})
        self.assertEqual(self.api.values["SMTP_PASS"], "secret")
        call(self.url + "/api/settings", {"values": {"SMTP_PASS": "abcd efgh ijkl mnop"}})
        self.assertEqual(self.api.values["SMTP_PASS"], "abcdefghijklmnop")

    def test_validation_errors(self):
        status, d = call(self.url + "/api/settings", {"values": {
            "TRUSTED_SENDERS": "not-an-email", "CHECK_INTERVAL": "1", "SMTP_USER": ""}})
        self.assertEqual(status, 400)
        self.assertEqual(set(d["errors"]), {"TRUSTED_SENDERS", "CHECK_INTERVAL", "SMTP_USER"})
        self.assertEqual(self.api.trusted, {"me@example.com", "boss@example.com"})  # nothing changed
        status, d = call(self.url + "/api/settings", {"values": {"TRUSTED_SENDERS": ""}})
        self.assertEqual(d["errors"], {"TRUSTED_SENDERS": "Required."})

    def test_settings_file_overrides_env_after_restart(self):
        call(self.url + "/api/settings", {"values": {"TRUSTED_SENDERS": "saved@example.com"}})
        restarted = server.make_server({**self.api.env, "PORT": "0"})
        try:
            self.assertEqual(restarted.trusted, {"saved@example.com"})
        finally:
            restarted.server_close()

    def test_login_test_button(self):
        status, d = call(self.url + "/api/test", {"what": "smtp"})
        self.assertTrue(d["ok"], d)
        call(self.url + "/api/settings", {"values": {"SMTP_PASS": "wrongpass"}})
        status, d = call(self.url + "/api/test", {"what": "smtp"})
        self.assertFalse(d["ok"])
        self.assertIn("App Password", d["error"])

    def test_test_email_button(self):
        status, d = call(self.url + "/api/test", {"what": "email", "to": "boss@example.com"})
        self.assertTrue(d["ok"], d)
        self.assertEqual(len(self.smtp.received), 1)
        self.assertEqual(self.smtp.received[0]["to"], ["<boss@example.com>"])
        status, d = call(self.url + "/api/test", {"what": "email", "to": "stranger@evil.com"})
        self.assertFalse(d["ok"])
        self.assertEqual(len(self.smtp.received), 1)

    def test_status_reports_clients_and_last_send(self):
        call(self.url + "/wait?after=-1&client=office-pc")
        send_image(self.url, self.api.trigger.fire("me@example.com"))
        status, d = call(self.url + "/api/status")
        self.assertEqual([c["name"] for c in d["status"]["clients"]], ["office-pc"])
        self.assertEqual(d["status"]["last_sent"]["filename"], "cat.png")


class AdminPasswordTest(Base):
    extra_env = {"ADMIN_PASSWORD": "hunter2"}

    def test_ui_requires_password(self):
        for path in ("/", "/api/settings", "/api/status"):
            self.assertEqual(call(self.url + path)[0], 401, path)
            self.assertEqual(call(self.url + path, auth="wrong")[0], 401, path)
            self.assertEqual(call(self.url + path, auth="hunter2")[0], 200, path)
        self.assertEqual(call(self.url + "/api/settings", {"values": {"TRUSTED_SENDERS": "evil@x.com"}})[0], 401)
        self.assertEqual(self.api.trusted, {"me@example.com", "boss@example.com"})

    def test_client_endpoints_stay_open(self):
        self.assertEqual(call(self.url + "/wait?after=-1")[0], 200)
        self.assertEqual(call(self.url + "/health")[0], 200)
        self.assertEqual(call(self.url + "/trigger", {"to": "me@example.com"})[0], 200)
        self.assertEqual(send_image(self.url, self.api.trigger.seq), 200)


class UnconfiguredServerTest(unittest.TestCase):
    """A fresh install with an empty .env starts, and can be set up entirely from the UI."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.smtp = FakeSMTPServer()
        threading.Thread(target=self.smtp.serve_forever, daemon=True).start()
        self.api = server.make_server({"HOST": "127.0.0.1", "PORT": "0", "WATCH_EMAIL": "false",
                                       "SETTINGS_FILE": os.path.join(self.tmp.name, "s.json")})
        self.api.retry_delay = 0
        threading.Thread(target=self.api.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.api.server_address[1]}"

    def tearDown(self):
        for s in (self.api, self.smtp):
            s.shutdown()
            s.server_close()
        self.tmp.cleanup()

    def test_setup_from_ui(self):
        self.assertEqual(send_image(self.url), 503)
        _, d = call(self.url + "/api/status")
        self.assertFalse(d["status"]["configured"])
        status, d = call(self.url + "/api/settings", {"values": {
            "SMTP_USER": "user@gmail.com", "SMTP_PASS": "secret", "TRUSTED_SENDERS": "me@example.com",
            "SMTP_HOST": "127.0.0.1", "SMTP_PORT": str(self.smtp.server_address[1]), "SMTP_SECURITY": "none"}})
        self.assertEqual(status, 200, d)
        self.assertTrue(d["status"]["configured"])
        self.smtp.creds = ("user@gmail.com", "secret")
        self.assertEqual(send_image(self.url, self.api.trigger.fire("me@example.com")), 200)
        self.assertEqual(len(self.smtp.received), 1)


if __name__ == "__main__":
    unittest.main()
