#!/usr/bin/env python3
"""Vibe Control — keystroke and clipboard injection (TOS SDK).

Windows only.  Component of TOS, subject to <LICENSE.md>.

Drive a terminal session on the server machine by voice, from across the room:
tap the badge, say "computer proceed", and an Enter lands in the window this
module is latched to.  The defaults target Claude Code, but nothing here knows
what is running in the window — it sends Enter, Escape, Right Arrow, digits and
pasted text at whatever the target rule matches.

Pure stdlib ctypes against user32/kernel32, matching the SDK's no-dependency
posture.  These APIs have been stable since Windows 2000.

STATE IS IN-PROCESS.  Whether the mode is armed, and which window it is
latched to, live in module globals owned here and driven by computer.py —
there is no flag file, no lock file, and no supervisor process to keep alive.
The server is already one long-lived process; anything else would be
machinery for its own sake.  A CONSEQUENCE WORTH NAMING: running this file
standalone can inject nothing at all, because a fresh interpreter starts
disarmed and unlatched.  That is a guardrail other designs have to build
deliberately, and here it comes for free.

GUARDRAILS (all load-bearing — see sdk/VIBE.md -> Guardrails):

  * Refuses to inject unless activate() has succeeded.  Inert until armed.
  * Only explicit actions are reachable: Enter, Escape, Right Arrow (only as
    half of the fixed continue pair), digits 1-9, Ctrl+V.  There is
    deliberately no "send arbitrary keys" entry point.
  * The target HWND is re-validated as a live session window before every
    send, and focus is VERIFIED after being requested.  If focus cannot be
    confirmed the send is ABORTED, never redirected.  This is what stops a
    stray Enter from landing in some other window.
  * One injection at a time (_send_lock): connection handlers run on their own
    threads, and two overlapping sends would fight over the foreground window.

  EXCEPTION — wake() is exempt from the armed check and the focus check,
  because it must work when neither can be satisfied: outside Vibe Control,
  and while a screensaver holds the desktop.  It is allowed to be exempt only
  because its payload (a bare Shift, plus a net-zero mouse nudge) cannot alter
  any application's state wherever it lands.  See wake() for the full
  argument.  Do not extend this exemption to any key that types, submits, or
  cancels.

CLI:  python vibekeys.py report    — window/latch diagnostic (never injects)
"""

import ctypes
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

if sys.platform != "win32":
    raise ImportError(
        "vibekeys is Windows-only: it drives a local window through the Win32 "
        "SendInput API.  computer.py imports it defensively and simply omits "
        "the Vibe Control commands on other platforms."
    )

import ctypes.wintypes as w

import vibewin


def _gap_s(env_key, default_ms):
    try:
        return float(os.environ.get(env_key, default_ms)) / 1000.0
    except ValueError:
        return float(default_ms) / 1000.0


ESCAPE_GAP_S = _gap_s("SDK_VIBE_ESCAPE_GAP_MS", "80")
CONTINUE_GAP_S = _gap_s("SDK_VIBE_CONTINUE_GAP_MS", "80")

_u32 = ctypes.WinDLL("user32", use_last_error=True)
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)

VK_RETURN, VK_ESCAPE, VK_CONTROL, VK_V = 0x0D, 0x1B, 0x11, 0x56
VK_SHIFT = 0x10
VK_RIGHT = 0x27                  # extended key -- see _key_events(extended=)
VK_1 = 0x31                      # VK_1..VK_9 are contiguous

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008
INPUT_KEYBOARD = 1
INPUT_MOUSE = 0
MOUSEEVENTF_MOVE = 0x0001
MAPVK_VK_TO_VSC = 0

SW_RESTORE = 9
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002


# ---------------------------------------------------------------------------
# Mode state
#
# _active   armed or not.  Nothing injects while False.
# _target   the latched HWND.  Bound at activate(), re-validated on every send.
#
# _state_lock guards both: computer.py serves each badge connection on its own
# thread, so two badges can reach activate()/deactivate() concurrently.
# _send_lock serializes the injections themselves — overlapping sends would
# race each other for the foreground window.
# ---------------------------------------------------------------------------

