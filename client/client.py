#!/usr/bin/env python3
"""Screenshot client (Windows; also works on macOS).

It stays connected to the server and waits. When a trusted sender emails "status" to the server's
Gmail account, the server tells this client to take a screenshot of the whole screen (all monitors)
and upload it. The server then emails it back to the person who asked.

The screenshot exists only in memory. It is never written to disk.
A send counts as done only when the server answers 200 {"ok": true} AND the sha256 it
emailed matches the screenshot we took. Otherwise the client sends the same image again.

  python client.py --server http://192.168.1.50:8989

To test without sending an email, use "Take screenshot now" on the server's settings page.

Requires: pip install pillow
"""
import argparse
import hashlib
import io
import json
import socket
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from urllib.parse import urlencode


def log(text):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {text}", flush=True)


def _enable_dpi_awareness():
    """Without this, Windows display scaling (125%, 150%...) gives a cropped or blurry capture."""
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor aware (Win 8.1+)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass  # not on Windows


def capture_screen(fmt="png"):
    """Return (image_bytes, filename) for the full virtual desktop, all monitors."""
    from PIL import ImageGrab  # imported here so the tests can run without Pillow
    img = ImageGrab.grab(all_screens=True)
    buf = io.BytesIO()
    if fmt == "jpeg":
        img.convert("RGB").save(buf, "JPEG", quality=85, optimize=True)
    else:
        img.save(buf, "PNG", optimize=True)
    img.close()
    ext = "jpg" if fmt == "jpeg" else "png"
    host = "".join(ch for ch in socket.gethostname().split(".")[0] if ch.isalnum() or ch in "-_") or "pc"
    return buf.getvalue(), f"{host}_screenshot_{datetime.now():%Y-%m-%d_%H-%M-%S}.{ext}"


def post_image(server, data, filename, requests=(), client_name="", timeout=120):
    """POST the image once. Return (http_status, json_body). Raises on network errors."""
    boundary = uuid.uuid4().hex
    ctype = "image/jpeg" if filename.endswith(".jpg") else "image/png"
    body = b""
    for name, value in (("requests", ",".join(map(str, requests))), ("client", client_name)):
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n"
                 f"{value}\r\n").encode()
    body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
             f"Content-Type: {ctype}\r\n\r\n").encode() + data + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(server.rstrip("/") + "/send", data=body, method="POST",
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}


def send_verified(server, data, filename, requests=(), client_name="", retries=5, backoff=5.0,
                  max_backoff=60.0):
    """Send until the server confirms the exact image was emailed. Return True on success."""
    expected = hashlib.sha256(data).hexdigest()
    delay = backoff
    for attempt in range(1, retries + 1):
        try:
            status, body = post_image(server, data, filename, requests, client_name)
            if status == 200 and body.get("ok") is True and body.get("sha256") == expected:
                log(f"OK: {filename} ({len(data)} bytes) emailed, verified sha256={expected[:12]}")
                return True
            if status == 200 and body.get("sha256") != expected:
                reason = f"checksum mismatch (server {str(body.get('sha256'))[:12]}, local {expected[:12]})"
            else:
                reason = f"HTTP {status}: {body.get('error', body)}"
                if status in (400, 413):  # resending the same thing can't fix these
                    log(f"FAILED: {reason}")
                    return False
        except (urllib.error.URLError, OSError, ValueError) as e:
            reason = f"network error: {e}"
        log(f"attempt {attempt}/{retries} failed: {reason}")
        if attempt < retries:
            time.sleep(delay)
            delay = min(delay * 2, max_backoff)
    log(f"GAVE UP on {filename} after {retries} attempts")
    return False


def run_once(server, retries, fmt="png", capture=capture_screen, requests=(), client_name=""):
    data, filename = capture(fmt)
    try:
        return send_verified(server, data, filename, requests, client_name, retries=retries)
    finally:
        del data  # drop the screenshot from memory


def wait_for_trigger(server, after, boot, client_name, timeout=90):
    """Long-poll the server. Reply: {"capture": bool, "requests": [ids], "seq": int, "boot": str}."""
    q = urlencode({"after": after, "boot": boot or "", "client": client_name})
    with urllib.request.urlopen(f"{server.rstrip('/')}/wait?{q}", timeout=timeout) as r:
        return json.loads(r.read())


def listen(server, retries, fmt="png", capture=capture_screen, client_name=None, stop=lambda: False):
    """Wait for capture requests forever (until stop() returns True)."""
    client_name = client_name or socket.gethostname()
    seq, boot, connected = -1, None, False
    while not stop():
        try:
            reply = wait_for_trigger(server, seq, boot, client_name)
            if not connected:
                log(f"connected to {server}, waiting for a screenshot request")
                connected = True
            if reply.get("capture"):
                ids = reply.get("requests", [])
                log(f"capture requested (request {', '.join(map(str, ids))})")
                try:
                    run_once(server, retries, fmt, capture, ids, client_name)
                except Exception as e:  # e.g. screen locked or capture failed; keep listening
                    log(f"ERROR capturing: {e!r}")
            seq, boot = reply["seq"], reply["boot"]
        except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
            if connected:
                log(f"lost connection to server ({e}), retrying")
                connected = False
            time.sleep(5)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", required=True, help="e.g. http://192.168.1.50:8989")
    ap.add_argument("--retries", type=int, default=5, help="attempts per screenshot (default 5)")
    ap.add_argument("--format", choices=["png", "jpeg"], default="jpeg",
                    help="jpeg = small (default), png = sharp text but large")
    args = ap.parse_args(argv)
    _enable_dpi_awareness()
    listen(args.server, args.retries, args.format)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
