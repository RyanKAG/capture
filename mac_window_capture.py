"""Capture a single macOS application window by its exact title."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path


def _windows() -> list[tuple[int, str, bool]]:
    try:
        import Quartz
    except ImportError as exc:
        raise RuntimeError("Install the macOS dependencies in requirements.txt first.") from exc

    if not (Quartz.CGPreflightScreenCaptureAccess() or Quartz.CGRequestScreenCaptureAccess()):
        raise RuntimeError(
            "Allow Screen Recording for the app running Python (such as Terminal) in "
            "System Settings > Privacy & Security > Screen & System Audio Recording. "
            "Then reopen that app and run the command again."
        )
    entries = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionAll | Quartz.kCGWindowListExcludeDesktopElements,
        Quartz.kCGNullWindowID,
    )
    if entries is None:
        raise RuntimeError("Run the script from a signed-in macOS desktop session.")
    windows = []
    for entry in entries:
        title = entry.get(Quartz.kCGWindowName)
        if title and entry.get(Quartz.kCGWindowLayer) == 0:
            window_id = int(entry[Quartz.kCGWindowNumber])
            onscreen = bool(entry.get(Quartz.kCGWindowIsOnscreen, False))
            windows.append((window_id, str(title), onscreen))
    return windows


def list_windows() -> list[str]:
    """List titled application windows on the current desktop."""
    return sorted((title for _, title, onscreen in _windows() if onscreen), key=str.casefold)


def _find_window(title: str) -> int:
    wanted = title.casefold()
    matches = [
        (window_id, onscreen)
        for window_id, name, onscreen in _windows()
        if name.casefold() == wanted
    ]
    if not matches:
        raise RuntimeError(f"No application window has the title {title!r}.")
    if len(matches) > 1:
        raise RuntimeError(
            f"Multiple windows have the title {title!r}. Give the target a unique title."
        )
    window_id, onscreen = matches[0]
    if not onscreen:
        raise RuntimeError(f"Show {title!r} on the current desktop before capturing it.")
    return window_id


def capture_window(title: str) -> bytes:
    """Return a PNG of the unique on-screen window with the given title."""
    window_id = _find_window(title)
    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory) / "window.png"
        try:
            result = subprocess.run(
                ["/usr/sbin/screencapture", "-x", "-o", "-a", "-t", "png",
                 "-l", str(window_id), str(output)],
                capture_output=True, text=True, errors="replace", timeout=30,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("Timed out waiting for the macOS window capture.") from exc
        if result.returncode or not output.is_file():
            reason = result.stderr.strip() or "Check that the window is open on the current desktop."
            raise RuntimeError(f"macOS could not capture {title!r}. {reason}")
        png = output.read_bytes()
    if not png.startswith(b"\x89PNG\r\n\x1a\n"):
        raise RuntimeError("macOS returned an invalid PNG.")
    if _find_window(title) != window_id:
        raise RuntimeError("The selected window changed during capture.")
    return png