_active = False
_target = None
_state_lock = threading.Lock()
_send_lock = threading.Lock()


def _log(msg):
    print(f"[vibe] {msg}")


def is_active():
    """True while Vibe Control is armed.  computer.py swaps its vocabulary on
    this — see VIBE_COMMANDS there."""
    with _state_lock:
        return _active


def activate():
    """Run the single-instance gate and latch onto the one session found.

    Returns the phrase to speak — this ALWAYS produces a spoken outcome, so
    the user is never left guessing whether the mode came up.  On refusal
    nothing is armed, nothing is latched, and the normal vocabulary stays
    live: the situation is resolved by closing a window and saying the
    activation phrase again, with no cleanup in between.
    """
    global _active, _target
    r = vibewin.resolve()
    if r["status"] != "ok":
        for win in r["windows"]:
            _log(f"  candidate hwnd={win['hwnd']} pid={win['pid']} {win['title']!r}")
        _log(f"activation REFUSED: {r['status']} "
             f"(windows={len(r['windows'])} processes={len(r['pids'])})")
        return {
            "none": "No session detected. Vibe control not activated.",
            "multiple": "Multiple sessions detected. Vibe control not activated.",
            "inconsistent": "Session detection inconsistent. "
                            "Vibe control not activated.",
        }[r["status"]]

    with _state_lock:
        _target = r["hwnd"]
        _active = True
    name = r["name"] or vibewin.UNNAMED
    _log(f"LATCHED hwnd={r['hwnd']} title={r['title']!r} speaks={name!r}")
    return f"Vibe control latched to {name}. Ready."


def deactivate():
    """Disarm and unlatch.  Returns the phrase to speak."""
    global _active, _target
    with _state_lock:
        _active = False
        _target = None
    _log("released")
    return "Vibe control released."


def target_hwnd():
    """The latched HWND, re-validated as a live session window.

    Returns None if the stored handle is gone and cannot be rebound — callers
    must treat that as 'do not send', NEVER as 'send somewhere else'.  Falling
    back to whatever happens to be focused at send time is how a stray Enter
    lands in the wrong application.

    Re-latch: if the bound window has died (the terminal was restarted) and
    there is now exactly one candidate, rebind to it.  Zero or several, and we
    decline and keep waiting — refusing to guess, exactly as at activation.
    Checking lazily here, at send time, is why no background re-validation
    thread is needed: nothing can be injected without passing through this
    function first.
    """
    global _target
    with _state_lock:
        hwnd = _target
    if hwnd is not None and vibewin.window_matches(hwnd):
        return hwnd

    r = vibewin.resolve()
    if r["status"] != "ok":
        _log(f"target window lost and cannot re-latch: {r['status']}")
        return None
    with _state_lock:
        _target = r["hwnd"]
    _log(f"RE-LATCHED hwnd={r['hwnd']} title={r['title']!r}")
    return r["hwnd"]


# ---------------------------------------------------------------------------
# Win32 plumbing
# ---------------------------------------------------------------------------

class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", w.WORD), ("wScan", w.WORD), ("dwFlags", w.DWORD),
                ("time", w.DWORD), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                ("mouseData", w.DWORD), ("dwFlags", w.DWORD),
                ("time", w.DWORD), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _INPUTUNION(ctypes.Union):
    # _pad keeps sizeof(_INPUT) at 40 on x64 whichever arm is set (MOUSEINPUT
    # is 32 bytes, KEYBDINPUT 24).  A wrong size makes SendInput fail SILENTLY
    # by returning 0, so this is asserted in the report below, not assumed.
    _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT), ("_pad", ctypes.c_byte * 32)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", w.DWORD), ("u", _INPUTUNION)]


