#!/usr/bin/env python3
"""
Minimal Combadge Transceiver / Connection Manager (TOS SDK).

This script runs as ROOT and does one job: keep the combadge connected
and keep listener.py running under the correct user account.

Why root?
  - `runuser` (drop to a normal user account) requires root.
  - `sg input -c ...` (add the `input` supplementary group so listener.py
    can open /dev/input/eventX) also requires root unless the caller is
    already in `input`.  Running as the user directly would require adding
    them to the `input` group permanently, which is a larger system change.

TWO loops, not one (see "Why two loops" below):

  DETECT (main thread, every SDK_DETECT_INTERVAL seconds)
    1. Is the badge connected?  (one cheap bluetoothctl query)
    2. If it just connected: bring up HFP, launch listener.py.
    3. If it just disconnected: stop listener.py.
    4. Restart listener.py if it exited unexpectedly.

  PAGE (background thread, every SDK_PAGE_GAP seconds while disconnected)
    5. `bluetoothctl connect <MAC>` — reach out to a badge that has not
       reached out to us.

Usage:
    sudo SDK_USER=$USER python3 transceiver.py [/path/to/listener.py]

Environment variables:
    SDK_USER            — the username to run listener.py as (required if not
                          using sudo; sudo sets SUDO_USER automatically)
    SDK_SERVER_HOST     — hostname/IP where computer.py is running (default: localhost)
    SDK_SERVER_PORT     — TCP port for computer.py (default: 1701)
    SDK_DETECT_INTERVAL — seconds between connectivity checks (default: 2)
    SDK_PAGE_GAP        — seconds between connect attempts while the badge is
                          absent (default: 5)
    SDK_BADGE_MAC       — optional MAC (or comma-separated MACs) to accept.
                          Unset = any paired TNG COMBADGE. Set this when more
                          than one badge is paired to the host: all TNG badges
                          share the same device NAME, so name matching alone
                          cannot distinguish the badge you are carrying from the
                          one in a drawer.

-----------------------------------------------------------------------------
WHY TWO LOOPS  (the single most important thing in this file)
-----------------------------------------------------------------------------
The obvious design is one loop that checks, then connects, then sleeps. Do not
write that. It is what this file used to be, and it is slow for a reason that
is invisible until you measure it.

`bluetoothctl connect` against a badge that is switched off or out of range
does not fail fast. It blocks for the controller's **page timeout** — BlueZ's
default is 0x2000 slots x 0.625 ms = **5.12 seconds** — before reporting
failure. Meanwhile, checking whether a badge is connected is a D-Bus property
read costing a few milliseconds.

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

  * Paging costs the HOST, not the badge. `bluetoothctl connect` transmits page
    trains from the host radio. The badge sits in page scan at a duty cycle
    fixed by its own firmware; it cannot tell how often you page it.
  * What costs the BADGE is SCO: bringing the audio link up, playing through
    its speaker, tearing it down. That is the badge's highest-power activity by
    a wide margin. If you want to save badge battery, look at how often you
    play audio to it — not at how often you poll.
  * What costs the HOST is SDK_DETECT_INTERVAL. Each pass forks a subprocess
    and does a D-Bus round trip. At 2 s that is a rounding error; at 0 it is a
    busy loop that keeps a core warm forever. On a battery-powered relay host
    (a Raspberry Pi, a laptop) keep it at >= 0.5. If you need instant detection
    without polling at all, the right answer is not a tighter loop — it is a
    D-Bus signal subscription on org.bluez.Device1's `Connected` property.

Stripped down from relay/combadge.py: no per-host PID files, no IPC flags,
no log file rotation, no authorized-badge filtering, no focus-shift handling.
Single badge, single listener process, foreground.
"""
import os
import pwd
import subprocess
import sys
import threading
import time

# How often the DETECT loop checks connectivity. This is the reconnect latency
# you actually feel. See "Battery" above before setting it below 0.5.
DETECT_INTERVAL = max(0.0, float(os.environ.get("SDK_DETECT_INTERVAL", "2")))

# How long the PAGE loop waits between connect attempts while the badge is away.
# Each attempt against an absent badge costs ~5 s of page timeout regardless of
# this value, so the effective retry period is roughly 5 s + SDK_PAGE_GAP.
PAGE_GAP = max(0.0, float(os.environ.get("SDK_PAGE_GAP", "5")))

