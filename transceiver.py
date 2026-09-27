#!/usr/bin/env python3
"""
Minimal Combadge Transceiver / Connection Manager (TOS SDK).

This script runs as ROOT and supervises every combadge paired to this host:
one badge per Bluetooth adapter, one listener.py per badge. With one adapter
and one badge it is exactly the single-badge transceiver it always was.

Why root?
  - `runuser` (drop to a normal user account) requires root.
  - `sg input -c ...` (add the `input` supplementary group so listener.py
    can open /dev/input/eventX) also requires root unless the caller is
    already in `input`.  Running as the user directly would require adding
    them to the `input` group permanently, which is a larger system change.

-----------------------------------------------------------------------------
THE SHAPE  (multiuser/PI.md, phase 1)
-----------------------------------------------------------------------------
  COORDINATOR (main thread, every SDK_DETECT_INTERVAL seconds)
    1. Pre-flight survey of BlueZ, per adapter: power adapters on, `trust`
       any paired badge that is not trusted.  Re-run when anything in BlueZ's
       object tree changes (a pairing, a removal, an adapter plugged in), and
       every SURVEY_INTERVAL regardless.
    2. Observe which adapter each badge is connected on.
    3. Assign badges to adapters, ONE BADGE PER ADAPTER, never shared.
    4. Enforce: remove a second pairing of a badge on another adapter;
       disconnect a badge that has come up on an adapter another badge owns.
    5. Keep one supervisor thread per assigned badge.

  SUPERVISOR (one thread per badge)
    Bring up HFP on its badge's link, run listener.py for it with the badge's
    MAC AND adapter, restart it if it dies, stop it when the badge goes.

  PAGE (one background thread)
    `Device1.Connect` on each assigned badge that is absent, through ITS OWN
    adapter only.

Everything talks to BlueZ over D-Bus (`busctl`), addressed per adapter
(/org/bluez/hciN/dev_...), not through `bluetoothctl`.  bluetoothctl works on
ONE adapter at a time -- the default -- so on a two-adapter host it cannot
even list the second adapter's badge.  Verified on PAN 2026-09-26: adapter B
was the default, and the old transceiver would have seen only its badge.

Adapters are identified by ADDRESS.  `hciN` numbering is not stable across
boots with identical dongles; it is looked up fresh, and only used where a
tool demands it (btmon -i).

Usage:
    sudo SDK_USER=$USER python3 transceiver.py [/path/to/listener.py]

Environment variables:
    SDK_USER            — the username to run listener.py as (required if not
                          using sudo; sudo sets SUDO_USER automatically)
    SDK_SERVER_HOST     — hostname/IP where computer.py is running (default: localhost)
    SDK_SERVER_PORT     — TCP port for computer.py (default: 1701)
    SDK_DETECT_INTERVAL — seconds between connectivity checks (default: 2)
    SDK_PAGE_GAP        — seconds between page sweeps while a badge is
                          absent (default: 5)

~~SDK_BADGE_MAC~~ REMOVED 2026-09-26 (Captain, PI.md Ruling 10). It pinned one
badge out of several paired ones, for a transceiver that ran one badge at a
time. This one runs every badge paired to its adapters; the pairings ARE the
set. To keep a badge out, unpair it from this host.

-----------------------------------------------------------------------------
WHY TWO LOOPS  (the single most important thing in this file)
-----------------------------------------------------------------------------
The obvious design is one loop that checks, then connects, then sleeps. Do not
write that. It is what this file used to be, and it is slow for a reason that
is invisible until you measure it.

A connect against a badge that is switched off or out of range does not fail
fast. It blocks for the controller's **page timeout** — BlueZ's default is
0x2000 slots x 0.625 ms = **5.12 seconds** — before reporting failure.
Meanwhile, checking whether a badge is connected is a D-Bus property read
costing a few milliseconds.

Put both in one loop and the cheap operation is held hostage by the expensive
one. That matters more than it sounds, because a badge often connects
*itself*: powering it on makes it page the host it was last paired with. In the
reference TOS deployment, **24% of all links measured over 8,951 retry cycles
were badge-initiated**. For every one of those the connect attempt was pointless
and the badge sat unnoticed for an average of 8 seconds waiting for a loop that
was busy paging a badge already on the line.

Split them and detection costs whatever you set SDK_DETECT_INTERVAL to — about
2 seconds — while paging carries on in the background at its own pace, never
blocking anything.

-----------------------------------------------------------------------------
BATTERY: which battery, and what actually drains it
-----------------------------------------------------------------------------
A natural worry is that retrying faster will drain the badge. It will not, and
it is worth understanding why before tuning anything.

  * Paging costs the HOST, not the badge. A connect transmits page trains from
    the host radio. The badge sits in page scan at a duty cycle fixed by its
    own firmware; it cannot tell how often you page it.
  * What costs the BADGE is SCO: bringing the audio link up, playing through
    its speaker, tearing it down. That is the badge's highest-power activity by
    a wide margin. If you want to save badge battery, look at how often you
    play audio to it — not at how often you poll.
  * What costs the HOST is SDK_DETECT_INTERVAL. Each pass forks a few busctl
    subprocesses (one per pairing). At 2 s that is a rounding error; at 0 it
    is a busy loop that keeps a core warm forever. On a battery-powered relay
    host (a Raspberry Pi, a laptop) keep it at >= 0.5. If you need instant
    detection without polling at all, the right answer is not a tighter loop —
    it is a D-Bus signal subscription on org.bluez.Device1's `Connected`
    property.

Stripped down from relay-linux/combadge.py: no per-host PID files, no IPC
flags, no log file rotation, foreground.
"""
import os
import pty
import pwd
import re
import select
import signal
import subprocess
import sys
import threading
import time

# How often the coordinator checks connectivity. This is the reconnect latency
# you actually feel. See "Battery" above before setting it below 0.5.
DETECT_INTERVAL = max(0.0, float(os.environ.get("SDK_DETECT_INTERVAL", "2")))

# How long the PAGE loop waits between sweeps while a badge is away. Each
# attempt against an absent badge costs ~5 s of page timeout regardless of
# this value, so the effective retry period is roughly 5 s per absent badge +
# SDK_PAGE_GAP.
PAGE_GAP = max(0.0, float(os.environ.get("SDK_PAGE_GAP", "5")))

# The pre-flight survey re-runs whenever BlueZ's object tree changes (cheap to
# check every pass), and at least this often regardless, which is what catches
# a badge being un-trusted or an adapter being powered off from outside.
SURVEY_INTERVAL = 30  # seconds

# How long to stand a badge down after it proves unusable. See quarantine().
UNUSABLE_COOLDOWN = 60  # seconds
_cooldown      = {}     # MAC (upper) -> timestamp until which it is skipped
_cooldown_lock = threading.Lock()