_u32.SendInput.argtypes = [w.UINT, ctypes.POINTER(_INPUT), ctypes.c_int]
_u32.MapVirtualKeyW.argtypes = [w.UINT, w.UINT]
_u32.SetForegroundWindow.argtypes = [w.HWND]
_u32.GetForegroundWindow.restype = w.HWND
_u32.ShowWindow.argtypes = [w.HWND, ctypes.c_int]
_u32.IsIconic.argtypes = [w.HWND]
_u32.AttachThreadInput.argtypes = [w.DWORD, w.DWORD, w.BOOL]
_u32.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
_u32.OpenClipboard.argtypes = [w.HWND]
_u32.SetClipboardData.argtypes = [w.UINT, w.HANDLE]
_u32.SetClipboardData.restype = w.HANDLE
_k32.GlobalAlloc.argtypes = [w.UINT, ctypes.c_size_t]
_k32.GlobalAlloc.restype = w.HGLOBAL
_k32.GlobalLock.argtypes = [w.HGLOBAL]
_k32.GlobalLock.restype = ctypes.c_void_p
_k32.GlobalUnlock.argtypes = [w.HGLOBAL]
_k32.GetCurrentThreadId.restype = w.DWORD


def foreground_hwnd():
    """GetForegroundWindow as a plain int, never None.

    ctypes maps a NULL HWND return to None rather than 0, and NULL is a
    DOCUMENTED result here, not an error: there is genuinely no foreground
    window while activation is changing, and none visible to us at all when
    the interactive desktop is not ours — screensaver, lock screen, UAC's
    secure desktop.  int(None) raises, so every call site must normalize.  0
    is the right answer for "nobody has focus": it can never equal a real
    HWND, so callers fall through to their failure path instead of crashing.

    Any ctypes function with a pointer restype has this hazard — FindWindow,
    GetParent, GetWindow, GlobalLock.  Treat int(...) on such a return as a
    bug unless NULL is impossible.
    """
    return _u32.GetForegroundWindow() or 0


def _focus(hwnd):
    """Bring the target forward and CONFIRM it actually got focus.

    SetForegroundWindow is silently refused for processes without recent user
    input, so its return value is not trustworthy on its own; AttachThreadInput
    is the standard workaround.  The verification loop is the point — an
    unverified focus is treated as failure.
    """
    if _u32.IsIconic(w.HWND(hwnd)):
        _u32.ShowWindow(w.HWND(hwnd), SW_RESTORE)
    _u32.SetForegroundWindow(w.HWND(hwnd))
    if foreground_hwnd() == hwnd:
        return True

    cur = _k32.GetCurrentThreadId()
    fg = foreground_hwnd()
    # 0 when nothing holds focus — attaching to thread 0 is meaningless, so
    # skip straight to the plain retry rather than pairing a bogus
    # attach/detach.
    other = _u32.GetWindowThreadProcessId(w.HWND(fg), None) if fg else 0
    target = _u32.GetWindowThreadProcessId(w.HWND(hwnd), None)
    attached = [t for t in (other, target) if t and t != cur]
    for t in attached:
        _u32.AttachThreadInput(cur, t, True)
    try:
        _u32.SetForegroundWindow(w.HWND(hwnd))
    finally:
        for t in attached:
            _u32.AttachThreadInput(cur, t, False)

    for _ in range(20):                      # up to ~200ms for the switch to settle
        if foreground_hwnd() == hwnd:
            return True
        time.sleep(0.01)
    return False


def _key_events(vk, down=True, up=True, extended=False):
    """Scancode-based key events.

    Scancodes rather than virtual keys: console hosts and terminal emulators
    read scancodes far more reliably than synthesized VK-only events.

    `extended` is MANDATORY for the E0-prefixed keys — the arrow cluster,
    Insert/Delete/Home/End/PgUp/PgDn, and right Ctrl/Alt.  MapVirtualKeyW
    returns the same scancode for the arrow and its numeric-keypad twin
    (VK_RIGHT and numpad 6 are both 0x4D), so a scancode send WITHOUT this
    flag is numpad 6 — which with NumLock on types the character "6" into the
    composer instead of moving the cursor.  The flag is what distinguishes
    them on the wire.  Nothing warns you if it is missing.
    """
    scan = _u32.MapVirtualKeyW(vk, MAPVK_VK_TO_VSC)
    flags = KEYEVENTF_SCANCODE | (KEYEVENTF_EXTENDEDKEY if extended else 0)
    out = []
    if down:
        out.append(_INPUT(type=INPUT_KEYBOARD,
                          u=_INPUTUNION(ki=_KEYBDINPUT(0, scan, flags, 0, None))))
    if up:
        out.append(_INPUT(type=INPUT_KEYBOARD,
                          u=_INPUTUNION(ki=_KEYBDINPUT(
                              0, scan, flags | KEYEVENTF_KEYUP, 0, None))))
    return out


