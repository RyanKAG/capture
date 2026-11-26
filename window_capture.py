"""Capture one explicitly selected Windows window into an in-memory PNG."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def _require_windows() -> None:
    if sys.platform != "win32":
        raise RuntimeError("Window capture requires Windows.")


def _windows() -> list[tuple[int, str, bool]]:
    _require_windows()
    try:
        import pywintypes
        import win32gui
    except ImportError as exc:
        raise RuntimeError("Install the dependencies in requirements.txt first.") from exc

    windows = []

    def collect(hwnd, _):
        try:
            if win32gui.IsWindowVisible(hwnd):
                title = win32gui.GetWindowText(hwnd)
                if title:
                    windows.append((hwnd, title, bool(win32gui.IsIconic(hwnd))))
        except pywintypes.error:
            # A window may close while its title is being read.
            if win32gui.IsWindow(hwnd):
                raise
        return True

    win32gui.EnumWindows(collect, None)
    return windows


def list_windows() -> list[str]:
    """List visible, titled top-level windows, including minimized windows."""
    return sorted((title for _, title, _ in _windows()), key=str.casefold)


def _find_window(title: str) -> int:
    wanted = title.casefold()
    matches = [
        (hwnd, minimized)
        for hwnd, name, minimized in _windows()
        if name.casefold() == wanted
    ]
    if not matches:
        raise RuntimeError(f"No visible window has the title {title!r}.")
    if len(matches) > 1:
        raise RuntimeError(
            f"Multiple windows have the title {title!r}. Give the target a unique title."
        )
    hwnd, minimized = matches[0]
    if minimized:
        raise RuntimeError(f"Restore the minimized window {title!r} before capturing it.")
    return hwnd


def _capture_frame(title: str) -> bytes:
    hwnd = _find_window(title)
    try:
        import cv2
        from windows_capture import WindowsCapture
    except ImportError as exc:
        raise RuntimeError("Install the dependencies in requirements.txt first.") from exc

    capture = WindowsCapture(window_hwnd=hwnd, cursor_capture=None)
    png = b""

    @capture.event
    def on_frame_arrived(frame, capture_control):
        nonlocal png
        pixels = frame.convert_to_bgr().frame_buffer
        success, encoded = cv2.imencode(".png", pixels)
        if not success:
            raise RuntimeError("Could not encode the window as a PNG.")
        png = encoded.tobytes()
        capture_control.stop()

    @capture.event
    def on_closed():
        pass

    capture.start()
    if not png:
        raise RuntimeError("The capture session closed before Windows returned a frame.")
    if _find_window(title) != hwnd:
        raise RuntimeError("The selected window changed during capture.")
    return png


def capture_window(title: str) -> bytes:
    """Return a PNG for the unique visible, restored window with this exact title."""
    _require_windows()
    # Windows may stop supplying frames while the workstation is locked. Bound
    # the native capture call so a scheduled run can try again at the next slot.
    try:
        result = subprocess.run(
            [sys.executable, "-X", "utf8", str(Path(__file__).resolve()), title],
            capture_output=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            "Timed out waiting for a window frame. Check the interactive Windows session."
        ) from exc

    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(detail or f"Windows capture exited with code {result.returncode}.")
    if not result.stdout.startswith(b"\x89PNG\r\n\x1a\n"):
        raise RuntimeError("The capture process returned an invalid PNG.")
    return result.stdout


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("Expected one window title.")
    try:
        sys.stdout.buffer.write(_capture_frame(sys.argv[1]))
    except Exception as exc:
        sys.exit(str(exc))
