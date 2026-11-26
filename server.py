#!/usr/bin/env python3
"""Lightweight HTTP server. Stdlib only.

Flow: the server watches the Gmail inbox. When a new email titled "status" arrives FROM A TRUSTED
SENDER, it tells every waiting client to take a screenshot. Each client uploads its screenshot to /send,
and the server emails it back to the sender who asked, and only to them, as a reply in the same thread.
Emails from senders who aren't trusted are ignored.

  GET  /                            settings page (password-protected if ADMIN_PASSWORD is set)
  GET  /wait?after=<seq>&boot=<id>  client long-poll; returns {"capture": true, "requests": [ids]}
  POST /send                        multipart: file=image, requests=ids. Emails the requesters
  POST /trigger                     {"to": "<trusted address>"}: request a capture by hand
  GET  /health

Settings come from the environment (.env) and are overridden by what is saved in the UI
(SETTINGS_FILE, default ./settings.json). Images are only ever held in memory.
"""
import base64
import hashlib
import hmac
import json
import os
import sys
import threading
import time
import uuid
from email.message import EmailMessage
from email.parser import BytesParser
from email.policy import default as default_policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import sendfile
import settings
import watcher

UI_HTML = Path(__file__).with_name("ui.html")

# Sniff the actual bytes rather than trusting the client's filename / content type.
IMAGE_SIGNATURES = [
    (lambda b: b.startswith(b"\xff\xd8\xff"), "image/jpeg"),
    (lambda b: b.startswith(b"\x89PNG\r\n\x1a\n"), "image/png"),
    (lambda b: b[:6] in (b"GIF87a", b"GIF89a"), "image/gif"),
    (lambda b: b[:4] == b"RIFF" and b[8:12] == b"WEBP", "image/webp"),
    (lambda b: b.startswith(b"BM"), "image/bmp"),
]
EXT = {"image/jpeg": "jpg", "image/png": "png", "image/gif": "gif",
       "image/webp": "webp", "image/bmp": "bmp"}


def sniff_image(data):
    for check, mime in IMAGE_SIGNATURES:
        if check(data):
            return mime
    return None


def parse_multipart(content_type, body):
    """Return {field_name: (filename, bytes)} for a multipart/form-data body (in memory)."""
    msg = BytesParser(policy=default_policy).parsebytes(
        b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + body)
    if not msg.is_multipart():
        return {}
    fields = {}
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if name:
            fields[name] = (part.get_filename(), part.get_payload(decode=True) or b"")
    return fields


REQUEST_TTL = 3600  # a request can be answered for this long


class Trigger:
    """A counter that clients long-poll on. Each request increments it once and records
    who asked, so the screenshot can be sent back to that person only."""

    def __init__(self):
        self.cond = threading.Condition()
        self.seq = 0
        self.requests = {}                # seq -> {"to", "subject", "message_id", "time", "sent": set}
        self.boot = uuid.uuid4().hex[:8]  # lets clients notice a server restart (seq reset)
        self.waiting = {}                 # client name -> number of open /wait requests
        self.seen = {}                    # client name -> last time it contacted us
        self.last = None

    def fire(self, to, subject="status", message_id="", reason="manual"):
        now = time.time()
        with self.cond:
            self.seq += 1
            self.requests = {k: v for k, v in self.requests.items() if now - v["time"] < REQUEST_TTL}
            self.requests[self.seq] = {"to": to, "subject": subject, "message_id": message_id,
                                       "time": now, "sent": set()}
            clients = sorted(n for n, c in self.waiting.items() if c > 0)
            self.last = {"seq": self.seq, "reason": reason, "to": to, "time": now, "clients": clients}
            self.cond.notify_all()
        sendfile.log(f"capture #{self.seq} requested ({reason}); clients waiting: "
                     + (", ".join(clients) if clients else
                        "none connected right now (a client that reconnects soon will still capture)"))
        return self.seq

    def wait(self, after, timeout, client):
        with self.cond:
            self.waiting[client] = self.waiting.get(client, 0) + 1
            self.seen[client] = time.time()
            try:
                self.cond.wait_for(lambda: self.seq > after, timeout)
                return self.seq
            finally:
                self.waiting[client] -= 1
                self.seen[client] = time.time()

    def pending(self, after, upto):
        """Request ids in (after, upto] that can still be answered."""
        now = time.time()
        with self.cond:
            return [i for i in range(after + 1, upto + 1)
                    if i in self.requests and now - self.requests[i]["time"] < REQUEST_TTL]

    def lookup(self, ids):
        now = time.time()
        with self.cond:
            return {i: self.requests[i] for i in ids
                    if i in self.requests and now - self.requests[i]["time"] < REQUEST_TTL}

    def touch(self, client):
        with self.cond:
            self.seen[client] = time.time()

    def clients(self):
        now = time.time()
        with self.cond:
            return [{"name": n, "online": self.waiting.get(n, 0) > 0 or now - t < 15, "last_seen": t}
                    for n, t in sorted(self.seen.items()) if now - t < 7 * 86400]


