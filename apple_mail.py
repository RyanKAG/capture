"""Submit a screenshot through a configured account in Apple Mail."""

from __future__ import annotations

import subprocess
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory


_SEND_SCRIPT = """\
on run argv
    set wantedSender to item 1 of argv
    set subjectLine to item 2 of argv
    set messageBody to item 3 of argv
    set pngFile to (POSIX file (item 4 of argv)) as alias
    set addresses to items 5 thru -1 of argv

    tell application id "com.apple.mail"
        set matchedAccounts to 0
        set fromLine to wantedSender
        repeat with mailAccount in (every account whose enabled is true)
            if (email addresses of mailAccount) contains wantedSender then
                set matchedAccounts to matchedAccounts + 1
                set accountName to full name of mailAccount
                if accountName is not missing value and accountName is not "" then
                    set fromLine to accountName & " <" & wantedSender & ">"
                end if
            end if
        end repeat
        if matchedAccounts = 0 then error "Add or enable the configured sender in Mail > Accounts."
        if matchedAccounts > 1 then error "More than one enabled Mail account uses this sender."

        set draftMessage to make new outgoing message with properties {visible:false, subject:subjectLine, content:messageBody}
        set submitting to false
        try
            set sender of draftMessage to fromLine
            tell draftMessage
                repeat with recipientAddress in addresses
                    make new to recipient at end of to recipients with properties {address:(contents of recipientAddress)}
                end repeat
                tell content to make new attachment at after last paragraph with properties {file name:pngFile}
            end tell
            if (extract address from (sender of draftMessage)) is not wantedSender then error "Apple Mail selected a different sender."
            set submitting to true
            if not (send draftMessage) then
                set submitting to false
                error "Apple Mail declined to send the screenshot."
            end if
        on error errorText number errorNumber
            if not submitting then
                try
                    delete draftMessage
                end try
            end if
            error errorText number errorNumber
        end try
    end tell
    return "submitted"
end run
"""


def send_via_apple_mail(
    sender: str, recipients: tuple[str, ...], title: str, captured_at: datetime, png: bytes
) -> None:
    """Return after Mail accepts the outgoing message for delivery."""
    if not recipients:
        raise ValueError("Set at least one recipient for Apple Mail.")
    captured_at = captured_at.astimezone()
    subject = f"Window screenshot — {captured_at:%Y-%m-%d %H:%M %z}"
    body = f"Window: {title}\nCaptured: {captured_at.isoformat(timespec='seconds')}\n"
    with TemporaryDirectory() as directory:
        attachment = Path(directory) / f"screenshot-{captured_at:%Y%m%d-%H%M%S}.png"
        attachment.write_bytes(png)
        try:
            result = subprocess.run(
                ["/usr/bin/osascript", "-", sender, subject, body, str(attachment), *recipients],
                input=_SEND_SCRIPT, capture_output=True, encoding="utf-8", timeout=90,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("Apple Mail timed out. Check its Outbox and Sent mailbox.") from exc
    if result.returncode:
        detail = result.stderr.strip()
        if "-1743" in detail:
            raise RuntimeError("Allow the app running Python to control Mail in System Settings > Privacy & Security > Automation.")
        raise RuntimeError(detail or "Apple Mail could not submit the message.")
    if result.stdout.strip() != "submitted":
        raise RuntimeError("Apple Mail did not confirm submission. Check its Outbox and Sent mailbox.")
