#!/usr/bin/env python3
"""Windows client: screenshot the whole screen (all monitors) and POST it to the server's /send.

The screenshot exists only in memory. It is never written to disk.
A send counts as done only when the server answers 200 {"ok": true} AND the sha256 it
emailed matches the screenshot we took. Otherwise the client sends the same image again.

  python client.py --server http://192.168.1.50:8989            # every 10 minutes
  python client.py --server http://192.168.1.50:8989 --once     # one screenshot, then exit

Requires: pip install pillow
"""
import argparse
import hashlib
import io
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime


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
    return buf.getvalue(), f"screenshot_{datetime.now():%Y-%m-%d_%H-%M-%S}.{ext}"


def post_image(server, data, filename, timeout=120):
    """POST the image once. Return (http_status, json_body). Raises on network errors."""
    boundary = uuid.uuid4().hex
    ctype = "image/jpeg" if filename.endswith(".jpg") else "image/png"
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
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


def send_verified(server, data, filename, retries=5, backoff=5.0, max_backoff=60.0):
    """Send until the server confirms the exact image was emailed. Return True on success."""
    expected = hashlib.sha256(data).hexdigest()
    delay = backoff
    for attempt in range(1, retries + 1):
        try:
            status, body = post_image(server, data, filename)
            if status == 200 and body.get("ok") is True and body.get("sha256") == expected:
                log(f"OK: {filename} ({len(data)} bytes) emailed, verified sha256={expected[:12]}")
                return True
            if status == 200 and body.get("sha256") != expected:
                reason = f"checksum mismatch (server {str(body.get('sha256'))[:12]}, local {expected[:12]})"
            else:
                reason = f"HTTP {status}: {body.get('error', body)}"
        except (urllib.error.URLError, OSError, ValueError) as e:
            reason = f"network error: {e}"
        log(f"attempt {attempt}/{retries} failed: {reason}")
        if attempt < retries:
            time.sleep(delay)
            delay = min(delay * 2, max_backoff)
    log(f"GAVE UP on {filename} after {retries} attempts")
    return False


def run_once(server, retries, fmt="png", capture=capture_screen):
    data, filename = capture(fmt)
    try:
        return send_verified(server, data, filename, retries=retries)
    finally:
        del data  # drop the screenshot from memory


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", required=True, help="e.g. http://192.168.1.50:8989")
    ap.add_argument("--interval", type=float, default=600, help="seconds between screenshots (default 600)")
    ap.add_argument("--retries", type=int, default=5, help="attempts per screenshot (default 5)")
    ap.add_argument("--format", choices=["png", "jpeg"], default="png",
                    help="png = sharp text, jpeg = much smaller (default png)")
    ap.add_argument("--once", action="store_true", help="take one screenshot, send it, exit")
    args = ap.parse_args(argv)
    _enable_dpi_awareness()

    if args.once:
        return 0 if run_once(args.server, args.retries, args.format) else 1

    log(f"sending a screenshot to {args.server} every {args.interval:g}s (Ctrl+C to stop)")
    while True:
        started = time.monotonic()
        try:
            run_once(args.server, args.retries, args.format)
        except Exception as e:  # e.g. the screen is locked or capture failed; keep going
            log(f"ERROR: {e!r}")
        time.sleep(max(0, args.interval - (time.monotonic() - started)))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