class App(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, env):
        self.env = dict(env)
        super().__init__((env.get("HOST", "0.0.0.0"), int(env.get("PORT", "8989"))), Handler)
        self.settings_path = env.get("SETTINGS_FILE") or str(Path(__file__).with_name("settings.json"))
        self.admin_password = env.get("ADMIN_PASSWORD", "")
        self.watch_enabled = env.get("WATCH_EMAIL", "true").lower() != "false"
        self.retry_delay = 2
        self.max_bytes = int(float(env.get("MAX_UPLOAD_MB", "25")) * 2**20)
        self.wait_timeout = float(env.get("WAIT_TIMEOUT", "50"))
        self.trigger = Trigger()
        self.lock = threading.Lock()
        self.watcher = None
        self.trusted = set()
        self.cfg = None
        self.config_error = ""
        self.last_sent = None
        self.apply(settings.load(self.env, self.settings_path))

    def merged_env(self, values):
        return {**self.env, **values}

    def apply(self, values):
        """Use these settings from now on. Restarts the inbox watcher."""
        with self.lock:
            self.values = values
            try:
                self.cfg = sendfile.load_config(self.merged_env(values), need_file=False, need_to=False)
                self.config_error = ""
            except sendfile.ConfigError as e:
                self.cfg, self.config_error = None, str(e)
                sendfile.log(f"not configured yet: {e}. Open the settings page to finish setup.")
            self.trusted = watcher.load_watch_config(self.merged_env(values))["trusted"]
            if self.watcher:
                self.watcher.stop()
                self.watcher = None
            if self.cfg and self.watch_enabled:
                self.watcher = watcher.Watcher(
                    watcher.load_watch_config(self.merged_env(values)),
                    on_trigger=lambda sender, subject, msgid: self.trigger.fire(
                        sender, subject, msgid, reason=f"email from {sender}"))
                self.watcher.start()

    def status(self):
        w = self.watcher
        return {
            "configured": self.cfg is not None,
            "config_error": self.config_error,
            "watcher": None if not w else {"state": w.state, "error": w.error, "last_check": w.last_check,
                                           "user": w.cfg["user"], "subject": w.cfg["subject"]},
            "watch_enabled": self.watch_enabled,
            "trusted": sorted(self.trusted),
            "clients": self.trigger.clients(),
            "last_trigger": self.trigger.last,
            "last_sent": self.last_sent,
            "now": time.time(),
        }


