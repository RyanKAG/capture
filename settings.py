"""Server settings: defaults < environment (.env) < settings file saved from the web UI."""
import json
import os
import re
import tempfile

FIELDS = [
    # group, key, label, default, extra
    {"group": "Gmail account", "key": "SMTP_USER", "label": "Gmail address", "type": "email",
     "required": True, "help": "The inbox that receives requests, and the account the screenshots are sent from."},
    {"group": "Gmail account", "key": "SMTP_PASS", "label": "App Password", "type": "password",
     "secret": True, "required": True,
     "help": 'Create one at <a href="https://myaccount.google.com/apppasswords" target="_blank" '
             'rel="noopener">myaccount.google.com/apppasswords</a> (2-Step Verification must be on). '
             "Leave empty to keep the current one."},
    {"group": "Requests", "key": "TRUSTED_SENDERS", "label": "Trusted senders", "required": True,
     "help": "Comma-separated email addresses. Only these can request a screenshot, and each screenshot "
             "is emailed back only to the address that asked. Emails from anyone else are ignored."},
    {"group": "Requests", "key": "TRIGGER_SUBJECT", "label": "Request subject", "default": "status",
     "required": True, "help": "A new email with exactly this subject requests a screenshot (ignores case)."},
    {"group": "Requests", "key": "CHECK_INTERVAL", "label": "Check inbox every (seconds)", "type": "number",
     "default": "15", "help": "Minimum 5."},
    {"group": "Advanced", "key": "MAIL_FROM", "label": "From address", "default": "",
     "help": "Empty = the Gmail address."},
    {"group": "Advanced", "key": "SMTP_HOST", "label": "SMTP host", "default": "smtp.gmail.com"},
    {"group": "Advanced", "key": "SMTP_PORT", "label": "SMTP port", "type": "number", "default": "587"},
    {"group": "Advanced", "key": "SMTP_SECURITY", "label": "SMTP security", "default": "starttls",
     "choices": ["starttls", "ssl", "none"]},
    {"group": "Advanced", "key": "IMAP_HOST", "label": "IMAP host", "default": "imap.gmail.com"},
    {"group": "Advanced", "key": "IMAP_PORT", "label": "IMAP port", "type": "number", "default": "993"},
    {"group": "Advanced", "key": "IMAP_FOLDER", "label": "IMAP folder", "default": "INBOX"},
]
KEYS = [f["key"] for f in FIELDS]
SECRETS = {f["key"] for f in FIELDS if f.get("secret")}
EMAIL_RE = re.compile(r"^[^@\s,]+@[^@\s,]+\.[^@\s,]+$")


def load(env, path):
    values = {f["key"]: env.get(f["key"], f.get("default", "")) for f in FIELDS}
    if not values["TRUSTED_SENDERS"]:
        values["TRUSTED_SENDERS"] = env.get("ALLOWED_SENDERS", "")  # older name
    saved = read_file(path)
    values.update({k: str(v) for k, v in saved.items() if k in KEYS})
    return values


def read_file(path):
    try:
        with open(path) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save(path, values):
    """Atomic write, readable only by the server user (it contains the App Password)."""
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".settings-")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump({k: values[k] for k in KEYS}, fh, indent=2)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def merge_update(current, submitted):
    """Apply a form submission. Empty secret fields keep the existing value."""
    new = dict(current)
    for k in KEYS:
        if k not in submitted:
            continue
        v = str(submitted[k]).strip()
        if k in SECRETS:
            v = v.replace(" ", "")  # Google shows App Passwords as "abcd efgh ijkl mnop"
            if not v:
                continue
        if k == "TRUSTED_SENDERS":
            v = ", ".join(dict.fromkeys(a.strip().lower() for a in v.split(",") if a.strip()))
        new[k] = v
    return new


def validate(values):
    errors = {}
    for f in FIELDS:
        if f.get("required") and not values.get(f["key"], "").strip():
            errors[f["key"]] = "Required."
    for k in ("SMTP_USER", "MAIL_FROM"):
        v = values.get(k, "").strip()
        if v and not EMAIL_RE.match(v):
            errors[k] = "Not a valid email address."
    for k in ("TRUSTED_SENDERS",):
        bad = [a for a in (x.strip() for x in values.get(k, "").split(",")) if a and not EMAIL_RE.match(a)]
        if bad:
            errors[k] = f"Not a valid address: {bad[0]}"
    for k, lo, hi in (("SMTP_PORT", 1, 65535), ("IMAP_PORT", 1, 65535), ("CHECK_INTERVAL", 5, 3600)):
        try:
            n = float(values.get(k, ""))
            if not lo <= n <= hi:
                raise ValueError
        except ValueError:
            errors[k] = f"Must be a number from {lo} to {hi}."
    if values.get("SMTP_SECURITY") not in ("starttls", "ssl", "none"):
        errors["SMTP_SECURITY"] = "Choose starttls, ssl or none."
    return errors


def public(values):
    """Values safe to send to the browser: secrets are never sent back."""
    out = {k: ("" if k in SECRETS else v) for k, v in values.items()}
    out.update({f"{k}_set": bool(values.get(k)) for k in SECRETS})
    return out
