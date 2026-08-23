#!/usr/bin/env python3
"""Game Mode — the badge tap AS a mouse click (TOS SDK).

Windows only.  Component of TOS, subject to <LICENSE.md>.

While Game Mode is armed, a single badge tap is dispatched as a mouse click at
the pointer and NO SPEECH IS RECOGNIZED AT ALL.  The tap is not a way of saying
something; the tap *is* the click.  It was built for a game whose teleport
commits on right-click-down: aim with the mouse, tap the badge, go.

Pure stdlib ctypes against user32, matching the SDK's no-dependency posture.

STATE IS IN-PROCESS, exactly as in vibekeys.py, and here it pays a second
dividend.  The mode cannot outlive the server: kill computer.py by any means —
Ctrl-C, a crash, a closed window — and the armed flag dies with the process, so
there is no such thing as a stranded Game Mode.  A design that parked the mode
in a file would have to detect that case and clean up after it; this one cannot
enter it.  (The full TOS build does park it in a file, because its badge tap and
its console live in different processes, and it pays exactly that price: see
clicker/CLICKER.md -> The holder window.)

WHY THERE IS NO WINDOW TARGETING.  vibekeys.py acquires and VERIFIES focus on a
latched HWND before every send, and aborts rather than redirect, because a stray
Enter submits something.  That guardrail does not transfer to a click: Windows
routes a button-down to the window under the CURSOR, so "click the game" and
"click where the pointer is" are the same instruction, and re-pointing the
cursor to satisfy a window check would move the very thing being aimed.

What contains it instead is the mode:

  * the click is reachable only while activate() has succeeded — a fresh
    interpreter is disarmed, so running this file standalone injects nothing;
  * arming is an explicit spoken command, and it announces itself loudly on the
    server console, because a mode that suppresses the phrase which would end it
    has to say somewhere visible how to end it;
  * and it dies with the server.

THE COST, STATED PLAINLY: while Game Mode is on, every badge tap right-clicks
whatever the pointer happens to be over.  Stop the mode before leaving the
pointer somewhere a right-click would matter.

Configuration (environment, read at import):
    SDK_GAME_BUTTON     right | left | middle   (default: right)
    SDK_GAME_HOLD_MS    button-down duration    (default: 20)

CLI:  python clicker.py    — report configuration and state (never injects)
"""

import ctypes
import os
import sys
import threading
import time

if sys.platform != "win32":
    raise ImportError(
        "clicker is Windows-only: it injects mouse input through the Win32 "
        "SendInput API.  computer.py imports it defensively and simply omits "
        "the Game Mode commands on other platforms."
    )

_u32 = ctypes.WinDLL("user32", use_last_error=True)

INPUT_MOUSE = 0

# Down/up flag pairs.  RIGHT is the one this exists for; the others cost
# nothing to name and a different game may want one.  There is deliberately no
# "send arbitrary mouse input" entry point — no absolute moves, no wheel, no
# drag.  A tap is one click in place, and anything that could reposition the
# pointer would move the aim set by hand.
BUTTONS = {
    "right":  (0x0008, 0x0010),   # MOUSEEVENTF_RIGHTDOWN  / RIGHTUP
    "left":   (0x0002, 0x0004),   # MOUSEEVENTF_LEFTDOWN   / LEFTUP
    "middle": (0x0020, 0x0040),   # MOUSEEVENTF_MIDDLEDOWN / MIDDLEUP
}


def _int_env(key, default):
    try:
        return int(os.environ.get(key, default))
    except ValueError:
        return int(default)


BUTTON = os.environ.get("SDK_GAME_BUTTON", "right").strip().lower()
if BUTTON not in BUTTONS:
    BUTTON = "right"

# Not cosmetic.  A game that polls button state once per frame can miss a press
# that goes down and up inside the same frame.  20 ms clears a 60fps frame with
# room to spare and is imperceptible next to the ~1 s badge dispatch in front
# of it.
HOLD_S = _int_env("SDK_GAME_HOLD_MS", "20") / 1000.0


