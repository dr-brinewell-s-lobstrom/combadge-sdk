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

What it does in a loop:
  1. Find a paired device named "TNG COMBADGE" via bluetoothctl.
  2. Connect it and wait for BlueZ to register the audio card with PipeWire.
  3. Switch the card profile to `headset-head-unit` (HFP bidirectional audio).
  4. Wait for the HFP audio sink to appear in PipeWire.
  5. Launch listener.py as the logged-in user with a clean session environment.
  6. Monitor listener.py; restart it if it exits unexpectedly.
  7. If the badge disconnects, stop listener.py and reconnect.

Usage:
    sudo SDK_USER=$USER python3 transceiver.py [/path/to/listener.py]

Environment variables:
    SDK_USER         — the username to run listener.py as (required if not
                       using sudo; sudo sets SUDO_USER automatically)
    SDK_SERVER_HOST  — hostname/IP where computer.py is running (default: localhost)
    SDK_SERVER_PORT  — TCP port for computer.py (default: 1701)

Stripped down from relay/combadge.py: no per-host PID files, no IPC flags,
no log file rotation, no authorized-badge filtering, no focus-shift handling.
Single badge, single listener process, foreground.
"""
import os
import pwd
import subprocess
import sys
import time

# How often to check whether the badge is still connected and listener.py is alive.
CHECK_INTERVAL = 2  # seconds

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


def find_paired_badge():
    """Scan bluetoothctl's device lists for a TNG COMBADGE.

    Returns the MAC address string (e.g. "2C:F2:DF:45:EC:28") or None.

    Tries `paired-devices` first (only shows fully paired devices, faster),
    then falls back to `devices` which also lists devices seen in recent
    scans.  The name match is case-insensitive.

    Each bluetoothctl output line looks like:
        Device 2C:F2:DF:45:EC:28 TNG COMBADGE
    We take parts[1] (the MAC) when "TNG COMBADGE" appears anywhere in the line.
    """
    for sub in (["paired-devices"], ["devices"]):
        r = run([BTCTL] + sub)
        if not r:
            continue
        for line in r.stdout.splitlines():
            if "TNG COMBADGE" in line.upper():
                parts = line.split()
                if len(parts) >= 2:
                    return parts[1]  # the MAC address field
    return None


def is_connected(mac):
    """Return True if bluetoothctl reports the device as currently connected."""
    r = run([BTCTL, "info", mac])
    return bool(r and "Connected: yes" in r.stdout)


def connect_badge(mac, username):
    """Connect to the badge and bring up the HFP audio profile.

    Returns True if the audio sink is ready, False if something timed out.

    Steps:
      1. `bluetoothctl connect` — establishes the Bluetooth ACL (data) link.
      2. Wait for PipeWire to register the card as bluez_card.<MAC_with_underscores>.
         BlueZ notifies PipeWire/WirePlumber via D-Bus; this registration is
         asynchronous and typically takes 1–3 s after connect.
      3. `pactl set-card-profile ... headset-head-unit` — switches the card
         from A2DP (stereo music) to HFP (hands-free phone), which opens the
         bidirectional 16 kHz SCO audio channel used for voice capture and
         badge speaker playback.
      4. Wait for the HFP audio sink (bluez_output.<MAC>.1) to appear in
         PipeWire.  Audio played before this point falls back to the default
         output (laptop speakers) rather than the badge.

    Why run pactl as the user?  PipeWire is a per-user service.  The root
    process can't reach the user's PipeWire session directly — it must use
    `runuser` to execute pactl inside the user's D-Bus/XDG environment.
    """
    if not is_connected(mac):
        print(f"[transceiver] connecting {mac}...")
        run([BTCTL, "connect", mac])

    # PipeWire names the Bluetooth card with underscores replacing colons in the MAC.
    # Example: MAC 2C:F2:DF:45:EC:28 → bluez_card.2C_F2_DF_45_EC_28
    card = f"bluez_card.{mac.replace(':', '_')}"

    # Poll until PipeWire registers the card (up to ~15 s, 1 s intervals).
    for _ in range(15):
        r = run([RUNUSER, "-u", username, "--", PACTL, "list", "cards", "short"])
        if r and card in r.stdout:
            break
        time.sleep(1)
    else:
        print(f"[transceiver] timed out waiting for {card}")
        return False

    print(f"[transceiver] setting {card} to headset-head-unit")
    r = run([RUNUSER, "-u", username, "--",
             PACTL, "set-card-profile", card, "headset-head-unit"])
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
        r = run([RUNUSER, "-u", username, "--", PACTL, "list", "sinks", "short"])
        if r and sink in r.stdout:
            print(f"[transceiver] HFP sink ready.")
            return True
        time.sleep(1)
    print(f"[transceiver] timed out waiting for HFP sink {sink}")
    return False


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

    listener_proc = None
    current_mac   = None

    try:
        while True:
            mac = find_paired_badge()
            if not mac:
                print("[transceiver] no paired TNG COMBADGE — pair one with bluetoothctl.")
                time.sleep(5)
                continue

            if not is_connected(mac):
                # Badge disconnected — stop listener.py before attempting reconnect.
                if listener_proc and listener_proc.poll() is None:
                    print(f"[transceiver] {current_mac} disconnected, stopping listener.py")
                    listener_proc.terminate()
                    listener_proc.wait(timeout=5)
                    listener_proc = None
                    current_mac   = None
                if not connect_badge(mac, username):
                    time.sleep(5)
                    continue

            # New badge (or first run after startup) — (re)launch listener.py.
            if mac != current_mac:
                if listener_proc and listener_proc.poll() is None:
                    listener_proc.terminate()
                    listener_proc.wait(timeout=5)
                env           = build_session_env(mac, username)
                listener_proc = launch_listener(mac, username, listener_path, env)
                current_mac   = mac

            # listener.py crashed or exited cleanly — restart it.
            if listener_proc.poll() is not None:
                print(f"[transceiver] listener.py exited ({listener_proc.returncode}); restarting")
                env           = build_session_env(mac, username)
                listener_proc = launch_listener(mac, username, listener_path, env)

            time.sleep(CHECK_INTERVAL)

    except KeyboardInterrupt:
        print("\n[transceiver] shutting down")
        if listener_proc and listener_proc.poll() is None:
            listener_proc.terminate()


if __name__ == "__main__":
    main()