def quarantine(mac, seconds=UNUSABLE_COOLDOWN):
    """Stand a badge down temporarily.

    Needed because "connected" and "usable" are NOT the same thing. BlueZ will
    happily hold an ACL link open to a badge whose audio profile never came up
    (`br-connection-profile-unavailable` — see the troubleshooting notes in
    relay-linux/RELAY.md). Such a badge reports `Connected: yes` forever while no
    `bluez_card.<MAC>` ever appears.

    Without a quarantine that state is a LIVELOCK: the supervisor keeps
    retrying the half-connected badge, waiting out the 15 s card poll each
    time. Standing it down disconnects it and stops the page loop reaching for
    it until the cooldown passes. Observed on PAN 2026-08-08.
    """
    with _cooldown_lock:
        _cooldown[mac.upper()] = time.time() + seconds


def is_quarantined(mac):
    """True if `mac` is still standing down. Expired entries are dropped."""
    with _cooldown_lock:
        until = _cooldown.get(mac.upper())
        if until is None:
            return False
        if time.time() >= until:
            del _cooldown[mac.upper()]
            return False
        return True

# ---------------------------------------------------------------------------
# Logging — every line timestamped, host-tagged, and teed to a file
# (module-level `print` shadow; listener.py does the same with the badge
# MAC).  File: sdk/log/transceiver_<hostname>.log — on PAN that lands on the
# shared mount, live-readable from CUBE.  Open/write/close per line: a handle
# held open over sshfs locks the file against every CUBE reader.
# ---------------------------------------------------------------------------

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR    = os.path.join(SCRIPT_DIR, "log")
HOSTNAME   = os.uname().nodename
LOG_FILE   = os.path.join(LOG_DIR, f"transceiver_{HOSTNAME}.log")
try:
    os.makedirs(LOG_DIR, exist_ok=True)
except OSError:
    pass

_print    = print
_log_lock = threading.Lock()


def print(*args, **kwargs):   # noqa: A001 — deliberate shadow, see above
    line    = " ".join(str(a) for a in args)
    stamped = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [{HOSTNAME}] {line}"
    with _log_lock:
        _print(stamped, **kwargs)
        try:
            with open(LOG_FILE, "a") as f:
                f.write(stamped + "\n")
        except OSError:
            pass


_said = {}   # key -> last message, so a standing condition is logged once


def say_once(key, msg):
    """Log `msg` unless it is what was last said under `key`. For conditions
    that persist across passes: said when they start or change, not every 2 s."""
    if _said.get(key) != msg:
        _said[key] = msg
        print(msg)


def unsay(key):
    """Forget `key`, so the condition is reported again if it comes back."""
    _said.pop(key, None)


# Full paths to system tools.  Adjust if your distro puts them elsewhere.
BUSCTL  = "/usr/bin/busctl"         # systemd's D-Bus client -- talks to BlueZ
BTCTL_BIN = "/usr/bin/bluetoothctl" # claiming only: one interactive session (see CLAIMING)
PACTL   = "/usr/bin/pactl"          # PipeWire/PulseAudio control tool
RUNUSER = "/usr/sbin/runuser"       # Run a command as a different user (needs root)


def run(cmd, timeout=15, **kw):
    """Run a command and return the CompletedProcess, or None on failure.

    Swallows TimeoutExpired and FileNotFoundError so callers never need to
    handle the case where a system tool is missing or unresponsive.
    capture_output=True prevents system tool stdout/stderr from leaking into
    our console output.
    """
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout, **kw)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None


# ---------------------------------------------------------------------------
# BlueZ over D-Bus
#
# Every object is addressed by its path, which names its adapter:
#     /org/bluez/hci0                       adapter  (org.bluez.Adapter1)
#     /org/bluez/hci0/dev_2C_F2_DF_45_EC_28 device, AS SEEN BY hci0 (Device1)
# The same badge paired on two adapters is two objects. That is the whole
# reason this is per-path and not per-MAC.
# ---------------------------------------------------------------------------

BLUEZ      = "org.bluez"
ADAPTER_IF = "org.bluez.Adapter1"
DEVICE_IF  = "org.bluez.Device1"
_ADAPTER_RE = re.compile(r"^/org/bluez/(hci\d+)$")
_DEVICE_RE  = re.compile(r"^/org/bluez/(hci\d+)/dev_((?:[0-9A-Fa-f]{2}_){5}[0-9A-Fa-f]{2})$")


def _why(r):
    """The last line busctl printed, which is where it puts the reason."""
    if r is None:
        return "busctl did not respond"
    lines = ((r.stderr or "") + (r.stdout or "")).strip().splitlines()
    return lines[-1].strip() if lines else f"exit {r.returncode}"


def bz_props(path, iface, *names):
    """Read several properties in ONE busctl call. Returns a list of values
    (bool for 'b', str for 's'), or None if the object or any property is
    missing. busctl prints one line per property:  `b true`, `s "TNG COMBADGE"`."""
    r = run([BUSCTL, "get-property", BLUEZ, path, iface, *names])
    if not r or r.returncode != 0:
        return None
    vals = []
    for line in r.stdout.splitlines():
        kind, _, v = line.strip().partition(" ")
        if kind == "b":
            vals.append(v == "true")
        elif kind == "s":
            vals.append(v[1:-1] if len(v) >= 2 and v[0] == '"' else v)
        else:
            vals.append(v)
    return vals if len(vals) == len(names) else None


def bz_set_bool(path, iface, name, value):
    """Set a boolean property. Returns (ok, reason)."""
    r = run([BUSCTL, "set-property", BLUEZ, path, iface, name, "b",
             "true" if value else "false"])
    return (r is not None and r.returncode == 0), _why(r)


def bz_call(path, iface, method, *args, timeout=15):
    """Call a method. Returns (ok, reason). BlueZ reports failures as
    `Call failed: <reason>`, e.g. br-connection-page-timeout."""
    r = run([BUSCTL, f"--timeout={timeout}", "call", BLUEZ, path, iface,
             method, *args], timeout=timeout + 5)
    return (r is not None and r.returncode == 0), _why(r)


def bluez_tree():
    """Every object path BlueZ exports, as a tuple (comparable pass to pass),
    or None if BlueZ is not answering."""
    r = run([BUSCTL, "tree", "--list", BLUEZ])
    if not r or r.returncode != 0:
        return None
    return tuple(ln.strip() for ln in r.stdout.splitlines() if ln.strip())


# ---------------------------------------------------------------------------
# Shared state. The coordinator writes it; supervisors and the page loop
# read it. One lock, held only for copies -- never across a subprocess.
# ---------------------------------------------------------------------------