def _mouse_move_events(dx, dy):
    """A relative mouse move.  No buttons — MOUSEEVENTF_MOVE only."""
    return [_INPUT(type=INPUT_MOUSE,
                   u=_INPUTUNION(mi=_MOUSEINPUT(dx, dy, 0, MOUSEEVENTF_MOVE, 0, None)))]


def _send(events):
    arr = (_INPUT * len(events))(*events)
    sent = _u32.SendInput(len(events), arr, ctypes.sizeof(_INPUT))
    return sent == len(events)


def _guarded_send(batches, label, gap_s=0.0, before_send=None):
    """The only path to SendInput.  Armed -> target -> focus -> verify -> send.

    batches: list of event lists.  Multiple batches are sent through a SINGLE
    focus acquisition, separated by gap_s — used by the double-ESC and the
    continue pair, where the presses must be distinct in time but must not
    race a focus change between them.
    """
    if not is_active():
        _log(f"REFUSED {label}: vibe control is not active")
        return False
    with _send_lock:
        hwnd = target_hwnd()
        if hwnd is None:
            _log(f"REFUSED {label}: no valid target window")
            return False
        if not _focus(hwnd):
            # foreground=0 means no foreground window at all — usually the
            # screensaver or a locked/secure desktop, i.e. nothing was going
            # to be typed anywhere.  Logged so the condition is named rather
            # than guessed at.
            _log(f"ABORTED {label}: could not confirm focus on hwnd={hwnd} "
                 f"(foreground={foreground_hwnd()})")
            return False
        if before_send is not None and not before_send():
            _log(f"FAILED {label}: pre-send action")
            return False
        ok = True
        for i, events in enumerate(batches):
            if i:
                time.sleep(gap_s)
            ok = _send(events) and ok
        _log(f"{'sent' if ok else 'FAILED'} {label} -> hwnd={hwnd}")
        return ok


# ---------------------------------------------------------------------------
# Public actions (the entire reachable key set)
# ---------------------------------------------------------------------------

def wake():
    """Dismiss the screensaver / wake a blanked display.

    THE ONE ACTION THAT DOES NOT GO THROUGH _guarded_send, and it lives on its
    own path precisely so that the exemption is visible in the code rather
    than hidden behind a parameter.  Two guardrails are deliberately skipped:

      * No focus acquisition.  It cannot have any — while the screensaver
        holds the desktop there is no foreground window at all (that is the
        very condition this exists to escape), so requiring confirmed focus
        would make the action impossible by construction.
      * No armed check.  This is also reachable OUTSIDE Vibe Control, where
        nothing is armed, because a blanked screen is exactly the situation in
        which the user cannot see enough to activate anything.

    That is only defensible because the payload cannot do anything.  A bare
    Shift press/release types no character and alters no application state;
    the mouse events are a relative +1/-1 pair, so the pointer ends where it
    began and no button is ever pressed.  If this lands in some other window —
    or in the target session when the display was already awake — the worst
    outcome is nothing at all.  No other key would be safe here.

    Both kinds of input are sent because the two failure modes differ: a
    screensaver exits on any input, but a display blanked by a power profile
    responds more reliably to mouse movement.  Cheap to send both.

    Note this cannot help against a LOCKING screensaver: the secure desktop is
    a boundary a user-mode process on the default desktop cannot cross, and
    should not.
    """
    events = (_key_events(VK_SHIFT)
              + _mouse_move_events(1, 0)
              + _mouse_move_events(-1, 0))
    ok = _send(events)
    _log(f"{'sent' if ok else 'FAILED'} wake (shift + net-zero nudge)")
    return ok


def press_enter():
    """Enter.  In a selection UI this activates the highlighted entry — the
    first by default — and it also submits a composed prompt."""
    return _guarded_send([_key_events(VK_RETURN)], "enter")