# ---------------------------------------------------------------------------
# Win32 plumbing
# ---------------------------------------------------------------------------

class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("u", _INPUTUNION)]


_u32.SendInput.argtypes = [ctypes.c_uint, ctypes.POINTER(_INPUT), ctypes.c_int]
_u32.SendInput.restype = ctypes.c_uint


# ---------------------------------------------------------------------------
# Mode state — in-process, owned here, driven by computer.py
# ---------------------------------------------------------------------------

_state_lock = threading.Lock()
_send_lock = threading.Lock()

# Armed or not.  Nothing clicks while False, and a fresh interpreter starts
# False, which is what makes a standalone run of this file inert.
_active = False

BANNER = r"""
====================================================================
  G A M E   M O D E   A C T I V E
====================================================================

  Every badge tap is now a {button}-CLICK at the mouse pointer.
  The badge understands NO voice commands while this is on.

  >>>  TYPE  game off  IN THIS CONSOLE TO STOP GAME MODE  <<<
       (stopping the server stops it too)

  button : {button}          hold: {hold} ms
====================================================================
"""


def is_active():
    with _state_lock:
        return _active


def activate():
    """Arm Game Mode.  Returns the phrase to speak.

    The banner is not decoration.  Game Mode makes every tap a click, so the
    phrase that would end it can never be heard — a tap never becomes speech
    while it is on.  A mode you cannot leave the way you entered it has to say
    how to leave it, somewhere you can read it, and the server console is the
    one surface this program already owns.
    """
    global _active
    with _state_lock:
        if _active:
            return "Game mode is already active."
        _active = True
    print(BANNER.format(button=BUTTON.upper(), hold=int(HOLD_S * 1000)))
    return ("Game mode engaged. Badge taps send right-click until game mode "
            "is stopped from the server console.")


def deactivate():
    """Disarm.  Returns the phrase to speak.

    Reachable from the server console, never from the badge — see activate().
    """
    global _active
    with _state_lock:
        was = _active
        _active = False
    if was:
        # ASCII only in anything this module PRINTS. computer.py forces a UTF-8
        # stdout in main(), but clicker.py is also runnable on its own, and a
        # cp1252 console would mangle a dash for no gain.
        print("[clicker] game mode released - badge taps are voice commands again")
    return "Game mode released." if was else "Game mode is not active."


# ---------------------------------------------------------------------------
# The payload
# ---------------------------------------------------------------------------

def _mouse_events(flags):
    return [_INPUT(type=INPUT_MOUSE,
                   u=_INPUTUNION(mi=_MOUSEINPUT(0, 0, 0, flags, 0, None)))]


def _send(events):
    arr = (_INPUT * len(events))(*events)
    return _u32.SendInput(len(events), arr, ctypes.sizeof(_INPUT)) == len(events)


def click(button=None):
    """One click at the pointer.  The only path to SendInput in this module.

    Down and up go as two separate calls with HOLD_S between them rather than
    one two-event batch — see SDK_GAME_HOLD_MS above for why a zero-duration
    press is not good enough.

    The reference target locks its destination on the button-DOWN edge, so the
    up event is protocol hygiene rather than part of the aim.
    """
    button = (button or BUTTON).lower()
    if button not in BUTTONS:
        print(f"[clicker] REFUSED: {button!r} is not a known button")
        return False
    if not is_active():
        print("[clicker] REFUSED: game mode is not active")
        return False
    down, up = BUTTONS[button]
    with _send_lock:
        ok = _send(_mouse_events(down))
        if HOLD_S > 0:
            time.sleep(HOLD_S)
        ok = _send(_mouse_events(up)) and ok
    print(f"[clicker] {'sent' if ok else 'FAILED'} {button} click")
    return ok


def _report():
    print(f"button   : {BUTTON}")
    print(f"hold_ms  : {int(HOLD_S * 1000)}")
    print(f"armed    : {is_active()}  (always False in a fresh process - "
          f"state is in-process, so this file can inject nothing on its own)")
    return 0


if __name__ == "__main__":
    sys.exit(_report())