class Handler(BaseHTTPRequestHandler):
    server_version = "captureandmail/3.0"

    # ---------- helpers ----------
    def respond(self, code, **body):
        self.send_bytes(code, json.dumps(body).encode(), "application/json")

    def send_bytes(self, code, data, ctype, extra_headers=()):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in extra_headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def authorized(self):
        """HTTP Basic auth for the settings UI, only if ADMIN_PASSWORD is set (any username)."""
        pw = self.server.admin_password
        if not pw:
            return True
        hdr = self.headers.get("Authorization", "")
        if hdr.startswith("Basic "):
            try:
                given = base64.b64decode(hdr[6:]).decode().split(":", 1)[1]
                if hmac.compare_digest(given.encode(), pw.encode()):
                    return True
            except (ValueError, IndexError):
                pass
        self.send_bytes(401, b'{"ok": false, "error": "password required"}', "application/json",
                        [("WWW-Authenticate", 'Basic realm="captureandmail settings"')])
        return False

    def read_json(self, limit=64 * 1024):
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            n = 0
        if n <= 0 or n > limit:
            return {}
        try:
            data = json.loads(self.rfile.read(n))
            return data if isinstance(data, dict) else {}
        except ValueError:
            return {}

    # ---------- routes ----------
    def do_GET(self):
        url = urlsplit(self.path)
        if url.path == "/health":
            return self.respond(200, ok=True)
        if url.path == "/wait":
            return self.handle_wait(parse_qs(url.query))
        if url.path in ("/", "/settings"):
            if self.authorized():
                self.send_bytes(200, UI_HTML.read_bytes(), "text/html; charset=utf-8",
                                [("X-Frame-Options", "DENY"), ("Referrer-Policy", "no-referrer")])
            return
        if url.path == "/api/settings":
            if self.authorized():
                self.respond(200, ok=True, fields=settings.FIELDS,
                             values=settings.public(self.server.values), status=self.server.status(),
                             protected=bool(self.server.admin_password))
            return
        if url.path == "/api/status":
            if self.authorized():
                self.respond(200, ok=True, status=self.server.status())
            return
        self.respond(404, ok=False, error="not found")

    def do_POST(self):
        path = urlsplit(self.path).path
        if path == "/send":
            return self.handle_send()
        if path == "/trigger":
            to = str(self.read_json().get("to", "")).strip().lower()
            if not to or to not in self.server.trusted:
                return self.respond(403, ok=False, error="'to' must be one of the trusted senders")
            seq = self.server.trigger.fire(to, "status", reason=f"manual, for {to}")
            return self.respond(200, ok=True, seq=seq)
        if path == "/api/settings":
            if self.authorized():
                self.handle_save()
            return
        if path == "/api/test":
            if self.authorized():
                self.handle_test()
            return
        self.respond(404, ok=False, error="not found")

    def handle_wait(self, q):
        trig = self.server.trigger
        client = (q.get("client", ["?"])[0] or "?")[:64]
        try:
            after = int(q.get("after", ["-1"])[0])
        except ValueError:
            after = -1
        if after < 0 or q.get("boot", [""])[0] != trig.boot:
            # New client or server restarted: hand out the current position, don't capture.
            trig.touch(client)
            return self.respond(200, ok=True, capture=False, seq=trig.seq, boot=trig.boot)
        seq = trig.wait(after, self.server.wait_timeout, client)
        ids = trig.pending(after, seq)
        self.respond(200, ok=True, capture=bool(ids), requests=ids, seq=seq, boot=trig.boot)

    def handle_save(self):
        body = self.read_json()
        submitted = body.get("values") if isinstance(body.get("values"), dict) else {}
        new = settings.merge_update(self.server.values, submitted)
        errors = settings.validate(new)
        if errors:
            return self.respond(400, ok=False, errors=errors)
        try:
            settings.save(self.server.settings_path, new)
        except OSError as e:
            return self.respond(500, ok=False, error=f"could not save settings: {e}")
        self.server.apply(new)
        sendfile.log("settings saved from the web UI")
        self.respond(200, ok=True, values=settings.public(new), status=self.server.status())

    def handle_test(self):
        body = self.read_json()
        what = body.get("what")
        srv = self.server
        if srv.cfg is None:
            return self.respond(200, ok=False, error=f"Not configured: {srv.config_error}")
        try:
            if what == "smtp":
                with sendfile.connect(srv.cfg, timeout=20):
                    pass
                return self.respond(200, ok=True,
                                    message=f"Logged in to {srv.cfg['host']} as {srv.cfg['user']}.")
            if what == "imap":
                box = watcher.ImapMailbox(watcher.load_watch_config(srv.merged_env(srv.values)))
                try:
                    box.connect()
                    box.latest_uid()
                finally:
                    if box.conn:
                        box.close()
                return self.respond(200, ok=True,
                                    message=f"Opened {box.cfg['folder']} of {box.cfg['user']}.")
            if what == "email":
                to = str(body.get("to", "")).strip().lower()
                if to not in srv.trusted:
                    return self.respond(200, ok=False, error="Pick one of the trusted senders.")
                msg = EmailMessage()
                msg["From"], msg["To"] = srv.cfg["from"], to
                msg["Subject"] = "captureandmail test email"
                msg.set_content("Email delivery from your captureandmail server works.\n")
                sendfile.send(srv.cfg, msg, retry_delay=0, to_addrs=[to])
                return self.respond(200, ok=True, message=f"Test email sent to {to}.")
        except Exception as e:
            return self.respond(200, ok=False, error=friendly_error(e))
        self.respond(400, ok=False, error="unknown test")

    def handle_send(self):
        cfg = self.server.cfg
        if cfg is None:
            return self.respond(503, ok=False, error="server is not configured yet; open the settings page")
        ctype = self.headers.get("Content-Type", "")
        if not ctype.startswith("multipart/form-data"):
            return self.respond(400, ok=False, error="use multipart/form-data with a 'file' field")
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return self.respond(411, ok=False, error="Content-Length required")
        max_bytes = self.server.max_bytes
        if length > max_bytes + 64 * 1024:  # allow for multipart overhead
            # Drain a moderately oversized body so the client sees the 413 instead of a reset.
            if length <= 4 * max_bytes:
                while length > 0:
                    chunk = self.rfile.read(min(length, 65536))
                    if not chunk:
                        break
                    length -= len(chunk)
            self.close_connection = True
            return self.respond(413, ok=False, error=f"max upload is {max_bytes // 2**20} MB")

        fields = parse_multipart(ctype, self.rfile.read(length))
        if "file" not in fields:
            return self.respond(400, ok=False, error="missing 'file' field")
        filename, data = fields.pop("file")
        try:
            ids = [int(x) for x in fields.get("requests", (None, b""))[1].decode().split(",") if x.strip()]
        except ValueError:
            ids = []
        client = fields.get("client", (None, b"?"))[1].decode(errors="replace")[:64] or "?"
        del fields
        if not data:
            return self.respond(400, ok=False, error="file is empty")
        mime = sniff_image(data)
        if not mime:
            return self.respond(400, ok=False, error="file is not a supported image (jpeg/png/gif/webp/bmp)")

        requests = self.server.trigger.lookup(ids)
        if not requests:
            return self.respond(400, ok=False, error="no valid pending request; screenshots are only "
                                                     "emailed in answer to a 'status' email")

        filename = os.path.basename(filename or "") or f"image.{EXT[mime]}"
        digest = hashlib.sha256(data).hexdigest()
        size = len(data)
        sent_to = []
        try:
            for rid, req in sorted(requests.items()):
                if digest in req["sent"]:
                    continue  # client resent after a lost response: don't email twice
                subj = req["subject"] or "status"
                msg = sendfile.build_message(
                    cfg, data=data, filename=filename, ctype=mime, to=[req["to"]],
                    subject=subj if subj.lower().startswith("re:") else f"Re: {subj}",
                    in_reply_to=req["message_id"] or None,
                    body=f"Screenshot from {client}, taken {time.strftime('%Y-%m-%d %H:%M:%S')}.\n")
                sendfile.send(cfg, msg, retry_delay=self.server.retry_delay, to_addrs=[req["to"]])
                msg = None
                req["sent"].add(digest)
                sent_to.append(req["to"])
                sendfile.log(f"OK: emailed {filename} sha256={digest[:12]} to {req['to']} (request #{rid})")
        except Exception as e:
            sendfile.log(f"FAILED sending {filename}: {e!r}")
            return self.respond(502, ok=False, error="email could not be sent")
        finally:
            data = msg = None  # drop the image from memory
        if sent_to:
            self.server.last_sent = {"filename": filename, "bytes": size, "to": sent_to, "time": time.time()}
        self.respond(200, ok=True, filename=filename, bytes=size, sha256=digest, recipients=len(requests))

    def log_message(self, fmt, *args):
        line = fmt % args
        if any(p in line for p in ('"GET /wait', '"GET /health', '"GET /api/status')):
            return  # long-polls, health checks and UI refreshes would flood the log
        sendfile.log(f"{self.client_address[0]} {line}")


def friendly_error(e):
    text = watcher._imap_msg(e) if isinstance(e, watcher.imaplib.IMAP4.error) else (str(e) or repr(e))
    low = text.lower()
    if ("535" in text or "username and password not accepted" in low or "authenticationfailed" in low
            or "invalid credentials" in low):
        return ("Gmail rejected the login. Use a 16-character App Password (not your normal password), "
                "and make sure the Gmail address matches the account that created it.")
    if "timed out" in low or "name or service not known" in low or "nodename nor servname" in low:
        return f"Could not reach the mail server ({text}). Check the host/port and the network."
    return text


def make_server(env=os.environ):
    return App(env)


def main():
    srv = make_server()
    host, port = srv.server_address[:2]
    sendfile.log(f"listening on http://{host}:{port}  (settings page: http://<this-machine>:{port}/)")
    if not srv.admin_password:
        sendfile.log("WARNING: ADMIN_PASSWORD is not set, anyone on the network can open the settings page")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