_state_lock = threading.Lock()
ADAPTERS = {}   # adapter addr -> {"hci": "hci0", "path": "/org/bluez/hci0"}
PAIRED   = {}   # badge MAC -> {adapter addr: device path}   (paired only)
ASSIGNED = {}   # badge MAC -> adapter addr it is supervised on
LIVE     = {}   # badge MAC -> adapter addr it is connected on, or None
RUNNING  = set()  # badge MACs whose listener.py is currently up
_sweep_start = 0  # rotates which badge the page sweep tries first


# ---------------------------------------------------------------------------
# Pre-flight (PI.md -> "Pre-flight check", the phase-1 rows)
# ---------------------------------------------------------------------------

def survey(tree):
    """Read BlueZ's actual state, per adapter, and put right what can be put
    right without pairing anything. Assumes nothing about who set it up: the
    user may have paired, trusted or connected by hand, correctly or not.

    Returns (adapters, paired):
        adapters  addr -> {"hci", "path"}          powered adapters only
        paired    badge MAC -> {adapter addr: device path}

    Repairs made here, each logged as one line:
      * adapter powered off   -> power it on (failure = adapter fault)
      * badge paired, not trusted -> trust it. Without trust BlueZ refuses a
        badge that reconnects by itself, which is ~a quarter of reconnects.
    """
    adapters, hci_addr = {}, {}
    for path in tree:
        m = _ADAPTER_RE.match(path)
        if not m:
            continue
        hci = m.group(1)
        props = bz_props(path, ADAPTER_IF, "Address", "Powered")
        if not props:
            continue
        addr, powered = props[0].upper(), props[1]
        if not powered:
            ok, why = bz_set_bool(path, ADAPTER_IF, "Powered", True)
            if ok:
                print(f"[transceiver] pre-flight: adapter {addr} ({hci}) was "
                      "powered off -- powered on")
                powered = True
            else:
                say_once(f"power:{addr}",
                         f"[transceiver] ADAPTER FAULT: {addr} ({hci}) is powered "
                         f"off and will not power on: {why}. Check `rfkill list`.")
        if powered:
            unsay(f"power:{addr}")
            _POWER_FAULTS.discard(addr)
            adapters[addr] = {"hci": hci, "path": path}
            hci_addr[hci] = addr
        else:
            _POWER_FAULTS.add(addr)

    paired, unpaired = {}, set()
    for path in tree:
        m = _DEVICE_RE.match(path)
        if not m or m.group(1) not in hci_addr:
            continue
        addr = hci_addr[m.group(1)]
        mac  = m.group(2).replace("_", ":").upper()
        # Name first: a device with no Name is not a combadge we can recognise,
        # and asking for a missing property fails the whole call.
        props = bz_props(path, DEVICE_IF, "Name", "Paired", "Trusted")
        if not props or "TNG COMBADGE" not in props[0].upper():
            continue
        if not props[1]:
            unpaired.add(mac)   # seen in a scan; claiming it is phase 2
            continue
        if not props[2]:
            ok, why = bz_set_bool(path, DEVICE_IF, "Trusted", True)
            if ok:
                print(f"[transceiver] pre-flight: {mac} on {addr} was paired but "
                      "not trusted -- trusted")
            else:
                say_once(f"trust:{mac}:{addr}",
                         f"[transceiver] pre-flight: could not trust {mac} on "
                         f"{addr}: {why}. It will not be able to reconnect by itself.")
        paired.setdefault(mac, {})[addr] = path

    for mac in sorted(unpaired - set(paired)):
        say_once(f"unpaired:{mac}",
                 f"[transceiver] {mac} is visible but not paired to this host -- "
                 "the claim loop will take it if an adapter is free and the "
                 "new badge discovery is enabled.")
    return adapters, paired


def observe(paired):
    """Which adapter is each badge connected on? One busctl call per pairing;
    all are D-Bus property reads, none touches the radio."""
    live = {}
    for mac, by_adapter in paired.items():
        live[mac] = None
        for addr, path in sorted(by_adapter.items()):
            v = bz_props(path, DEVICE_IF, "Connected")
            if v and v[0]:
                live[mac] = addr
                break
    return live


def assign(adapters, paired, live, previous, running):
    """One badge per adapter. Never two on one (PI.md Ruling 11).

    Returns (assigned, evict, orphans):
        assigned  badge MAC -> adapter addr
        evict     [(MAC, adapter addr)]: connected on an adapter another badge
                  already owns -- must be disconnected
        orphans   [MAC]: paired, but every adapter it is paired on is taken

    Order matters, and it is chosen so nothing that works is disturbed:
      1. Connected badges keep the adapter they are on. If two are connected
         on ONE adapter, the one with a running listener keeps it (then the
         lower MAC); the other is evicted.
      2. The rest, most constrained first (fewest adapters it is paired on),
         so a badge with a choice never takes the only adapter another badge
         has. Each keeps its previous adapter if still free.
    """
    taken, assigned, evict, orphans = set(), {}, [], []
    for mac in sorted((m for m in paired if live.get(m)),
                      key=lambda m: (m not in running, m)):
        addr = live[mac]
        if addr in taken:
            evict.append((mac, addr))
            continue
        taken.add(addr)
        assigned[mac] = addr
    for mac in sorted((m for m in paired if not live.get(m)),
                      key=lambda m: (len(paired[m]), m)):
        cands = [a for a in sorted(paired[mac]) if a in adapters]
        prev  = previous.get(mac)
        order = ([prev] if prev in cands else []) + [a for a in cands if a != prev]
        addr  = next((a for a in order if a not in taken), None)
        if addr is None:
            orphans.append(mac)
            continue
        taken.add(addr)
        assigned[mac] = addr
    return assigned, evict, orphans


def enforce(assigned, evict, paired, live):
    """Act on assign()'s verdict. Returns True if BlueZ state was changed and
    the survey should be re-run.

      * Double pairing (Ruling 12): a badge connected on one adapter and also
        paired on another has the other pairing REMOVED. Only once it is
        connected: before that there is no telling which pairing is the extra.
      * Eviction (Ruling 11): a badge up on an adapter another badge owns is
        disconnected. It is paired there, so it may come back by itself; it
        will be disconnected again each time, which is the point.
    """
    changed = False
    for mac, addr in assigned.items():
        if live.get(mac) != addr:
            continue
        for other, path in sorted(paired[mac].items()):
            if other == addr:
                continue
            with _state_lock:
                other_path = ADAPTERS.get(other, {}).get("path")
            if not other_path:
                continue
            ok, why = bz_call(other_path, ADAPTER_IF, "RemoveDevice", "o", path)
            print(f"[transceiver] pre-flight: {mac} was paired on two adapters; "
                  f"connected on {addr}, removed its pairing on {other}"
                  + ("" if ok else f" -- FAILED: {why}"))
            changed = changed or ok
    for mac, addr in evict:
        ok, why = bz_call(paired[mac][addr], DEVICE_IF, "Disconnect")
        print(f"[transceiver] {mac} came up on {addr}, which another badge owns. "
              "Two badges never share an adapter -- disconnected"
              + ("" if ok else f" -- FAILED: {why}"))
    return changed


