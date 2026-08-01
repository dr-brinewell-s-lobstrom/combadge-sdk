#!/usr/bin/env python3
"""Vibe Control — target session detection and latch resolution (TOS SDK).

Windows only.  Component of TOS, subject to <LICENSE.md>.

Two independent signals, because counting sessions and targeting one are
different jobs (see sdk/VIBE.md -> Detecting the candidates):

  COUNT   claude.exe process enumeration.  One process per session.  Immune
          to window titles, /rename, and Claude's frequent retitling.  Ground
          truth.
  TARGET  window enumeration by class + title pattern.  Only a window can be
          focused and typed into, so this leg is unavoidably heuristic.
  CHECK   the two counts must agree.  A title rule that has silently stopped
          matching would otherwise let us latch onto one session while a
          second one exists unseen — precisely the mistarget the gate exists
          to stop.

Pure stdlib: ctypes against user32/kernel32.  No third-party dependencies —
consistent with the rest of the SDK, which asks only for vosk (server) and
evdev (relay).

Read-only.  This module never injects input and never arms anything; it only
reports what it sees.  vibekeys.py decides what to do about it.

Although the defaults target Claude Code in Windows Terminal, nothing here is
specific to it: point SDK_VIBE_TARGET_PROCESS / _CLASS / _TITLE_RE at any
process and window and the same gate applies.

CLI:  python vibewin.py     — diagnostic report, exit 0 only if exactly one
"""

import ctypes
import os
import re
import sys

if sys.platform != "win32":
    raise ImportError(
        "vibewin is Windows-only: it drives a local window through the Win32 "
        "SendInput API.  computer.py imports it defensively and simply omits "
        "the Vibe Control commands on other platforms."
    )

import ctypes.wintypes as w


def _cfg(env_key, fallback):
    """Configuration value, from the environment or the documented default."""
    return os.environ.get(env_key, fallback)


# --- Configuration (see sdk/VIBE.md -> Configuration) ---

TARGET_PROCESS = _cfg("SDK_VIBE_TARGET_PROCESS", "claude.exe").lower()
TARGET_CLASS = _cfg("SDK_VIBE_TARGET_CLASS", "CASCADIA_HOSTING_WINDOW_CLASS")
# Matches either a status-glyph prefix (Claude Code shows an animated Braille
# spinner U+2800-U+28FF while busy and U+2733 while idle) or a freshly
# launched, un-renamed session.  Measured 2026-07-27; re-verify after a
# Claude Code upgrade — the title format is empirical, not contractual.
TARGET_TITLE_RE = _cfg("SDK_VIBE_TARGET_TITLE_RE", r"^([^\x00-\x7F]\s|claude( code)?$)")
REQUIRE_AGREEMENT = _cfg("SDK_VIBE_REQUIRE_AGREEMENT", "true").strip().lower() == "true"
NAME_MAX_WORDS = int(_cfg("SDK_VIBE_NAME_MAX_WORDS", "6"))
UNNAMED = "an unnamed session"

_title_re = re.compile(TARGET_TITLE_RE, re.IGNORECASE)

# Titles that identify a session but carry no useful name to speak back.
_ANONYMOUS_TITLES = {"", "claude", "claude code"}


# --- Win32 ---

_u32 = ctypes.WinDLL("user32", use_last_error=True)
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)

_EnumWindowsProc = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
_u32.EnumWindows.argtypes = [_EnumWindowsProc, w.LPARAM]
_u32.GetWindowTextLengthW.argtypes = [w.HWND]
_u32.GetWindowTextW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
_u32.GetClassNameW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
_u32.IsWindowVisible.argtypes = [w.HWND]
_u32.IsWindow.argtypes = [w.HWND]
_u32.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
_u32.GetForegroundWindow.restype = w.HWND

TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", w.DWORD),
        ("cntUsage", w.DWORD),
        ("th32ProcessID", w.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", w.DWORD),
        ("cntThreads", w.DWORD),
        ("th32ParentProcessID", w.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", w.DWORD),
        ("szExeFile", w.WCHAR * 260),
    ]


_k32.CreateToolhelp32Snapshot.argtypes = [w.DWORD, w.DWORD]
_k32.CreateToolhelp32Snapshot.restype = w.HANDLE
_k32.Process32FirstW.argtypes = [w.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
_k32.Process32NextW.argtypes = [w.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
_k32.CloseHandle.argtypes = [w.HANDLE]


def session_processes():
    """PIDs of every running target session process.

    Toolhelp snapshot rather than a PowerShell subprocess: this runs on the
    activation path, which is already inside a badge tap's latency budget,
    and a ~300 ms shell spawn is not worth it.
    """
    pids = []
    snap = _k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == INVALID_HANDLE_VALUE:
        return pids
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = _k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            if entry.szExeFile.lower() == TARGET_PROCESS:
                pids.append(entry.th32ProcessID)
            ok = _k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        _k32.CloseHandle(snap)
    return pids


def _window_info(hwnd):
    # Accept either form of handle.  EnumWindows hands its callback a plain
    # int; window_matches() passes a ctypes HWND (a c_void_p).  int() on the
    # latter tries to parse the object's raw bytes and raises, so normalize
    # first.  (This bug shipped once in TOS and broke EVERY re-validation.)
    hwnd_int = hwnd.value if hasattr(hwnd, "value") else int(hwnd)
    n = _u32.GetWindowTextLengthW(hwnd)
    tbuf = ctypes.create_unicode_buffer(n + 1)
    _u32.GetWindowTextW(hwnd, tbuf, n + 1)
    cbuf = ctypes.create_unicode_buffer(256)
    _u32.GetClassNameW(hwnd, cbuf, 256)
    pid = w.DWORD()
    _u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return {"hwnd": hwnd_int, "title": tbuf.value,
            "cls": cbuf.value, "pid": int(pid.value)}


def terminal_windows():
    """Every visible window of the configured terminal class."""
    out = []

    def cb(hwnd, _):
        if not _u32.IsWindowVisible(hwnd):
            return True
        info = _window_info(hwnd)
        if info["cls"] == TARGET_CLASS and info["title"]:
            out.append(info)
        return True

    _u32.EnumWindows(_EnumWindowsProc(cb), 0)
    return out


def candidate_windows():
    """Terminal windows whose title matches the target pattern.

    Both class and title must match.  The class filter is what keeps a browser
    tab titled '... Claude ...' out of the results — the title rule alone
    would accept it (measured: an open claude.ai tab in Edge), and does not
    get to decide on its own.
    """
    return [i for i in terminal_windows() if _title_re.search(i["title"])]


def window_matches(hwnd):
    """Re-validate a stored HWND: still alive, still a target window?

    Pattern check, never a title-equality check — the spinner animates, Claude
    retitles as work proceeds, and even a /rename can vanish on its own
    (observed 2026-07-27).  An equality check would fail within the second.
    """
    if not hwnd or not _u32.IsWindow(w.HWND(hwnd)):
        return False
    info = _window_info(w.HWND(hwnd))
    return info["cls"] == TARGET_CLASS and bool(_title_re.search(info["title"]))


def speakable_name(title):
    """Session name to speak back at latch, or None if there isn't a useful one.

    Strips the leading status glyph, drops characters TTS engines mangle, and
    caps the length.  'claude' / 'Claude Code' are treated as anonymous: they
    identify a session but tell the user nothing about *which* one.
    """
    if not title:
        return None
    name = title
    # Drop a leading non-ASCII status glyph and its separating space.
    if ord(name[0]) > 0x7F:
        name = name[1:]
    name = re.sub(r"[^\w\s'-]", " ", name, flags=re.UNICODE)
    name = " ".join(name.split())
    if name.lower() in _ANONYMOUS_TITLES:
        return None
    words = name.split()
    if len(words) > NAME_MAX_WORDS:
        words = words[:NAME_MAX_WORDS]
    name = " ".join(words)
    return name or None


def resolve():
    """Decide whether Vibe Control may latch, and to what.

    Returns a dict with:
      status  'ok' | 'none' | 'multiple' | 'inconsistent'
      hwnd/title/name  populated only when status == 'ok'
      windows, pids    always, for the log
    Never has side effects.  vibekeys.py arms itself only on 'ok'.
    """
    wins = candidate_windows()
    pids = session_processes()
    res = {"windows": wins, "pids": pids,
           "hwnd": None, "title": None, "name": None}

    # Count disagreement outranks everything: if the window rule and the
    # process count tell different stories, we do not know what is running,
    # and latching on the window we happened to match is exactly the
    # mistarget to avoid.
    if REQUIRE_AGREEMENT and len(wins) != len(pids):
        res["status"] = "inconsistent"
        return res
    if len(pids) == 0 and len(wins) == 0:
        res["status"] = "none"
        return res
    if len(wins) > 1 or len(pids) > 1:
        res["status"] = "multiple"
        return res
    if len(wins) != 1:
        res["status"] = "none"
        return res

    win = wins[0]
    res.update(status="ok", hwnd=win["hwnd"], title=win["title"],
               name=speakable_name(win["title"]))
    return res


def _report():
    r = resolve()
    # `or 0` because ctypes maps a NULL HWND to None, and NULL is a legitimate
    # result — no foreground window exists during activation changes or while
    # the interactive desktop belongs to the screensaver / lock screen.
    # Without this, the report crashes on a blank screen: precisely when it is
    # wanted.
    fg = _u32.GetForegroundWindow() or 0
    print(f"target process : {TARGET_PROCESS}")
    print(f"target class   : {TARGET_CLASS}")
    print(f"title pattern  : {TARGET_TITLE_RE}")
    print()
    all_terms = terminal_windows()
    print(f"terminal windows of class {TARGET_CLASS}: {len(all_terms)}")
    for i in all_terms:
        hit = "MATCH " if _title_re.search(i["title"]) else "   -  "
        mark = " *FG*" if i["hwnd"] == fg else ""
        print(f"  {hit} hwnd={i['hwnd']:<10} pid={i['pid']:<7} {i['title']!r}{mark}")
    print()
    print(f"{TARGET_PROCESS} processes: {len(r['pids'])}  {r['pids']}")
    print(f"candidate windows : {len(r['windows'])}")
    print()
    print(f"STATUS: {r['status']}")
    if r["status"] == "ok":
        print(f"  hwnd  : {r['hwnd']}")
        print(f"  title : {r['title']!r}")
        print(f"  speaks: {r['name'] or UNNAMED}")
    return 0 if r["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(_report())
