"""Submit a window screenshot through a desktop mail app or SMTP."""

from __future__ import annotations

import configparser
import getpass
import os
import smtplib
import ssl
import sys
from dataclasses import dataclass, field
from datetime import datetime
from email.errors import HeaderParseError
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from pathlib import Path


@dataclass(frozen=True)
class MailAppSettings:
    sender: str
    recipients: tuple[str, ...]


@dataclass(frozen=True)
class MailSettings:
    host: str
    port: int
    security: str
    sender: str
    recipients: tuple[str, ...]
    username: str
    password: str = field(repr=False)
    oauth_token_file: Path | None


def _address(value: str) -> str:
    try:
        address = Address(addr_spec=value.strip())
        if address.username and address.domain:
            return address.addr_spec
    except (ValueError, HeaderParseError, IndexError):
        pass
    raise ValueError("Use a complete email address, such as name@example.com, in [email].")


def load_mail_settings(
    config: configparser.ConfigParser, *, use_password_env: bool = True
) -> MailSettings | MailAppSettings:
    if not config.has_section("email"):
        raise ValueError("Add an [email] section to the configuration.")
    email = config["email"]
    sender = _address(email.get("sender", ""))
    recipients = tuple(_address(item) for item in email.get("recipients", "").split(","))
    transport = email.get("transport", "smtp").strip().lower()
    if transport == "mail_app":
        return MailAppSettings(sender, recipients)
    if transport != "smtp":
        raise ValueError("Set [email] transport to mail_app or smtp.")

    host = email.get("host", "").strip()
    if not host or any(char.isspace() for char in host):
        raise ValueError("Set [email] host to your provider's SMTP hostname.")
    security = email.get("security", "starttls").strip().lower()
    if security not in {"starttls", "ssl"}:
        raise ValueError("Set [email] security to starttls or ssl.")
    port = email.getint("port", fallback=465 if security == "ssl" else 587)
    if not 1 <= port <= 65535:
        raise ValueError("Set [email] port to a number from 1 through 65535.")

    username = email.get("username", "").strip()
    token_path = email.get("oauth_token_file", "").strip()
    password = ""
    if token_path and not username:
        raise ValueError("Set [email] username when using an OAuth token.")
    if username and not token_path:
        if use_password_env:
            password = os.environ.get("SCREENSHOT_SMTP_PASSWORD", "")
        if not password:
            if not sys.stdin or not sys.stdin.isatty():
                if not use_password_env:
                    raise ValueError("Open Terminal to run the interactive Gmail test.")
                raise ValueError("Set SCREENSHOT_SMTP_PASSWORD for unattended SMTP login.")
            prompt = "Google app password: " if host.lower() == "smtp.gmail.com" else "SMTP password or app password: "
            password = getpass.getpass(prompt)
        if not password:
            raise ValueError("An SMTP password is required when a username is configured.")

    return MailSettings(
        host, port, security, sender, recipients, username, password,
        Path(token_path).expanduser() if token_path else None,
    )


def screenshot_message(
    sender: str, recipients: tuple[str, ...], title: str, captured_at: datetime, png: bytes
) -> EmailMessage:
    captured_at = captured_at.astimezone()
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = f"Window screenshot — {captured_at:%Y-%m-%d %H:%M %z}"
    msg["Date"] = format_datetime(captured_at)
    msg["Message-ID"] = make_msgid()
    msg.set_content(
        f"Window: {title}\nCaptured: {captured_at.isoformat(timespec='seconds')}\n",
        cte="quoted-printable",
    )
    msg.add_attachment(
        png, maintype="image", subtype="png",
        filename=f"screenshot-{captured_at:%Y%m%d-%H%M%S}.png",
    )
    return msg


def send_screenshot(settings: MailSettings | MailAppSettings, title: str, captured_at: datetime, png: bytes) -> None:
    if isinstance(settings, MailAppSettings):
        if sys.platform == "darwin":
            from apple_mail import send_via_apple_mail as submit
        elif sys.platform == "win32":
            from outlook_mail import send_via_outlook as submit
        else:
            raise RuntimeError("Desktop mail delivery requires Apple Mail on macOS or classic Outlook on Windows.")
        submit(settings.sender, settings.recipients, title, captured_at.astimezone(), png)
        return

    msg = screenshot_message(settings.sender, settings.recipients, title, captured_at, png)
    context = ssl.create_default_context()
    if settings.security == "ssl":
        connection = smtplib.SMTP_SSL(settings.host, settings.port, timeout=30, context=context)
    else:
        connection = smtplib.SMTP(settings.host, settings.port, timeout=30)
    with connection as smtp:
        if settings.security == "starttls":
            smtp.starttls(context=context)
        smtp.ehlo()
        if settings.oauth_token_file:
            token = settings.oauth_token_file.read_text(encoding="utf-8-sig").strip()
            if not token or not token.isascii() or not token.isprintable() or " " in token:
                raise ValueError("The OAuth token file must contain a single access token.")
            credentials = f"user={settings.username}\x01auth=Bearer {token}\x01\x01"
            # An XOAUTH2 error challenge requires an empty response.
            smtp.auth("XOAUTH2", lambda challenge=None: credentials if challenge is None else "")
        elif settings.username:
            smtp.login(settings.username, settings.password)
        refused = smtp.send_message(msg, from_addr=settings.sender, to_addrs=settings.recipients)
        if refused:
            raise smtplib.SMTPException(
                "SMTP accepted the message for some recipients. Refused: " + ", ".join(refused)
            )
