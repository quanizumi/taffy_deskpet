"""Pin a top-level window to the running Cursor desktop window.

Coordinates are Windows physical pixels from GetWindowRect / SetWindowPos.
The pet is kept directly above Cursor and is not topmost over other apps.
"""

from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import ctypes
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)

GW_OWNER = 4
GW_HWNDPREV = 3
DWMWA_CLOAKED = 14
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SWP_NOSIZE = 0x0001
SWP_NOACTIVATE = 0x0010
SWP_NOZORDER = 0x0004
HWND_TOP = wintypes.HWND(0)

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
user32.EnumWindows.restype = wintypes.BOOL
user32.IsWindow.argtypes = [wintypes.HWND]
user32.IsWindow.restype = wintypes.BOOL
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL
user32.IsIconic.argtypes = [wintypes.HWND]
user32.IsIconic.restype = wintypes.BOOL
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetWindow.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowRect.restype = wintypes.BOOL
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.SetWindowPos.argtypes = [
    wintypes.HWND,
    wintypes.HWND,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.UINT,
]
user32.SetWindowPos.restype = wintypes.BOOL
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.LPWSTR,
    ctypes.POINTER(wintypes.DWORD),
]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL

dwmapi.DwmGetWindowAttribute.argtypes = [
    wintypes.HWND,
    wintypes.DWORD,
    ctypes.c_void_p,
    wintypes.DWORD,
]
dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long

# pid -> is Cursor.exe. Cleared every few seconds so a recycled pid cannot stick.
_pid_cache: dict[int, bool] = {}
_pid_cache_until = 0.0


@dataclass(frozen=True)
class HostWindow:
    """One visible top-level Cursor frame, in physical pixels."""

    hwnd: int
    left: int
    top: int
    right: int
    bottom: int

    @property
    def area(self) -> int:
        return max(0, self.right - self.left) * max(0, self.bottom - self.top)


def find_cursor_host(prefer: int = 0, exclude: int = 0) -> HostWindow | None:
    """Return the Cursor window the pet should follow.

    Only the Agents window counts. The IDE frame is ignored, so the pet hides
    while that window is the one on screen. Prefers the foreground Agents
    window, then `prefer` if it is still open, then the largest one.
    """

    found: list[HostWindow] = []

    def _visit(hwnd: int, _lparam: int) -> bool:
        if exclude and int(hwnd) == exclude:
            return True
        host = _as_cursor_host(int(hwnd))
        if host is not None:
            found.append(host)
        return True

    callback = WNDENUMPROC(_visit)
    user32.EnumWindows(callback, 0)
    if not found:
        return None
    foreground = int(user32.GetForegroundWindow() or 0)
    for host in found:
        if host.hwnd == foreground:
            return host
    if prefer:
        for host in found:
            if host.hwnd == prefer:
                return host
    return max(found, key=lambda item: item.area)


def place_above(widget_hwnd: int, host_hwnd: int, x: int, y: int) -> None:
    """Move `widget_hwnd` to (x, y) and keep it directly above `host_hwnd`.

    This does not make the widget an owned window. Windows destroys an owned
    window when its owner closes, which would take the pet down with Cursor.
    Slotting just above the host also keeps the pet under whatever app is
    actually in front.
    """

    if not widget_hwnd or not user32.IsWindow(widget_hwnd) or not user32.IsWindow(host_hwnd):
        return
    prev = int(user32.GetWindow(host_hwnd, GW_HWNDPREV) or 0)
    flags = SWP_NOSIZE | SWP_NOACTIVATE
    if prev == int(widget_hwnd):
        flags |= SWP_NOZORDER
        insert_after = HWND_TOP
        current = window_rect(widget_hwnd)
        if current is not None and current[0] == x and current[1] == y:
            return
    else:
        insert_after = HWND_TOP if prev == 0 else wintypes.HWND(prev)
    user32.SetWindowPos(widget_hwnd, insert_after, int(x), int(y), 0, 0, flags)


