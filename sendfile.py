#!/usr/bin/env python3
"""Email a file as an attachment via any SMTP provider. Stdlib only.

Config comes from environment variables (load them from config.env):
  SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS
  SMTP_SECURITY   starttls (default, port 587) | ssl (port 465) | none (local testing)
  MAIL_FROM, MAIL_TO (comma-separated), FILE_PATH
  MAIL_SUBJECT    optional, may contain {filename} and {time}

Usage:
  sendfile.py            send FILE_PATH once
  sendfile.py --check    connect + log in only, send nothing (verifies credentials)

Exit code 0 = sent (or check passed), non-zero = failure, so systemd/cron can see it.
"""
import argparse
import mimetypes
import os
import smtplib
import socket
import ssl
import sys
import time
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

RETRIES = 3
RETRY_DELAY_S = 10


def load_config(env=os.environ, need_file=True):
    def need(key):
        val = env.get(key, "").strip()
        if not val:
            sys.exit(f"config error: {key} is not set")
        return val

    security = env.get("SMTP_SECURITY", "starttls").strip().lower()
    if security not in ("starttls", "ssl", "none"):
        sys.exit(f"config error: SMTP_SECURITY must be starttls, ssl or none, got {security!r}")
    default_port = {"starttls": 587, "ssl": 465, "none": 25}[security]
    return {
        "host": need("SMTP_HOST"),
        "port": int(env.get("SMTP_PORT") or default_port),
        "user": env.get("SMTP_USER", "").strip(),
        "password": env.get("SMTP_PASS", ""),
        "security": security,
        "from": need("MAIL_FROM"),
        "to": [a.strip() for a in need("MAIL_TO").split(",") if a.strip()],
        "file": need("FILE_PATH") if need_file else env.get("FILE_PATH", ""),
        "subject": env.get("MAIL_SUBJECT", "File: {filename} ({time})"),
    }


def build_message(cfg, data=None, filename=None, ctype=None):
    """Build the email. Pass data+filename to attach in-memory bytes, else reads cfg['file']."""
    if data is None:
        path = Path(cfg["file"])
        if not path.is_file():
            raise FileNotFoundError(f"file not found: {path}")
        data, filename = path.read_bytes(), path.name

    msg = EmailMessage()
    msg["From"] = cfg["from"]
    msg["To"] = ", ".join(cfg["to"])
    msg["Subject"] = cfg["subject"].format(
        filename=filename, time=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    )
    msg.set_content(f"Attached: {filename} ({len(data)} bytes)\n")

    if not ctype:
        ctype, encoding = mimetypes.guess_type(filename)
        if ctype is None or encoding is not None:
            ctype = "application/octet-stream"
    maintype, subtype = ctype.split("/", 1)
    msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    return msg


def connect(cfg, timeout=30):
    if cfg["security"] == "ssl":
        smtp = smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=timeout,
                                context=ssl.create_default_context())
    else:
        smtp = smtplib.SMTP(cfg["host"], cfg["port"], timeout=timeout)
        if cfg["security"] == "starttls":
            smtp.starttls(context=ssl.create_default_context())
    if cfg["user"]:
        smtp.login(cfg["user"], cfg["password"])
    return smtp


def send(cfg, msg=None, retry_delay=RETRY_DELAY_S):
    msg = msg or build_message(cfg)
    last_err = None
    for attempt in range(1, RETRIES + 1):
        try:
            with connect(cfg) as smtp:
                refused = smtp.send_message(msg, from_addr=cfg["from"], to_addrs=cfg["to"])
            if refused:
                raise smtplib.SMTPRecipientsRefused(refused)
            return
        except smtplib.SMTPAuthenticationError:
            raise  # retrying bad credentials is pointless
        except (smtplib.SMTPException, OSError, socket.timeout) as e:
            last_err = e
            log(f"attempt {attempt}/{RETRIES} failed: {e!r}")
            if attempt < RETRIES:
                time.sleep(retry_delay)
    raise last_err


def log(text):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {text}", flush=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="only connect and log in")
    args = ap.parse_args(argv)
    cfg = load_config(need_file=not args.check)

    try:
        if args.check:
            with connect(cfg):
                pass
            log(f"OK: connected and authenticated to {cfg['host']}:{cfg['port']}")
        else:
            send(cfg)
            log(f"OK: sent {cfg['file']} to {', '.join(cfg['to'])}")
        return 0
    except Exception as e:
        log(f"FAILED: {e!r}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