# ---------------------------------------------------------------------------
# HEALTH -- say what is wrong, on the badges that work (PI.md Ruling 6)
#
# An appliance has no screen, so a failing dongle must be SPOKEN: at startup,
# and whenever it happens later, with the other badges kept running. The
# transceiver knows the facts; only the server can speak. So the facts become
# one sentence in HEALTH_FILE, rewritten whenever it changes; every listener
# on this host watches the file and sends a new sentence up its downlink
# (b'R' + 2-byte length + UTF-8 text), and the server speaks it on that badge.
# Every working badge hears it. Empty file = healthy = nothing said: silence
# means all is well.
#
# Faults: an adapter that will not power on (rfkill, a USB fault); an adapter
# seen earlier in this run that has DISAPPEARED (unplugged, or its USB device
# died); a paired badge with no adapter of its own (Ruling 11). No MAC
# addresses in the sentence -- they do not survive TTS. The log has the detail.
# ---------------------------------------------------------------------------

HEALTH_FILE   = os.path.join(SCRIPT_DIR, "health.txt")
_POWER_FAULTS = set()        # adapter addrs that would not power on (survey)
_seen_adapters = set()       # every adapter present at some point this run
_health_text  = None         # last sentence written (None = never written)


def _count_word(n):
    return {1: "one", 2: "two", 3: "three", 4: "four"}.get(n, str(n))


def update_health(adapters, orphans):
    """Recompute the health sentence; write HEALTH_FILE if it changed."""
    global _health_text
    _seen_adapters.update(adapters)
    lost = _seen_adapters - set(adapters) - _POWER_FAULTS
    parts = []
    if _POWER_FAULTS:
        n = len(_POWER_FAULTS)
        parts.append(f"{_count_word(n)} Bluetooth adapter{'s are' if n > 1 else ' is'} "
                     "not responding")
    if lost:
        n = len(lost)
        parts.append(f"{_count_word(n)} Bluetooth adapter{'s have' if n > 1 else ' has'} "
                     "been lost")
    if orphans:
        n = len(orphans)
        parts.append(f"{_count_word(n)} badge{'s have' if n > 1 else ' has'} no adapter of "
                     f"{'their' if n > 1 else 'its'} own")
    text = ("" if not parts else
            "Warning. " + ". ".join(p[0].upper() + p[1:] for p in parts)
            + ". Check the transceiver log over SSH.")
    if text == _health_text:
        return
    _health_text = text
    tmp = HEALTH_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text + ("\n" if text else ""))
        os.replace(tmp, HEALTH_FILE)
    except OSError as e:
        print(f"[transceiver] health: could not write {HEALTH_FILE}: {e}")
        return
    if text:
        print(f"[transceiver] HEALTH: {text}  (faulty: {sorted(_POWER_FAULTS)}, "
              f"lost: {sorted(lost)}, no adapter: {orphans})")
    else:
        print("[transceiver] health: all well")


# ---------------------------------------------------------------------------
# Audio session
# ---------------------------------------------------------------------------

def session_xdg(username):
    """The user's XDG_RUNTIME_DIR — where their PipeWire socket and D-Bus live."""
    return f"/run/user/{pwd.getpwnam(username).pw_uid}"


def run_as_user(username, cmd):
    """Run a command as `username` WITH their session environment attached.

    THIS IS NOT OPTIONAL, and getting it wrong is silent. We run as root (for
    runuser/sg), and sudo strips XDG_RUNTIME_DIR on the way in. `runuser`
    without `-l` does not create a login session, so it does not set it either.
    A pactl launched that way looks for a PipeWire socket in a directory that
    does not belong to the target user, finds nothing, and reports NO CARDS AND
    NO ERROR — indistinguishable, from the caller's side, from a badge that
    genuinely has no audio card.

    Diagnosed on PAN 2026-08-08: a badge that `bluetoothctl info` showed as
    Connected, Bonded, Trusted, HFP UUID present, 50% battery, was being
    quarantined as "exposes no audio card" — because pactl was querying the
    wrong session. If `pactl` works for you in a terminal but returns nothing
    here, this is why.
    """
    return run([RUNUSER, "-u", username, "--"] + cmd,
               env={"XDG_RUNTIME_DIR": session_xdg(username)})


_audio_services_ready = False


def ensure_audio_services(username, force=False):
    """Start the user's PipeWire stack. Startup only, and again on fault.

    Ported from relay-linux/combadge.py. A headless or SSH-only relay host often has
    no running PipeWire session at all until something asks for one, and
    WirePlumber's Bluetooth monitor additionally gates on logind reporting the
    seat active — see the wireplumber seat-monitoring note in README §7 if the
    card still never appears after this.

    `start`, never `restart`: on a unit that is already running it is a no-op.
    That matters twice over with several badges, because restarting PipeWire
    under a connected badge takes it silent (TOS.md, Relay Host Hazards), and
    with two badges one of them is always the OTHER badge.
    """
    global _audio_services_ready
    if _audio_services_ready and not force:
        return
    print(f"[transceiver] ensuring PipeWire is running for {username}...")
    run_as_user(username, ["systemctl", "--user", "start",
                           "pipewire", "pipewire-pulse", "wireplumber"])
    _audio_services_ready = True


NULL_SINK   = "sdk_no_badge"
_null_lock  = threading.Lock()