def press_escape():
    """ESC twice.  Claude Code needs a double-press to clear the input box; a
    single ESC does not (found in badge use, not by inspection).

    The gap matters in BOTH directions.  Too short and the pair risks being
    parsed as an escape SEQUENCE rather than two presses: TUI input layers
    typically wait ~25-50 ms after ESC to see whether a CSI/Alt continuation
    follows.  Too long and it falls outside the application's double-press
    window.  80 ms clears the sequence timeout with margin while staying far
    inside any plausible double-press window.
    """
    return _guarded_send([_key_events(VK_ESCAPE), _key_events(VK_ESCAPE)],
                         "escape x2", gap_s=ESCAPE_GAP_S)


def press_continue():
    """Right Arrow, then Enter — accept the suggested next action and submit it.

    When Claude Code settles it offers a recommended next action as ghost text
    in the composer; Right Arrow autocompletes it and Enter submits.  Both
    keys go through ONE focus acquisition, so the Enter cannot race a focus
    change and land somewhere the Right Arrow did not.

    The gap between them is SDK_VIBE_CONTINUE_GAP_MS (default 80 ms), NOT the
    escape gap — it exists for a different reason and must be tunable apart
    from it.  Here it gives the TUI time to accept the completion and re-render
    before Enter arrives; an Enter that beat the autocomplete would submit an
    empty composer.

    This is the one place the Right Arrow is reachable at all: it is half of a
    fixed pair, never a standalone verb, so the "no arbitrary keys" property
    of the injector is unchanged.
    """
    return _guarded_send([_key_events(VK_RIGHT, extended=True),
                          _key_events(VK_RETURN)],
                         "continue (right + enter)", gap_s=CONTINUE_GAP_S)


def press_digit(n):
    """A digit 1-9, for explicit selection from a numbered list."""
    if not 1 <= n <= 9:
        _log(f"REFUSED digit {n}: out of range")
        return False
    return _guarded_send([_key_events(VK_1 + (n - 1))], f"digit {n}")


def set_clipboard(text):
    """Put text on the clipboard as CF_UNICODETEXT."""
    if not _u32.OpenClipboard(None):
        return False
    try:
        _u32.EmptyClipboard()
        buf = ctypes.create_unicode_buffer(text)
        size = ctypes.sizeof(buf)
        h = _k32.GlobalAlloc(GMEM_MOVEABLE, size)
        if not h:
            return False
        ptr = _k32.GlobalLock(h)
        ctypes.memmove(ptr, buf, size)
        _k32.GlobalUnlock(h)
        return bool(_u32.SetClipboardData(CF_UNICODETEXT, h))
    finally:
        _u32.CloseClipboard()


def paste_text(text):
    """Stage text on the clipboard and Ctrl+V it into the latched window.

    Enter is deliberately NOT pressed.  No voice command in the shipped
    vocabulary calls this — it is the hook for anyone adding dictation on top
    of the SDK: swap the recognizer to a large model, capture free-form
    speech, and land it in the composer with this.  Keeping submission a
    SEPARATE, deliberate act is the whole safety argument for that feature;
    see sdk/VIBE.md -> Philosophy.
    """
    if not text.strip():
        _log("REFUSED paste: empty text")
        return False
    if not set_clipboard(text):
        _log("FAILED paste: clipboard write failed")
        return False
    events = (_key_events(VK_CONTROL, up=False)
              + _key_events(VK_V)
              + _key_events(VK_CONTROL, down=False))
    return _guarded_send([events], f"paste ({len(text)} chars)")


def _report():
    """Diagnostic only — never injects.  A standalone run is unarmed by
    construction (state is in-process), so this is all the CLI can do."""
    print(f"sizeof(INPUT)  : {ctypes.sizeof(_INPUT)}  (must be 40 on x64)")
    print(f"escape gap     : {ESCAPE_GAP_S:.3f}s")
    print(f"continue gap   : {CONTINUE_GAP_S:.3f}s")
    print(f"armed          : {is_active()}  (always False in a fresh process)")
    print()
    return vibewin._report()


if __name__ == "__main__":
    sys.exit(_report())