# The paired-device list changes only when you pair or remove a badge, so it is
# cached rather than re-queried on every detect pass (two bluetoothctl calls).
PAIRED_CACHE_TTL = 60  # seconds

# Optional badge allow-list. Every TNG COMBADGE shares the same device NAME, so
# name matching alone cannot tell two badges apart — set SDK_BADGE_MAC to pin a
# specific one (or several, comma-separated):
#
#     sudo SDK_BADGE_MAC=1B:B8:82:88:2F:60 python3 transceiver.py
#
# Leave it unset to accept any paired TNG COMBADGE, which is the right default
# for a single-badge setup. With two badges paired and only one switched on,
# pinning the live one skips a wasted ~5 s page of the absent one per sweep.
WANTED_MACS = {m.strip().upper()
               for m in os.environ.get("SDK_BADGE_MAC", "").split(",")
               if m.strip()}

# --- Shared state between the two loops ---------------------------------
# The detect loop writes; the page loop reads it to decide whether to page at
# all. A plain lock is sufficient — there are exactly two threads and one flag.
_state_lock = threading.Lock()
_badge_connected = False
_audio_services_ready = False
_sweep_start = 0        # rotates which badge the page sweep tries first

# How long to stand a badge down after it proves unusable. See quarantine().
UNUSABLE_COOLDOWN = 60  # seconds
_cooldown      = {}     # MAC (upper) -> timestamp until which it is skipped
_cooldown_lock = threading.Lock()