def ensure_no_badge_default(username):
    """Never let a badge be the default sink or source.

    A stream whose target goes away is re-linked by PipeWire to the DEFAULT
    device. With one badge that was harmless. With two, on a host with no sound
    device of its own (PAN, a Pi), PipeWire elects the badges themselves as
    the defaults -- found on PAN 2026-09-26: default sink :28's speaker, default
    source :60's mic. One badge's answer would then play out of the other, and
    one badge's recording would hear the other.

    So whenever the default sink or source is a badge (a bluez_ node), point it
    at a null sink / that sink's monitor instead, and a fallback lands nowhere.
    A real device the user chose (laptop speakers, a USB mic) is left alone:
    falling back to it is the long-standing single-badge behaviour and hurts
    no badge.

    Not node.dont-fallback on the streams, which forbids the fallback outright
    and looks like the direct fix: it kills a stream whenever the HFP node is
    briefly re-created, which it is as SCO comes up (see listener.py,
    "Staying on this badge"). Measured with multiuser/xtalk_probe.py.

    Called at startup and after every badge's audio comes up, since that is
    when PipeWire may elect a new default. The null sink is a runtime module
    and does not survive a PipeWire restart; it is re-created here if missing.
    """
    with _null_lock:
        r = run_as_user(username, [PACTL, "list", "sinks", "short"])
        if r is None or r.returncode != 0:
            return
        if NULL_SINK not in r.stdout.split():
            run_as_user(username, [PACTL, "load-module", "module-null-sink",
                                   f"sink_name={NULL_SINK}",
                                   "sink_properties=device.description=SDK-no-badge"])
        for kind, target in (("sink", NULL_SINK), ("source", f"{NULL_SINK}.monitor")):
            r = run_as_user(username, [PACTL, f"get-default-{kind}"])
            current = (r.stdout.strip() if r else "")
            if current.startswith("bluez_"):
                run_as_user(username, [PACTL, f"set-default-{kind}", target])
                print(f"[transceiver] default {kind} was a badge ({current}) -- "
                      f"moved to {target}, so no badge's audio can fall back "
                      "onto another badge")


def ensure_audio_ready(mac, username):
    """Bring up the HFP audio profile on an ALREADY-CONNECTED badge.

    Returns True if the audio sink is ready, False if something timed out.

    Steps:
      1. Wait for PipeWire to register the card as bluez_card.<MAC_with_underscores>.
         BlueZ notifies PipeWire/WirePlumber via D-Bus; this registration is
         asynchronous and typically takes 1–3 s after connect.
      2. `pactl set-card-profile ... headset-head-unit` — switches the card
         from A2DP (stereo music) to HFP (hands-free phone), which opens the
         bidirectional 16 kHz SCO audio channel used for voice capture and
         badge speaker playback.
      3. Wait for the HFP audio sink (bluez_output.<MAC>.1) to appear in
         PipeWire.  Audio played before this point has nowhere to go.

    Why is this separate from paging?  Because a badge that connects ITSELF
    never goes through page_badge() at all, and this setup still has to
    happen. Folding these two together — as this file once did — means roughly
    a quarter of all links (see "Why two loops") skip the profile switch
    entirely and land on A2DP, where the badge microphone does not exist. Run
    this for EVERY link, however it was established.

    Why run pactl as the user?  PipeWire is a per-user service.  The root
    process can't reach the user's PipeWire session directly — it must use
    `runuser` to execute pactl inside the user's D-Bus/XDG environment. See
    run_as_user(): passing that environment is mandatory, and omitting it fails
    SILENTLY as "no cards" rather than as an error.
    """
    # PipeWire names the Bluetooth card with underscores replacing colons in the MAC.
    # Example: MAC 2C:F2:DF:45:EC:28 → bluez_card.2C_F2_DF_45_EC_28
    card = f"bluez_card.{mac.replace(':', '_')}"

    # Poll until PipeWire registers the card (up to ~15 s, 1 s intervals).
    saw_any_card = False
    for _ in range(15):
        r = run_as_user(username, [PACTL, "list", "cards", "short"])
        if r and r.stdout.strip():
            saw_any_card = True
        if r and card in r.stdout:
            break
        time.sleep(1)
    else:
        print(f"[transceiver] {mac}: timed out waiting for {card}")
        if not saw_any_card:
            # pactl reported NOTHING at all — not even the built-in sound card.
            # That is a session problem, not a badge problem, and saying so
            # here saves chasing a healthy badge around.
            print(f"[transceiver] ...and pactl listed NO cards whatsoever for "
                  f"{username}. That points at the PipeWire session, not the "
                  f"badge: check `XDG_RUNTIME_DIR={session_xdg(username)}` exists "
                  f"and `systemctl --user status pipewire wireplumber` as {username}.")
        return False

    print(f"[transceiver] {mac}: setting {card} to headset-head-unit")
    r = run_as_user(username, [PACTL, "set-card-profile", card, "headset-head-unit"])
    if r is None or r.returncode != 0:
        return False

    # The profile switch is asynchronous — poll until the HFP sink appears
    # (up to ~15 s).  listener.py also polls for the sink at each tap, but
    # confirming it here avoids launching listener.py before audio can route
    # to the badge.
    #
    # Sink naming: underscores in MAC + ".1" suffix.
    # Example: MAC 2C:F2:DF:45:EC:28 → bluez_output.2C_F2_DF_45_EC_28.1
    sink = f"bluez_output.{mac.replace(':', '_')}.1"
    for _ in range(15):
        r = run_as_user(username, [PACTL, "list", "sinks", "short"])
        if r and sink in r.stdout:
            print(f"[transceiver] {mac}: HFP sink ready.")
            return True
        time.sleep(1)
    print(f"[transceiver] {mac}: timed out waiting for HFP sink {sink}")
    return False


# ---------------------------------------------------------------------------
# PAGE loop
# ---------------------------------------------------------------------------

def page_badge(mac, addr, path):
    """Reach out to a badge that has not reached out to us, through ITS OWN
    adapter. PAGE loop only. Returns True if BlueZ reports the link came up.

    This is the expensive half of the split and the reason the split exists: if
    the badge is off or out of range, this call blocks for the controller's page
    timeout (~5 s) before failing. Nothing else may wait on it.

    Only ever the adapter the badge is assigned to, which is one it is paired
    on. Paging from any other adapter would at best fail, and at worst -- with
    a pairing agent registered, as phase 2 will have -- pair it there too.

    The return value is CHECKED by the caller. This function's predecessor
    issued the connect, ignored the result, and then polled for an audio card
    for a further 15 seconds — a card that cannot possibly appear when the
    connect just failed. Never poll for a side effect of an operation you did
    not confirm succeeded.
    """
    print(f"[transceiver] paging {mac} via {addr}...")
    ok, why = bz_call(path, DEVICE_IF, "Connect")
    if ok:
        return True
    # Surface WHY. Silently swallowing this is what turned a five-second
    # diagnosis into a long one on PAN, 2026-08-08. Common replies:
    #   br-connection-page-timeout         badge is off, asleep, or out of range
    #   br-connection-profile-unavailable  HFP not registered (see README §7)
    #   br-connection-busy / In Progress   badge is mid-reconnect; harmless
    #   AuthenticationFailed / canceled    pairing is stale — remove and re-pair
    print(f"[transceiver] {mac}: connect did not complete — {why}")
    return False


