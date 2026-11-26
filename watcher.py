"""Watch a Gmail (or any IMAP) mailbox for new emails with a trigger subject, e.g. "status".

The mailbox is opened READ-ONLY: nothing is marked read, moved or deleted.
Only emails that arrive after the server starts are considered, so old "status" emails never
fire. Each message is identified by its IMAP UID, so a message fires once even if it has been read.
"""
import email
import email.policy
import email.utils
import imaplib
import re
import threading
import time

import sendfile


def load_watch_config(env):
    raw = env.get("TRUSTED_SENDERS") or env.get("ALLOWED_SENDERS") or ""
    trusted = {a.strip().lower() for a in raw.split(",") if a.strip()}
    return {
        "host": env.get("IMAP_HOST", "imap.gmail.com"),
        "port": int(env.get("IMAP_PORT", "993")),
        "ssl": env.get("IMAP_SSL", "true").lower() != "false",
        "user": env.get("IMAP_USER") or env.get("SMTP_USER", ""),
        "password": env.get("IMAP_PASS") or env.get("SMTP_PASS", ""),
        "folder": env.get("IMAP_FOLDER", "INBOX"),
        "subject": env.get("TRIGGER_SUBJECT", "status").strip().lower(),
        "trusted": trusted,
        "interval": float(env.get("CHECK_INTERVAL") or "15"),
    }


class ImapMailbox:
    """Thin wrapper over imaplib so the watcher logic can be tested with a fake."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.conn = None

    def connect(self):
        c = self.cfg
        cls = imaplib.IMAP4_SSL if c["ssl"] else imaplib.IMAP4
        self.conn = cls(c["host"], c["port"], timeout=60)
        self.conn.login(c["user"], c["password"])
        typ, _ = self.conn.select(_quote(c["folder"]), readonly=True)
        if typ != "OK":
            raise RuntimeError(f"cannot open folder {c['folder']!r}")

    def latest_uid(self):
        typ, data = self.conn.status(_quote(self.cfg["folder"]), "(UIDNEXT)")
        m = re.search(rb"UIDNEXT (\d+)", data[0] or b"")
        if typ != "OK" or not m:
            raise RuntimeError(f"STATUS failed: {data!r}")
        return int(m.group(1)) - 1

    def messages_after(self, uid):
        """Return [(uid, from_addr, subject, message_id)] for messages with UID > uid."""
        self.conn.noop()  # lets the server report newly arrived mail
        typ, data = self.conn.uid("SEARCH", None, f"UID {uid + 1}:*")
        if typ != "OK":
            raise RuntimeError(f"SEARCH failed: {data!r}")
        uids = [int(u) for u in (data[0] or b"").split() if int(u) > uid]  # "n:*" can return the last msg
        if not uids:
            return []
        typ, data = self.conn.uid("FETCH", ",".join(map(str, uids)),
                                  "(UID BODY.PEEK[HEADER.FIELDS (FROM SUBJECT MESSAGE-ID)])")
        out = []
        for item in data:
            if not isinstance(item, tuple):
                continue
            m = re.search(rb"UID (\d+)", item[0])
            hdr = email.message_from_bytes(item[1], policy=email.policy.default)
            out.append((int(m.group(1)) if m else 0,
                        email.utils.parseaddr(str(hdr.get("From", "")))[1].lower(),
                        str(hdr.get("Subject", "")),
                        str(hdr.get("Message-ID", "")).strip()))
        return sorted(out)

    def close(self):
        try:
            self.conn.logout()
        except Exception:
            pass


def _imap_msg(e):
    msg = e.args[0] if e.args else e
    return msg.decode(errors="replace") if isinstance(msg, bytes) else str(msg)


def _quote(folder):
    return '"' + folder.replace('"', '\\"') + '"'


class Watcher(threading.Thread):
    """Calls on_trigger(from_addr, subject, message_id) for each new message with the trigger
    subject from a trusted sender. Everything else is ignored."""

    def __init__(self, cfg, on_trigger, mailbox_factory=ImapMailbox):
        super().__init__(daemon=True, name="mail-watcher")
        self.cfg = cfg
        self.on_trigger = on_trigger
        self.mailbox_factory = mailbox_factory
        self.last_uid = None  # None = take a baseline on first connect
        self.stop_event = threading.Event()
        self.state = "starting"   # starting | watching | error  (shown in the settings UI)
        self.error = ""
        self.last_check = None

    def matches(self, sender, subject):
        if subject.strip().lower() != self.cfg["subject"]:
            return False
        if sender not in self.cfg["trusted"]:
            sendfile.log(f"ignored '{subject.strip()}' from {sender or '(no sender)'}: not a trusted sender")
            return False
        return True

    def check(self, mailbox):
        for uid, sender, subject, message_id in mailbox.messages_after(self.last_uid):
            self.last_uid = max(self.last_uid, uid)
            if self.stop_event.is_set():
                return
            if self.matches(sender, subject):
                sendfile.log(f"trigger: '{subject}' from {sender} (uid {uid})")
                self.on_trigger(sender, subject.strip(), message_id)

    def run(self):
        backoff = 5
        while not self.stop_event.is_set():
            mailbox = self.mailbox_factory(self.cfg)
            try:
                mailbox.connect()
                if self.last_uid is None:
                    self.last_uid = mailbox.latest_uid()
                sendfile.log(f"watching {self.cfg['user']} / {self.cfg['folder']} for subject "
                             f"'{self.cfg['subject']}' every {self.cfg['interval']:g}s")
                if not self.cfg["trusted"]:
                    sendfile.log("WARNING: no trusted senders configured, so every request will be ignored")
                backoff = 5
                self.state, self.error = "watching", ""
                while not self.stop_event.is_set():
                    self.check(mailbox)
                    self.last_check = time.time()
                    self.stop_event.wait(self.cfg["interval"])
            except imaplib.IMAP4.error as e:
                self.state, self.error = "error", _imap_msg(e)
                sendfile.log(f"IMAP error: {self.error}")  # e.g. bad password
            except Exception as e:
                self.state, self.error = "error", repr(e)
                sendfile.log(f"mail watcher error: {e!r}, reconnecting in {backoff}s")
            finally:
                mailbox.close()
            self.stop_event.wait(backoff)
            backoff = min(backoff * 2, 300)

    def stop(self):
        self.stop_event.set()