def quarantine(mac, seconds=UNUSABLE_COOLDOWN):
    """Stand a badge down temporarily so the others get a turn.

    Needed because "connected" and "usable" are NOT the same thing. BlueZ will
    happily hold an ACL link open to a badge whose audio profile never came up
    (`br-connection-profile-unavailable` — see the troubleshooting notes in
    relay/RELAY.md). Such a badge reports `Connected: yes` forever while no
    `bluez_card.<MAC>` ever appears.

    Without a quarantine that state is a LIVELOCK: the detect loop keeps
    selecting the half-connected badge, waits out the 15 s card poll, fails,
    and tries the same badge again — while the page loop, told a badge is
    connected, stands down and never pages the badge you are actually holding.
    Observed on PAN 2026-08-08 with two badges paired and only one switched on.
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
# shared mount, live-readable from CUBE.
# ---------------------------------------------------------------------------

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR    = os.path.join(SCRIPT_DIR, "log")
HOSTNAME   = os.uname().nodename
LOG_FILE   = os.path.join(LOG_DIR, f"transceiver_{HOSTNAME}.log")
try:
    os.makedirs(LOG_DIR, exist_ok=True)
except OSError:
    pass

_print = print


def print(*args, **kwargs):   # noqa: A001 — deliberate shadow, see above
    line    = " ".join(str(a) for a in args)
    stamped = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [{HOSTNAME}] {line}"
    _print(stamped, **kwargs)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(stamped + "\n")
    except OSError:
        pass

# Full paths to system tools.  Adjust if your distro puts them elsewhere.
BTCTL   = "/usr/bin/bluetoothctl"   # BlueZ command-line interface
PACTL   = "/usr/bin/pactl"          # PipeWire/PulseAudio control tool
RUNUSER = "/usr/sbin/runuser"       # Run a command as a different user (needs root)


def run(cmd, **kw):
    """Run a shell command and return the CompletedProcess, or None on failure.

    Swallows TimeoutExpired and FileNotFoundError so callers never need to
    handle the case where a system tool is missing or unresponsive.
    capture_output=True prevents system tool stdout/stderr from leaking into
    our console output.
    """
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=15, **kw)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None


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


def ensure_audio_services(username, force=False):
    """Start the user's PipeWire stack. Startup only, and again on fault.

    Ported from relay/combadge.py. A headless or SSH-only relay host often has
    no running PipeWire session at all until something asks for one, and
    WirePlumber's Bluetooth monitor additionally gates on logind reporting the
    seat active — see the wireplumber seat-monitoring note in README §7 if the
    card still never appears after this.

    Deliberately NOT called from the detect loop. `systemctl --user start` on an
    already-running unit is a no-op, but it is the one call here with any route
    to disturbing the audio stack, and at a 2 s cadence it would run 30 times a
    minute. Once at startup, then only when something has actually gone wrong.
    """
    global _audio_services_ready
    if _audio_services_ready and not force:
        return
    print(f"[transceiver] ensuring PipeWire is running for {username}...")
    run_as_user(username, ["systemctl", "--user", "start",
                           "pipewire", "pipewire-pulse", "wireplumber"])
    _audio_services_ready = True


def find_paired_badges():
    """Scan bluetoothctl's device lists for TNG COMBADGEs.

    Returns a list of MAC address strings (e.g. ["2C:F2:DF:45:EC:28"]), in the
    order bluetoothctl reports them, filtered by WANTED_MACS if you set
    SDK_BADGE_MAC.  Empty list if none are paired.

    Tries `paired-devices` first (only shows fully paired devices, faster),
    then falls back to `devices` which also lists devices seen in recent
    scans.  The name match is case-insensitive.

    Each bluetoothctl output line looks like:
        Device 2C:F2:DF:45:EC:28 TNG COMBADGE
    We take parts[1] (the MAC) when "TNG COMBADGE" appears anywhere in the line.

    Why a LIST and not the first match?  Because this used to return the first
    match and stop, which is wrong the moment you own two badges. Every TNG
    COMBADGE has the same device NAME, so "the first one named TNG COMBADGE" is
    whichever one bluetoothctl happens to print first — not the one that is
    switched on. Pair two, carry one, and the transceiver will spend forever
    paging the badge sitting in a drawer while the badge in your hand is
    ignored. Collect them all; let the caller try each.
    """
    found, truly_paired = [], set()
    for sub in (["paired-devices"], ["devices"]):
        r = run([BTCTL] + sub)
        if not r:
            continue
        for line in r.stdout.splitlines():
            if "TNG COMBADGE" in line.upper():
                parts = line.split()
                if len(parts) < 2:
                    continue
                mac = parts[1]
                if sub[0] == "paired-devices":
                    truly_paired.add(mac.upper())
                if mac not in found:
                    if not WANTED_MACS or mac.upper() in WANTED_MACS:
                        found.append(mac)  # the MAC address field

    # `devices` lists everything BlueZ has seen, not just what is paired to this
    # host. A badge paired to a DIFFERENT host (a phone, another relay box) shows
    # up here and can be paged forever — every attempt failing with "Device not
    # available" — while looking, in the log, exactly like a badge that is merely
    # out of range. Say so once, plainly, instead of letting it masquerade.
    for mac in found:
        if mac.upper() not in truly_paired and mac.upper() not in _warned_unpaired:
            _warned_unpaired.add(mac.upper())
            print(f"[transceiver] NOTE: {mac} is visible but NOT PAIRED to this host "
                  "— connects will fail until you pair it "
                  f"(bluetoothctl pair {mac}; trust {mac}). "
                  "If it is paired to another device (phone, second relay), "
                  "disconnect it there first.")
    return found


_paired_cache    = []
_paired_cache_ts = 0.0
_warned_unpaired = set()   # MACs we have already warned about, so it is said once


def find_paired_badges_cached():
    """find_paired_badges() behind a PAIRED_CACHE_TTL cache.

    The detect loop runs every couple of seconds and the uncached call is two
    bluetoothctl invocations. An EMPTY result is deliberately never cached: a
    host with no badge paired yet must notice the moment you pair one.
    """
    global _paired_cache, _paired_cache_ts
    if not _paired_cache or (time.time() - _paired_cache_ts) > PAIRED_CACHE_TTL:
        _paired_cache    = find_paired_badges()
        _paired_cache_ts = time.time()
    return list(_paired_cache)


def live_badges():
    """Paired badges that are not currently standing down (see quarantine())."""
    return [m for m in find_paired_badges_cached() if not is_quarantined(m)]


# `bluetoothctl devices Connected` needs BlueZ >= 5.65. We probe once and
# remember the answer; older builds fall back to `info <MAC>`, which is what
# this file used to do unconditionally. None = not yet probed.
_devices_connected_supported = None


def connected_badge(macs):
    """Return the first MAC in `macs` that is currently connected, or None.

    This is the DETECT pass and the only thing standing between a badge coming
    back and the SDK noticing, so it is kept cheap — ONE query answers the
    question for every badge at once on BlueZ >= 5.65. Both paths below are
    property reads over D-Bus; neither transmits anything on the radio, which is
    why calling this every 2 s is free and calling `bluetoothctl connect` every
    2 s would not be.

    Several badges may be PAIRED; the expected pattern is that one is switched
    on at a time and you swap between them (see "Two badges, one at a time" in
    README §4). First-connected therefore wins, and there is normally only one
    candidate anyway.
    """
    global _devices_connected_supported
    if not macs:
        return None
    by_upper = {m.upper(): m for m in macs}

    if _devices_connected_supported is not False:
        r = run([BTCTL, "devices", "Connected"])
        if r and r.returncode == 0 and "Invalid" not in (r.stdout + r.stderr):
            live = set()
            for line in r.stdout.splitlines():
                p = line.split()
                if len(p) >= 2 and p[0] == "Device" and p[1].upper() in by_upper:
                    live.add(p[1].upper())
            hit = next((m for m in macs if m.upper() in live), None)
            # Cross-validate ONCE, on the first positive answer. A returncode of
            # 0 is not proof the filter was honoured: some bluetoothctl builds
            # ignore an unrecognised `devices` argument and print the FULL device
            # list instead of erroring, which would make every paired badge look
            # permanently connected. One `info` call settles it; if the two
            # disagree, the fast path is wrong and we never use it again.
            if hit is not None and _devices_connected_supported is None:
                v = run([BTCTL, "info", hit])
                if v and "Connected: yes" in v.stdout:
                    _devices_connected_supported = True      # trusted from now on
                else:
                    _devices_connected_supported = False
                    print("[transceiver] `devices Connected` is not filtering "
                          "(it listed a disconnected badge) — using `info` probe.")
                    hit = None                               # fall through below
            if _devices_connected_supported is not False:
                return hit
        else:
            _devices_connected_supported = False
            print("[transceiver] `devices Connected` unsupported; using `info` probe.")

    for mac in macs:
        r = run([BTCTL, "info", mac])
        if r and "Connected: yes" in r.stdout:
            return mac
    return None


def page_badge(mac):
    """Reach out to a badge that has not reached out to us. PAGE loop only.

    Returns True if bluetoothctl reports the link came up.

    This is the expensive half of the split and the reason the split exists: if
    the badge is off or out of range, this call blocks for the controller's page
    timeout (~5 s) before failing. Nothing else may wait on it.

    NOTE the return value is CHECKED by the caller. It did not used to be: this
    function's predecessor issued the connect, ignored the result, and then
    polled for an audio card for a further 15 seconds — a card that cannot
    possibly appear when the connect just failed. That made every failed retry
    cost ~20 s of dead time on top of the 5 s page timeout. If you take one
    practical lesson from this file, take that one: never poll for a
    side effect of an operation you did not confirm succeeded.
    """
    print(f"[transceiver] paging {mac}...")
    r = run([BTCTL, "connect", mac])
    if r is None:
        print(f"[transceiver] {mac}: bluetoothctl did not respond (missing or hung)")
        return False

    out = (r.stdout or "") + (r.stderr or "")

    # Do NOT trust the exit code. bluetoothctl exits 0 on plenty of failures,
    # so a returncode check reports phantom successes — which looks exactly
    # like a badge that connects and instantly vanishes. The success banner is
    # the reliable signal.
    if "Connection successful" in out:
        return True

    # Surface WHY. Silently swallowing this is what turned a five-second
    # diagnosis into a long one on PAN, 2026-08-08: the log said "paging..."
    # over and over and never once said what bluetoothctl replied. Common
    # replies and what they mean:
    #   br-connection-page-timeout      badge is off, asleep, or out of range
    #   br-connection-profile-unavailable  HFP not registered (see README §7)
    #   br-connection-busy              badge is mid-reconnect; harmless, retries
    #   Device <MAC> not available      NOT PAIRED to this host, or unknown
    #   AuthenticationFailed / canceled pairing is stale — remove and re-pair
    reason = next((ln.strip() for ln in out.splitlines()
                   if any(k in ln for k in ("Failed", "failed", "Error", "not available"))),
                  "no reason reported")
    print(f"[transceiver] {mac}: connect did not complete — {reason}")
    return False


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
         PipeWire.  Audio played before this point falls back to the default
         output (laptop speakers) rather than the badge.

    Why is this separate from page_badge()?  Because a badge that connects
    ITSELF never goes through page_badge() at all, and this setup still has to
    happen. Folding these two together — as this file used to — means roughly a
    quarter of all links (see "Why two loops") skip the profile switch entirely
    and land on A2DP, where the badge microphone does not exist. listener.py's
    own ensure_hfp_profile() papers over it at the first tap, but the badge is
    silently in the wrong state until then. Run this for EVERY link, however it
    was established.

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
        print(f"[transceiver] timed out waiting for {card}")
        if not saw_any_card:
            # pactl reported NOTHING at all — not even the built-in sound card.
            # That is a session problem, not a badge problem, and saying so
            # here saves chasing a healthy badge around.
            print(f"[transceiver] ...and pactl listed NO cards whatsoever for "
                  f"{username}. That points at the PipeWire session, not the "
                  f"badge: check `XDG_RUNTIME_DIR={session_xdg(username)}` exists "
                  f"and `systemctl --user status pipewire wireplumber` as {username}.")
        return False

    print(f"[transceiver] setting {card} to headset-head-unit")
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
    print(f"[transceiver] waiting for HFP sink...")
    for _ in range(15):
        r = run_as_user(username, [PACTL, "list", "sinks", "short"])
        if r and sink in r.stdout:
            print(f"[transceiver] HFP sink ready.")
            return True
        time.sleep(1)
    print(f"[transceiver] timed out waiting for HFP sink {sink}")
    return False


def page_loop():
    """PAGE loop (background thread): page the badge while it is absent.

    Deliberately dumb. It pages, it waits, it pages again. All the intelligence
    — is a badge here, does it need setting up, does listener.py need starting —
    lives in the detect loop, which is free to run fast precisely because this
    thread absorbs all the slow work.

    The initial sleep is not padding: at startup the detect loop has not yet
    published its first observation, so without it a badge that is ALREADY
    connected gets pointlessly paged once on every launch.
    """
    time.sleep(2)
    while True:
        try:
            with _state_lock:
                connected = _badge_connected
            if connected:
                time.sleep(max(PAGE_GAP, 1.0))
                continue

            macs = live_badges()
            if not macs:
                time.sleep(max(PAGE_GAP, 5.0))
                continue

            # Page each paired badge in turn. Only one is expected to be switched
            # on; the others cost ~5 s of page timeout each, which is exactly
            # why this runs here and not on the detect path. Re-check between
            # badges so a link that comes up mid-sweep is not made to wait out
            # the remaining pages.
            #
            # ROTATE the starting point each sweep. Without this the list order
            # is fixed, so the badge you just switched OFF is always paged first
            # and always burns its full ~5 s page timeout before the badge you
            # just switched ON is even tried — the exact swap you perform most
            # often, made as slow as possible. Rotating shares first position out
            # evenly and roughly halves the average swap time.
            global _sweep_start
            macs = macs[_sweep_start % len(macs):] + macs[:_sweep_start % len(macs)]
            _sweep_start += 1

            for mac in macs:
                with _state_lock:
                    if _badge_connected:
                        break
                if page_badge(mac):
                    break   # detect loop takes it from here. One owner per job.
            time.sleep(PAGE_GAP)
        except Exception as e:                      # keep the thread alive
            print(f"[transceiver] page loop error: {e}")
            time.sleep(5)


def build_session_env(mac, username):
    """Build the environment dictionary that listener.py needs to run correctly.

    When `runuser` and `sg` launch a subprocess, they strip the parent's
    environment.  Without the variables below, pw-play exits silently and
    PipeWire tools can't find the user's session.  We reconstruct the minimum
    required set explicitly:

      BADGE_MAC                — which badge this listener instance manages
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
    cmd = [RUNUSER, "-u", username, "--", "sg", "input", "-c",
           f"{sys.executable} {listener_path}"]
    print(f"[transceiver] launching listener.py for {mac} as {username}")
    return subprocess.Popen(cmd, env=env)


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

    global _badge_connected

    listener_proc = None
    current_mac   = None
    last_idle_msg = 0.0

    # A headless/SSH-only relay host may have no PipeWire session running yet.
    # Once, at startup — see ensure_audio_services() for why not per-pass.
    ensure_audio_services(username)

    # Start the PAGE loop. Daemon, so Ctrl-C kills it with the process.
    threading.Thread(target=page_loop, daemon=True).start()

    print(f"[transceiver] detect every {DETECT_INTERVAL}s, page every "
          f"~{PAGE_GAP}s + page timeout while absent")
    print("[transceiver] badge filter: " +
          (", ".join(sorted(WANTED_MACS)) if WANTED_MACS
           else "any paired TNG COMBADGE (set SDK_BADGE_MAC to pin one)"))

    # DETECT loop. Note what is NOT here: no bluetoothctl connect. This loop
    # only ever observes and reacts, which is what keeps it fast.
    try:
        while True:
            macs = live_badges()
            if not macs:
                # Throttled: at a 2 s cadence an unconditional print would
                # scroll the console into uselessness.
                if time.time() - last_idle_msg > 30:
                    if find_paired_badges_cached():
                        print("[transceiver] all paired badges are standing down "
                              "(connected but no audio card) — waiting for cooldown.")
                    elif WANTED_MACS:
                        print("[transceiver] none of SDK_BADGE_MAC "
                              f"({', '.join(sorted(WANTED_MACS))}) is paired — "
                              "pair it with bluetoothctl, or unset SDK_BADGE_MAC.")
                    else:
                        print("[transceiver] no paired TNG COMBADGE — pair one with bluetoothctl.")
                    last_idle_msg = time.time()
                time.sleep(max(DETECT_INTERVAL, 1.0))
                continue

            mac = connected_badge(macs)
            with _state_lock:
                _badge_connected = mac is not None   # tells the page loop to stand down

            if not mac:
                # Badge gone. Stop listener.py and let the page loop do its work.
                if listener_proc and listener_proc.poll() is None:
                    print(f"[transceiver] {current_mac} disconnected, stopping listener.py")
                    listener_proc.terminate()
                    listener_proc.wait(timeout=5)
                listener_proc = None
                current_mac   = None
                time.sleep(DETECT_INTERVAL)
                continue

            # New link — however it was established: paged by us, or the badge
            # powered on and paged us. Both arrive here, which is the point.
            # This is also the badge-SWAP path: switch one badge off and another
            # on, and the handover happens here with no restart.
            if mac != current_mac:
                if current_mac:
                    print(f"[transceiver] badge changed: {current_mac} -> {mac}")
                else:
                    print(f"[transceiver] badge online: {mac}")
                if listener_proc and listener_proc.poll() is None:
                    listener_proc.terminate()
                    listener_proc.wait(timeout=5)
                ok = ensure_audio_ready(mac, username)
                if not ok:
                    # Before writing the badge off, restart the user's audio
                    # stack once and try again. A dead PipeWire session looks
                    # exactly like a dead badge from here, and the badge is the
                    # more expensive thing to wrongly discard.
                    print("[transceiver] retrying after restarting the audio stack...")
                    ensure_audio_services(username, force=True)
                    ok = ensure_audio_ready(mac, username)
                if not ok:
                    # Connected but NOT usable — no audio card ever appeared.
                    # Disconnect to clear the half-open link, stand this badge
                    # down, and release the page loop so the OTHER badge gets
                    # paged. Retrying the same badge here instead (which this
                    # code originally did) is a livelock: the page loop stays
                    # parked because a badge is "connected", and the badge you
                    # are actually holding is never reached.
                    print(f"[transceiver] {mac} is connected but exposes no audio "
                          f"card — disconnecting and standing it down for "
                          f"{UNUSABLE_COOLDOWN}s so other badges get a turn.")
                    run([BTCTL, "disconnect", mac])
                    quarantine(mac)
                    with _state_lock:
                        _badge_connected = False
                    current_mac = None
                    time.sleep(DETECT_INTERVAL)
                    continue
                env           = build_session_env(mac, username)
                listener_proc = launch_listener(mac, username, listener_path, env)
                current_mac   = mac

            # listener.py crashed or exited cleanly — restart it.
            if listener_proc and listener_proc.poll() is not None:
                print(f"[transceiver] listener.py exited ({listener_proc.returncode}); restarting")
                env           = build_session_env(mac, username)
                listener_proc = launch_listener(mac, username, listener_path, env)

            time.sleep(DETECT_INTERVAL)

    except KeyboardInterrupt:
        print("\n[transceiver] shutting down")
        if listener_proc and listener_proc.poll() is None:
            listener_proc.terminate()


if __name__ == "__main__":
    main()