def page_loop():
    """PAGE loop (background thread): page each absent badge, in turn.

    Deliberately dumb. It pages, it waits, it pages again. All the intelligence
    lives in the coordinator, which is free to run fast precisely because this
    thread absorbs all the slow work.

    ONE thread, sequential, even with several adapters: each absent badge
    costs ~5 s, and paging on one adapter while another badge's audio is live
    on the next is radio contention nobody has measured yet (PI.md Ruling 5).
    Revisit with measurements, not before.

    The initial sleep is not padding: at startup the coordinator has not yet
    published its first observation, so without it a badge that is ALREADY
    connected gets pointlessly paged once on every launch.
    """
    global _sweep_start
    time.sleep(max(2.0, DETECT_INTERVAL + 1))
    while True:
        try:
            with _state_lock:
                absent = sorted((m, a, PAIRED[m][a]) for m, a in ASSIGNED.items()
                                if LIVE.get(m) is None and a in PAIRED.get(m, {}))
            absent = [t for t in absent if not is_quarantined(t[0])]
            if not absent:
                time.sleep(max(PAGE_GAP, 1.0))
                continue

            # ROTATE the starting point each sweep, so the badge in a drawer
            # does not always burn its ~5 s before the badge in your hand.
            k = _sweep_start % len(absent)
            _sweep_start += 1
            for mac, addr, path in absent[k:] + absent[:k]:
                with _state_lock:
                    still = LIVE.get(mac) is None and ASSIGNED.get(mac) == addr
                if still:
                    page_badge(mac, addr, path)   # coordinator takes it from here
            time.sleep(PAGE_GAP)
        except Exception as e:                      # keep the thread alive
            print(f"[transceiver] page loop error: {e}")
            time.sleep(5)


# ---------------------------------------------------------------------------
# CLAIMING -- a never-paired badge becomes this host's (multiuser/PI.md phase 2)
#
# Out of the box nobody pairs anything (Ruling 8): while an adapter has no
# badge of its own and new badge discovery is enabled, it scans that
# adapter for a TNG COMBADGE paired to NO adapter here, and pairs, trusts and
# connects it -- on that adapter only.  The pre-flight survey then sees the new
# pairing, a supervisor starts its listener, and the server, finding the badge
# unnamed, asks its wearer to name it (PI.md phase 3).  Power on, name, done.
#
# ONE bluetoothctl session per attempt, driven under a pty, the approach the
# Captain proposed: `select` the adapter, then scan / pair / trust / connect in
# the same session (select lasts only for the session it was typed in).
# Proven by multiuser/claim_probe.py on PAN, 2026-09-26: pair 5.4 s, trust
# 0.2 s, connect 0.5 s, NO agent prompt -- the NoInputNoOutput agent makes it
# silent.  A badge the host has unpaired goes straight back to discoverable.
# The rest of this file keeps using busctl; only claiming needs a session.
#
# Every badge found is claimed while an adapter is free (Ruling 1).  DISABLING
# (a file, so it survives a power cut) stops that: the server's "computer,
# disable new badge discovery" sends b'D' down a badge's downlink and its
# listener writes DISCOVERY_OFF_FILE; "computer, enable new badge discovery"
# removes it.
# ---------------------------------------------------------------------------

CLAIM_SCAN_S  = 20    # one scan window on a free adapter
CLAIM_GAP_S   = 30    # between windows: a free adapter is scanned ~40% of the time
CLAIM_NAME    = "TNG COMBADGE"
DISCOVERY_OFF_FILE = os.path.join(SCRIPT_DIR, "discovery_disabled.flag")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|[\x01\x02\r]")
_MACPAT = r"((?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2})"


def discovery_disabled():
    return os.path.exists(DISCOVERY_OFF_FILE)


class Bluetoothctl:
    """One interactive bluetoothctl session under a pty. Agent prompts --
    which the NoInputNoOutput agent should never raise -- are answered "yes"
    and logged loudly, since silence is the point of the design."""

    def __init__(self):
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            os.execvp(BTCTL_BIN, [BTCTL_BIN])
        self.buf = ""

    def send(self, cmd):
        os.write(self.fd, (cmd + "\n").encode())

    def lines(self, timeout):
        end, out = time.time() + timeout, []
        while time.time() < end:
            r, _, _ = select.select([self.fd], [], [], 0.1)
            if not r:
                continue
            try:
                self.buf += os.read(self.fd, 4096).decode("utf-8", "replace")
            except OSError:
                break
            while "\n" in self.buf:
                line, self.buf = self.buf.split("\n", 1)
                line = _ANSI.sub("", line).strip()
                if line:
                    out.append(line)
            tail = _ANSI.sub("", self.buf)
            if re.search(r"\(yes/no\):\s*$", tail):
                print(f"[transceiver] claim: AGENT PROMPT {tail.strip()!r} -- answering yes")
                self.buf = ""
                self.send("yes")
        return out

    def expect(self, patterns, timeout):
        """The first of `patterns` to appear, or None. The whole line that
        matched is kept in self.last, so a failure can be reported in
        bluetoothctl's own words."""
        end = time.time() + timeout
        self.last = ""
        while time.time() < end:
            for line in self.lines(0.2):
                for p in patterns:
                    if re.search(p, line, re.I):
                        self.last = line
                        return p
        return None

    def close(self):
        try:
            self.send("quit")
            self.lines(1)
        except OSError:
            pass
        try:
            os.close(self.fd)
        except OSError:
            pass
        try:
            os.kill(self.pid, signal.SIGTERM)
            os.waitpid(self.pid, 0)
        except (OSError, ChildProcessError):
            pass


def claim_on(adapter, known):
    """Scan `adapter` for CLAIM_SCAN_S for a combadge not in `known` (the MACs
    paired to any adapter here) and claim the first one: pair, trust, connect,
    on this adapter. Returns the MAC claimed, or None."""
    s = Bluetoothctl()
    try:
        s.lines(1)
        s.send("agent NoInputNoOutput")
        s.expect([r"Agent registered", r"already registered"], 5)
        s.send("default-agent")
        s.expect([r"Default agent request successful"], 5)
        s.send(f"select {adapter}")
        s.lines(0.5)
        s.send("scan on")
        found, end = None, time.time() + CLAIM_SCAN_S
        while time.time() < end and not found:
            for line in s.lines(0.3):
                m = re.search(rf"Device {_MACPAT}\b.*{CLAIM_NAME}", line, re.I)
                if m and m.group(1).upper() not in known:
                    found = m.group(1).upper()
                    break
        s.send("scan off")
        s.lines(0.5)
        if not found:
            return None
        print(f"[transceiver] claim: new badge {found} seen on {adapter} -- claiming it")
        s.send(f"pair {found}")
        if s.expect([r"Pairing successful", r"Failed to pair", r"AlreadyExists"], 40) \
                != r"Pairing successful":
            # bluetoothctl's own line, e.g. "Failed to pair:
            # org.bluez.Error.AuthenticationFailed" -- or nothing within 40 s.
            # The next scan window tries again (a first attempt failed once on
            # PAN, 2026-09-26, and the retry 36 s later succeeded).
            print(f"[transceiver] claim: pairing {found} on {adapter} FAILED -- "
                  f"{s.last or 'no reply within 40s'}; retrying next window")
            return None
        s.send(f"trust {found}")
        if not s.expect([r"trust succeeded"], 10):
            print(f"[transceiver] claim: {found} paired on {adapter} but trust FAILED "
                  "-- the pre-flight will retry it")
        s.send(f"connect {found}")
        ok = s.expect([r"Connection successful", r"Failed to connect"], 25)
        print(f"[transceiver] claim: {found} claimed on {adapter} (paired, trusted"
              + (", connected)" if ok == r"Connection successful" else "; connect failed, "
                 "the page loop will reach it)"))
        return found
    finally:
        s.close()