def window_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    """Physical rectangle of `hwnd`, or None when the window is gone."""

    if not hwnd or not user32.IsWindow(hwnd):
        return None
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    return rect.left, rect.top, rect.right, rect.bottom


def login_autostart_enabled() -> bool:
    """True when the current-user Startup folder has this pet's shortcut."""

    return _startup_link().is_file()


def set_login_autostart(enabled: bool) -> None:
    """Create or remove the Startup shortcut that waits for Cursor.

    Uses the interpreter that launched this process. No admin rights.
    """

    link = _startup_link()
    if not enabled:
        link.unlink(missing_ok=True)
        return
    target, arguments, workdir = _launch_command()
    command = (
        "$shell = New-Object -ComObject WScript.Shell; "
        f"$shortcut = $shell.CreateShortcut('{_ps(str(link))}'); "
        f"$shortcut.TargetPath = '{_ps(target)}'; "
        f"$shortcut.Arguments = '{_ps(arguments)}'; "
        f"$shortcut.WorkingDirectory = '{_ps(workdir)}'; "
        "$shortcut.WindowStyle = 7; "
        "$shortcut.Description = '永雏塔菲'; "
        "$shortcut.Save()"
    )
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        check=True,
        creationflags=flags,
    )


def _startup_link() -> Path:
    appdata = Path.home() / "AppData" / "Roaming"
    return appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "永雏塔菲.lnk"


def _launch_command() -> tuple[str, str, str]:
    """Program, arguments, and working directory for the Startup shortcut."""

    if getattr(sys, "frozen", False):
        exe = Path(sys.executable)
        return str(exe), "", str(exe.parent)
    exe = Path(sys.executable)
    if exe.name.lower() == "python.exe":
        pyw = exe.with_name("pythonw.exe")
        if pyw.is_file():
            exe = pyw
    script = Path(sys.argv[0]).resolve()
    workdir = script.parent.parent if script.parent.name == "src" else script.parent
    return str(exe), f'"{script}"', str(workdir)


def _ps(value: str) -> str:
    """Escape a string for a PowerShell single-quoted literal."""

    return value.replace("'", "''")


def _is_agent_window_title(title: str) -> bool:
    """True for Cursor's Agents window, false for the IDE editor frame.

    The Agents window title is "Cursor Agents". The IDE title names the open
    file and ends with "Cursor", so it does not match.
    """

    return title.strip().endswith("Cursor Agents")


def _window_title(hwnd: int) -> str:
    """Visible title bar text. Empty when the window has none."""

    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def _as_cursor_host(hwnd: int) -> HostWindow | None:
    """A visible unowned Agents window. IDE frames return None."""

    if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
        return None
    if user32.GetWindow(hwnd, GW_OWNER):
        return None
    cloaked = wintypes.DWORD()
    hr = dwmapi.DwmGetWindowAttribute(
        hwnd,
        DWMWA_CLOAKED,
        ctypes.byref(cloaked),
        ctypes.sizeof(cloaked),
    )
    if hr == 0 and cloaked.value:
        return None
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value or not _is_cursor_process(int(pid.value)):
        return None
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    width = rect.right - rect.left
    height = rect.bottom - rect.top
    if width < 480 or height < 320:
        return None
    if not _is_agent_window_title(_window_title(hwnd)):
        return None
    return HostWindow(hwnd, rect.left, rect.top, rect.right, rect.bottom)


def _is_cursor_process(pid: int) -> bool:
    """True when `pid` is the Cursor desktop executable."""

    global _pid_cache_until
    now = time.monotonic()
    if now >= _pid_cache_until:
        _pid_cache.clear()
        _pid_cache_until = now + 5.0
    cached = _pid_cache.get(pid)
    if cached is not None:
        return cached
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        _pid_cache[pid] = False
        return False
    try:
        size = wintypes.DWORD(32768)
        buf = ctypes.create_unicode_buffer(32768)
        ok = kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size))
        hit = bool(ok) and buf.value.lower().endswith("\\cursor.exe")
    finally:
        kernel32.CloseHandle(handle)
    _pid_cache[pid] = hit
    return hit
