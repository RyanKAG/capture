"""Queue a screenshot through a signed-in account in classic Outlook for Windows."""

from contextlib import suppress
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory


def send_via_outlook(
    sender: str, recipients: tuple[str, ...], title: str, captured_at: datetime, png: bytes
) -> None:
    """Queue one PNG attachment with the Outlook account matching the sender."""
    try:
        import pythoncom
        from win32com.client import Dispatch
    except ImportError as exc:
        raise RuntimeError("Install the Windows dependencies in requirements.txt first.") from exc

    try:
        outlook = Dispatch("Outlook.Application")
    except pythoncom.com_error as exc:
        raise RuntimeError(
            "Open classic Outlook for Windows and sign in to your sending account. "
            "This integration requires the classic Outlook desktop app."
        ) from exc

    wanted = sender.casefold()
    try:
        accounts = [
            account for account in outlook.Session.Accounts
            if (account.SmtpAddress or "").casefold() == wanted
        ]
        if len(accounts) != 1:
            raise RuntimeError(f"Expected one classic Outlook account for {sender}. Found {len(accounts)}.")
        account = accounts[0]
        store = account.DeliveryStore
        drafts = store.GetDefaultFolder(16) if store is not None else None  # olFolderDrafts
        if drafts is None:
            raise RuntimeError(f"Outlook could not open the Drafts folder for {sender}.")
        item = drafts.Items.Add("IPM.Note")
        try:
            # Outlook's setter takes a COM object reference, including with late binding.
            dispatch = item._oleobj_
            prop = dispatch.GetIDsOfNames("SendUsingAccount")
            dispatch.Invoke(prop, 0, pythoncom.DISPATCH_PROPERTYPUTREF, False, account._oleobj_)
            captured_at = captured_at.astimezone()
            item.Subject = f"Window screenshot — {captured_at:%Y-%m-%d %H:%M %z}"
            item.Body = f"Window: {title}\r\nCaptured: {captured_at.isoformat(timespec='seconds')}\r\n"
            for address in recipients:
                item.Recipients.Add(address)
            if not item.Recipients.ResolveAll():
                raise RuntimeError("Outlook could not resolve all recipient addresses.")
            item.Save()
            with TemporaryDirectory() as directory:
                path = Path(directory, f"screenshot-{captured_at:%Y%m%d-%H%M%S}.png")
                path.write_bytes(png)
                item.Attachments.Add(str(path), 1)  # olByValue embeds a copy
                item.Save()
            selected = item.SendUsingAccount
            if selected is None or (selected.SmtpAddress or "").casefold() != wanted:
                raise RuntimeError(f"Outlook did not select {sender} as the sending account.")
        except (pythoncom.com_error, RuntimeError, OSError):
            with suppress(pythoncom.com_error):
                item.Delete()
            raise
        # A failure after submission may be ambiguous. Leave Outlook to manage its queue.
        item.Send()
    except pythoncom.com_error as exc:
        raise RuntimeError(f"Classic Outlook could not complete the send. Check its Outbox. {exc}") from exc