def claim_loop():
    """While an adapter has no badge of its own and the transceiver is not
    new badge discovery is enabled, look for a new badge on it. One adapter at a time, one
    bluetoothctl session at a time."""
    time.sleep(max(5.0, DETECT_INTERVAL * 2))   # let the first survey settle
    while True:
        try:
            if discovery_disabled():
                say_once("claim", "[transceiver] claiming: NEW BADGE DISCOVERY DISABLED -- "
                         f"no new badges will be accepted ({DISCOVERY_OFF_FILE} present).")
                time.sleep(CLAIM_GAP_S)
                continue
            with _state_lock:
                free = sorted(a for a in ADAPTERS if a not in ASSIGNED.values())
                known = set(PAIRED)
            if not free:
                say_once("claim", "[transceiver] claiming: every adapter has its badge -- "
                         "not scanning.")
                time.sleep(CLAIM_GAP_S)
                continue
            say_once("claim", f"[transceiver] claiming: open -- scanning {', '.join(free)} "
                     f"for a new badge ({CLAIM_SCAN_S:.0f}s every "
                     f"{CLAIM_SCAN_S + CLAIM_GAP_S:.0f}s).")
            claim_on(free[0], known)
        except Exception as e:
            print(f"[transceiver] claim loop error: {e}")
        time.sleep(CLAIM_GAP_S)


# ---------------------------------------------------------------------------
# Per-badge supervisor
# ---------------------------------------------------------------------------

def build_session_env(mac, adapter, hci, username):
    """Build the environment dictionary that listener.py needs to run correctly.

    When `runuser` and `sg` launch a subprocess, they strip the parent's
    environment.  Without the variables below, pw-play exits silently and
    PipeWire tools can't find the user's session.  We reconstruct the minimum
    required set explicitly:

      BADGE_MAC                — which badge this listener instance manages
      BADGE_ADAPTER            — the adapter its link is on, taken from the
                                 LIVE link at launch, not from any record of
                                 where it was paired. Scopes the tap node.
      BADGE_HCI                — that adapter's hciN, looked up now (it can
                                 renumber across boots). Scopes btmon.
      HOME / USER / LOGNAME    — basic identity expected by many Unix tools
      PATH                     — so listener.py can find ffmpeg, pw-play, pactl
      XDG_RUNTIME_DIR          — directory containing the user's PipeWire socket,
                                 typically /run/user/<uid>
      DBUS_SESSION_BUS_ADDRESS — how pw-play and pactl locate the user's D-Bus
                                 session and through it the PipeWire daemon
      SDK_SERVER_HOST/PORT     — forwarded from transceiver's own environment
                                 so users configure them in one place (here)
    """
    pw  = pwd.getpwnam(username)
    xdg = f"/run/user/{pw.pw_uid}"
    return {
        "BADGE_MAC":               mac,
        "BADGE_ADAPTER":           adapter,
        "BADGE_HCI":               hci,
        "HOME":                    pw.pw_dir,
        "USER":                    username,
        "LOGNAME":                 username,
        "PATH":                    os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "XDG_RUNTIME_DIR":         xdg,
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path={xdg}/bus",
        "SDK_SERVER_HOST":         os.environ.get("SDK_SERVER_HOST", "localhost"),
        "SDK_SERVER_PORT":         os.environ.get("SDK_SERVER_PORT", "1701"),
    }


def launch_listener(mac, username, listener_path, env):
    """Spawn listener.py as `username` with the `input` supplementary group.

    The command chain does two privilege adjustments in sequence:
      runuser -u <user> --  — drop from root to the specified user account
      sg input -c "<cmd>"  — add the `input` group to the new process's
                             supplementary groups, so it can open
                             /dev/input/eventX (badge HID device), which is
                             typically owned root:input with mode 0660.

    We pass `env=` explicitly because runuser/sg strip the environment and
    the child needs the session variables assembled by build_session_env().
    """
    # -u: unbuffered, so the listener's lines reach the console as they
    # happen. Block-buffered, a killed listener takes its last few KB of
    # output with it -- which is exactly the part that explains the kill.
    cmd = [RUNUSER, "-u", username, "--", "sg", "input", "-c",
           f"{sys.executable} -u {listener_path}"]
    print(f"[transceiver] launching listener.py for {mac} on "
          f"{env['BADGE_ADAPTER']} ({env['BADGE_HCI']}) as {username}")
    return subprocess.Popen(cmd, env=env)


def _stop(proc):
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


