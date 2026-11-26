#!/usr/bin/env python3
"""Lightweight HTTP API: POST an image to /send and the server emails it. Stdlib only.

  curl -F "file=@photo.png" http://server:8989/send

The image is held in memory only. It is never written to disk and is dropped after sending.

Settings (environment): SMTP_HOST, SMTP_PORT, SMTP_SECURITY, SMTP_USER, SMTP_PASS,
MAIL_FROM, MAIL_TO, MAIL_SUBJECT, plus HOST (default 0.0.0.0), PORT (default 8989),
and MAX_UPLOAD_MB (default 25).

Responses (JSON):
  200 {"ok": true, "sha256": "...", "bytes": N}  only after the SMTP server accepted the email
  400 bad upload | 413 too big | 502 email could not be sent
"""
import hashlib
import json
import os
import sys
from email.parser import BytesParser
from email.policy import default as default_policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import sendfile

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


class Handler(BaseHTTPRequestHandler):
    server_version = "sendfile/2.0"

    def respond(self, code, **body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            return self.respond(200, ok=True)
        self.respond(404, ok=False, error="not found")

    def do_POST(self):
        if self.path.split("?", 1)[0] != "/send":
            return self.respond(404, ok=False, error="not found")

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
        del fields
        if not data:
            return self.respond(400, ok=False, error="file is empty")
        mime = sniff_image(data)
        if not mime:
            return self.respond(400, ok=False, error="file is not a supported image (jpeg/png/gif/webp/bmp)")

        filename = os.path.basename(filename or "") or f"image.{EXT[mime]}"
        digest = hashlib.sha256(data).hexdigest()
        size = len(data)
        cfg = self.server.cfg
        try:
            msg = sendfile.build_message(cfg, data=data, filename=filename, ctype=mime)
            sendfile.send(cfg, msg, retry_delay=self.server.retry_delay)
        except Exception as e:
            sendfile.log(f"FAILED sending {filename}: {e!r}")
            return self.respond(502, ok=False, error="email could not be sent")
        finally:
            data = msg = None  # drop the image from memory
        sendfile.log(f"OK: emailed {filename} sha256={digest[:12]} to {', '.join(cfg['to'])}")
        self.respond(200, ok=True, filename=filename, bytes=size, sha256=digest, to=cfg["to"])

    def log_message(self, fmt, *args):
        sendfile.log(f"{self.client_address[0]} {fmt % args}")


def make_server(env=os.environ):
    cfg = sendfile.load_config(env, need_file=False)
    srv = ThreadingHTTPServer((env.get("HOST", "0.0.0.0"), int(env.get("PORT", "8989"))), Handler)
    srv.cfg = cfg
    srv.retry_delay = 2
    srv.max_bytes = int(float(env.get("MAX_UPLOAD_MB", "25")) * 2**20)
    return srv


def main():
    srv = make_server()
    host, port = srv.server_address[:2]
    sendfile.log(f"listening on http://{host}:{port}  (POST /send, GET /health)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