class BadgeSupervisor(threading.Thread):
    """One badge's listener.py, for as long as the badge is assigned.

    The single-badge transceiver's detect loop, per badge: bring up HFP on a
    new link, launch the listener, restart it if it dies, stop it when the link
    goes. Its own thread so one badge's slow path (a 30 s card timeout) never
    holds up another's.

    It acts only when the badge is connected on the adapter it is ASSIGNED to.
    Connected anywhere else means the coordinator is about to evict it or
    remove a pairing; the listener waits for that to settle.
    """

    def __init__(self, mac, username, listener_path):
        super().__init__(daemon=True, name=f"sup-{mac}")
        self.mac, self.username, self.listener_path = mac, username, listener_path
        self.stop_evt = threading.Event()
        self.proc     = None
        self.adapter  = None   # adapter the running listener was scoped to

    def _launch(self, adapter):
        with _state_lock:
            hci = ADAPTERS.get(adapter, {}).get("hci", "")
        env = build_session_env(self.mac, adapter, hci, self.username)
        self.proc    = launch_listener(self.mac, self.username, self.listener_path, env)
        self.adapter = adapter
        with _state_lock:
            RUNNING.add(self.mac)

    def _halt(self, why=None):
        if self.proc and self.proc.poll() is None and why:
            print(f"[transceiver] {self.mac}: {why}, stopping listener.py")
        _stop(self.proc)
        self.proc, self.adapter = None, None
        with _state_lock:
            RUNNING.discard(self.mac)

    def run(self):
        mac = self.mac
        try:
            while not self.stop_evt.is_set():
                with _state_lock:
                    live, mine = LIVE.get(mac), ASSIGNED.get(mac)
                adapter = live if live and live == mine else None

                if adapter is None:
                    self._halt("disconnected" if not live else f"moved to {live}")
                elif adapter != self.adapter:
                    # New link — paged by us, or the badge powered on and
                    # paged us. Both arrive here, which is the point.
                    self._halt(f"link moved {self.adapter} -> {adapter}")
                    print(f"[transceiver] badge online: {mac} on {adapter}")
                    ok = ensure_audio_ready(mac, self.username)
                    if not ok:
                        # A dead PipeWire session looks exactly like a dead
                        # badge from here; start (never restart) it and retry
                        # once before writing the badge off.
                        print(f"[transceiver] {mac}: retrying after ensuring the "
                              "audio stack is up...")
                        ensure_audio_services(self.username, force=True)
                        ok = ensure_audio_ready(mac, self.username)
                    if not ok:
                        print(f"[transceiver] {mac} is connected but exposes no "
                              f"audio card — disconnecting and standing it down "
                              f"for {UNUSABLE_COOLDOWN}s.")
                        with _state_lock:
                            path = PAIRED.get(mac, {}).get(adapter)
                        if path:
                            bz_call(path, DEVICE_IF, "Disconnect")
                        quarantine(mac)
                    else:
                        ensure_no_badge_default(self.username)
                        self._launch(adapter)
                elif self.proc and self.proc.poll() is not None:
                    print(f"[transceiver] {mac}: listener.py exited "
                          f"({self.proc.returncode}); restarting")
                    self._launch(adapter)
                self.stop_evt.wait(DETECT_INTERVAL)
        except Exception as e:
            print(f"[transceiver] {mac}: supervisor error: {e}")
        finally:
            self._halt()


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------

def _fmt_assignment(assigned, adapters):
    if not assigned:
        return "(none)"
    return ", ".join(f"{m} -> {a} ({adapters.get(a, {}).get('hci', '?')})"
                     for m, a in sorted(assigned.items()))


def main():
    # Must run as root to use runuser and sg input.
    if os.geteuid() != 0:
        sys.exit("transceiver.py must run as root (needs runuser + sg input).")

    # Prefer SUDO_USER (set automatically when invoked via sudo) over SDK_USER.
    username = os.environ.get("SUDO_USER") or os.environ.get("SDK_USER")
    if not username:
        sys.exit("Set SDK_USER=<your user> or run via sudo (which sets SUDO_USER).")

    # Default: look for listener.py in the same directory as this script.
    listener_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "listener.py")
    if not os.path.isfile(listener_path):
        sys.exit(f"listener.py not found at {listener_path}")

    if os.environ.get("SDK_BADGE_MAC"):
        print("[transceiver] NOTE: SDK_BADGE_MAC is no longer used and is "
              "ignored. Every badge paired to this host is supervised; unpair "
              "a badge to keep it out.")

    # SIGTERM (systemd, pkill) must stop the listeners too, not orphan them.
    def _term(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _term)

    # A headless/SSH-only relay host may have no PipeWire session running yet.
    # Once, at startup — see ensure_audio_services() for why not per-pass.
    ensure_audio_services(username)
    ensure_no_badge_default(username)

    threading.Thread(target=page_loop, daemon=True, name="page").start()
    threading.Thread(target=claim_loop, daemon=True, name="claim").start()
    print("[transceiver] new badge discovery: " + ("DISABLED -- no new badges will be accepted"
                                                   if discovery_disabled() else "enabled -- new badges are claimed"))

    print(f"[transceiver] detect every {DETECT_INTERVAL}s, page every "
          f"~{PAGE_GAP}s + page timeout per absent badge; one badge per adapter")

    supervisors = {}
    last_tree, last_survey = None, 0.0
    adapters, paired = {}, {}

    try:
        while True:
            tree = bluez_tree()
            if tree is None:
                say_once("bluez", "[transceiver] BlueZ is not answering on D-Bus "
                                  "(is bluetoothd running?)")
                time.sleep(max(DETECT_INTERVAL, 1.0))
                continue
            unsay("bluez")

            # Pre-flight: on any change to BlueZ's objects, and periodically.
            if tree != last_tree or time.time() - last_survey > SURVEY_INTERVAL:
                adapters, paired = survey(tree)
                last_tree, last_survey = tree, time.time()
                say_once("adapters", "[transceiver] adapters: " + (
                    ", ".join(f"{a} ({v['hci']})" for a, v in sorted(adapters.items()))
                    or "NONE"))

            live = observe(paired)
            with _state_lock:
                previous, running = dict(ASSIGNED), set(RUNNING)
            assigned, evict, orphans = assign(adapters, paired, live, previous, running)
            with _state_lock:
                ADAPTERS.clear(); ADAPTERS.update(adapters)
                PAIRED.clear();   PAIRED.update(paired)
                ASSIGNED.clear(); ASSIGNED.update(assigned)
                LIVE.clear();     LIVE.update(live)

            say_once("assign", "[transceiver] assignment: "
                     + _fmt_assignment(assigned, adapters))
            if orphans:
                say_once("orphans", f"[transceiver] no adapter of its own for "
                         f"{', '.join(orphans)} ({len(paired)} badges paired, "
                         f"{len(adapters)} adapters) -- left unconnected. Two "
                         "badges never share an adapter.")
            else:
                unsay("orphans")
            update_health(adapters, orphans)
            if not adapters:
                say_once("idle", "[transceiver] no Bluetooth adapter is available.")
            elif not paired:
                say_once("idle", "[transceiver] no TNG COMBADGE is paired to any "
                                 "adapter on this host.")
            else:
                unsay("idle")

            if enforce(assigned, evict, paired, live):
                last_tree = None    # BlueZ changed under us: survey next pass

            # One supervisor per assigned badge; retire the rest.
            for mac in assigned:
                if mac not in supervisors or not supervisors[mac].is_alive():
                    supervisors[mac] = BadgeSupervisor(mac, username, listener_path)
                    supervisors[mac].start()
            for mac in [m for m in supervisors if m not in assigned]:
                supervisors.pop(mac).stop_evt.set()

            time.sleep(DETECT_INTERVAL)

    except KeyboardInterrupt:
        print("[transceiver] shutting down")
        for sup in supervisors.values():
            sup.stop_evt.set()
        for sup in supervisors.values():
            sup.join(timeout=8)


if __name__ == "__main__":
    main()
