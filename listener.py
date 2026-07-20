#!/usr/bin/env python3
"""
Minimal Combadge Listener / Tap and Audio Handler (TOS SDK).

Launched by transceiver.py as the logged-in user (in the `input` group) with
a populated session environment.  This script owns the full tap-to-response
cycle:

  1. Play startup sounds through the badge (confirms audio routing works).
  2. Open a persistent "downlink" connection to computer.py (b'h' + MAC) and
     hold it open for the life of the session.  The server keepalives it
     (b'k' every ~5 s) and can push unsolicited voice audio (b'v' frames —
     hail delivery) down it at any time.  If it drops (server restart), the
     relay reconnects every 5 s and replays maincomputeronline.wav on
     success, so a server restart is audible on the badge.
  3. Watch the badge's HID input device (/dev/input/eventX) for a single tap.
  4. On tap: establish the SCO audio link, play a listening chirp, stream mic
     audio over TCP to computer.py, and play back the response through the badge.
  5. Tear down the SCO link cleanly, then go back to waiting for the next tap.

Background — the HFP/SCO audio link:
  The badge sends and receives voice audio over Bluetooth HFP (Hands-Free
  Profile) using a synchronous SCO (Synchronous Connection Oriented) channel.
  This is a separate, dedicated audio path at 16 kHz mono — distinct from the
  HID tap channel and from A2DP stereo streaming.

  The SCO link must be explicitly opened (by starting capture from the badge
  microphone) and torn down (by setting the card profile to "off").  When it
  isn't active, pw-play targeting the badge sink falls back to default output
  (usually laptop speakers), which is the most common source of confusion.

Required environment (set by transceiver.py — do not run this script directly):
    BADGE_MAC                — e.g. 2C:F2:DF:45:EC:28
    SDK_SERVER_HOST          — hostname/IP of the machine running computer.py
    SDK_SERVER_PORT          — TCP port for computer.py (default 1701)
    XDG_RUNTIME_DIR, DBUS_SESSION_BUS_ADDRESS, HOME, USER  — for pw-play/pactl

Stripped down from relay/relay.py: no double-tap, no btmon, no per-scenario
priming, no captain's log.  Single-tap loop plus the persistent downlink
(sdk/INTERCOM.md Phase 2).
"""
import os
import pty
import re
import select
import socket
import subprocess
import sys
import tempfile
import threading
import time
import wave

import evdev   # pip install evdev  (Linux only — reads badge HID key events)


# ---------------------------------------------------------------------------
# Configuration — read from environment set by transceiver.py
# ---------------------------------------------------------------------------

BADGE_MAC = os.environ.get("BADGE_MAC")
if not BADGE_MAC:
    sys.exit("BADGE_MAC env var not set.  Launch via transceiver.py.")
if len(BADGE_MAC) != 17:
    # The protocol handshake sends the MAC as exactly 17 ASCII bytes
    # (AA:BB:CC:DD:EE:FF).  A malformed MAC would corrupt the audio stream
    # framing on the server side, so refuse to start.
    sys.exit(f"BADGE_MAC {BADGE_MAC!r} is not a 17-char colon-separated MAC.")

SERVER_HOST = os.environ.get("SDK_SERVER_HOST", "127.0.0.1")
SERVER_PORT = int(os.environ.get("SDK_SERVER_PORT", "1701"))


# ---------------------------------------------------------------------------
# PipeWire/BlueZ audio device names — derived from the badge MAC address.
#
# IMPORTANT ASYMMETRY: PipeWire (inheriting BlueZ naming) uses colons in the
# MAC for audio sources (microphone) but underscores for sinks (speaker) and
# cards.  This is not a typo and trips up almost everyone the first time.
#
#   SOURCE  bluez_input.AA:BB:CC:DD:EE:FF      ← colons, no suffix
#   SINK    bluez_output.AA_BB_CC_DD_EE_FF.1   ← underscores, ".1" suffix
#   CARD    bluez_card.AA_BB_CC_DD_EE_FF        ← underscores, no suffix
#
# The CARD name is used with `pactl set-card-profile` to switch the HFP link
# on (headset-head-unit) and off.
# The SOURCE is passed to ffmpeg to capture microphone audio.
# The SINK is passed to pw-play to route audio to the badge speaker.
# ---------------------------------------------------------------------------

SOURCE = f"bluez_input.{BADGE_MAC}"
SINK   = f"bluez_output.{BADGE_MAC.replace(':', '_')}.1"
CARD   = f"bluez_card.{BADGE_MAC.replace(':', '_')}"


# ---------------------------------------------------------------------------
# Asset file paths — WAV/MP3 files played through the badge speaker.
# Copy these from the TOS audio/ directory or substitute your own files.
# ---------------------------------------------------------------------------

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ASSET_DIR  = os.path.join(SCRIPT_DIR, "assets")

LISTENING_WAV           = os.path.join(ASSET_DIR, "listening.wav")
ACK_WAV                 = os.path.join(ASSET_DIR, "commandexecuted.wav")
NACK_WAV                = os.path.join(ASSET_DIR, "commandfailure.wav")
BADGE_ONLINE_WAV        = os.path.join(ASSET_DIR, "badge-to-comms-relay-online.wav")
MAINCOMPUTER_ONLINE_WAV = os.path.join(ASSET_DIR, "maincomputeronline.wav")


# ---------------------------------------------------------------------------
# Logging — every line timestamped, MAC-tagged, and teed to a file
# ---------------------------------------------------------------------------
# The module-level `print` is deliberately shadowed: every existing log call
# gains a timestamp + badge MAC and is appended (line-flushed) to
# sdk/log/listener_<MAC>.log alongside the console.  On PAN the sdk/ tree is
# the shared sshfs mount, so the file is live-readable from CUBE for remote
# diagnosis; on BOX (severed) it stays local.

LOG_DIR  = os.path.join(SCRIPT_DIR, "log")
LOG_FILE = os.path.join(LOG_DIR, f"listener_{BADGE_MAC.replace(':', '_')}.log")
try:
    os.makedirs(LOG_DIR, exist_ok=True)
except OSError:
    pass

_print    = print
_log_lock = threading.Lock()


def print(*args, **kwargs):   # noqa: A001 — deliberate shadow, see above
    line    = " ".join(str(a) for a in args)
    stamped = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [{BADGE_MAC}] {line}"
    _print(stamped, **kwargs)
    with _log_lock:
        try:
            with open(LOG_FILE, "a") as f:
                f.write(stamped + "\n")
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Timing constants
# ---------------------------------------------------------------------------

# Ignore taps that arrive within this many seconds of the previous tap.
# The badge can emit spurious key events when the SCO link tears down; 2 s
# covers that window without imposing a noticeable delay between real taps.
TAP_DEBOUNCE = 2.0  # seconds

# Milliseconds of silence played before ack/nack chirps (SCO link already warm).
# 200 ms prevents PipeWire from briefly releasing the audio path between two
# successive sounds, which would cause the next sound's first ~200 ms to be cut.
PRIME_MS = 200

# Milliseconds of silence played before the listening chirp.
# Set to 0 because ensure_hfp_profile() already confirmed the HFP sink is live
# before the chirp plays.  If you hear the chirp routing to laptop speakers,
# increase this to 200–500 ms to give the output-side SCO more time to settle.
PRIME_MS_LISTENING = 0

# Milliseconds of silence played after SCO is confirmed live on a cold link
# (startup sounds only).  Cold links need more priming (~1000 ms) because the
# output-side SCO negotiation lags behind the input-side confirmation.
PRIME_MS_COLD = 1000

# Maximum seconds to stream microphone audio per tap before giving up.
# computer.py enforces its own timeout too; this is the relay-side backstop.
# Kept ABOVE the server's 10 s so the server's verdict always lands: its
# clock starts after tap->chirp->connect (~1-2 s later than ours), so an
# equal value made the relay give up just before b'f' arrived — logged as
# the confusing "no/unknown signal byte: None" with no failure chirp.
RECORD_MAX_S = 13

# Downlink (persistent server connection) tuning.
DOWNLINK_RETRY_S = 5    # seconds between reconnect attempts while down
DOWNLINK_TIMEOUT = 15   # recv timeout: 3 missed 5 s keepalives = server presumed dead
PREWARM_MAX_S    = 25   # max seconds to hold a prewarmed SCO awaiting the b'v'
                        # (covers the server's 20 s hail-capture hard cap)

# Playback volume for PUSHED voice (hails).  Chirps and tap-cycle TTS keep
# the original 0.5 clipping guard, but the live channel plays at 1.0
# (paplay, no volume flag) — pushed hails must match the channel's level or
# the opening hail sounds half as loud as the conversation that follows.
PUSH_VOLUME = float(os.environ.get("SDK_PUSH_VOLUME", "1.0"))


# ---------------------------------------------------------------------------
# Persistent downlink — server-initiated audio (hail delivery)
# ---------------------------------------------------------------------------
#
# The tap cycle is relay-initiated: nothing reaches the badge unless its
# user taps first.  Badge-to-badge hails invert that — the server must be
# able to deliver audio to a badge whose user did nothing.  The downlink
# is that path: a TCP connection opened at startup (b'h' + MAC handshake)
# and held open forever.  Outbound-only by design — the relay never
# listens on a port (works behind NAT; matches the future mobile relay's
# constraints).
#
# audio_lock serializes ALL badge audio between the tap cycle and pushed
# playback: a hail arriving mid-command waits for the command to finish
# rather than playing over the top of it (and vice versa).

audio_lock  = threading.Lock()    # whoever holds this owns the badge's audio
downlink_up = threading.Event()   # set while the downlink is established


# ---------------------------------------------------------------------------
# Audio playback helpers
# ---------------------------------------------------------------------------

def play_silence(ms):
    """Play `ms` milliseconds of 16 kHz mono silence through the badge speaker.

    Why this matters:
      When the SCO link is already active and we play two sounds back-to-back
      as separate pw-play calls, PipeWire may briefly release the audio path
      in the gap between them.  Re-acquiring it eats the first ~200 ms of the
      second sound.  Playing silence first (and keeping the link "warm") before
      the real audio prevents this gap from forming.

    Implementation:
      The silence WAV is constructed in memory using Python's wave module and
      written to a temp file because pw-play requires a file path, not stdin.
      The temp file is deleted immediately after playback.
    """
    if ms <= 0:
        return
    samples = int(16000 * ms / 1000)
    fd, path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    try:
        with wave.open(path, "w") as wf:
            wf.setnchannels(1)       # mono  (HFP SCO is always mono)
            wf.setsampwidth(2)       # 16-bit signed PCM samples
            wf.setframerate(16000)   # 16 kHz sample rate (HFP standard)
            wf.writeframes(b"\x00\x00" * samples)
        subprocess.run(["pw-play", "--target", SINK,
                        "--media-role=communication", path],
                       check=False, capture_output=True, timeout=5)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def play_wav(path, prime=True, volume=0.5):
    """Play a WAV or MP3 file through the badge speaker via pw-play.

    `prime=True`  — play PRIME_MS of silence first to keep PipeWire from
                    releasing the audio path between successive sounds.
                    Use this when playing ack/nack chirps after a response.

    `prime=False` — skip the silence prime.  Use this when the SCO link is
                    already actively in use (ffmpeg is still streaming) so
                    there is no gap and no release-and-reacquire problem.

    --media-role=communication hints to PipeWire to route audio to the HFP
    sink rather than mixing with music/media streams.
    --volume 0.5 avoids clipping on the badge's small speaker.
    """
    if not os.path.isfile(path):
        print(f"[listener] missing wav: {path}", file=sys.stderr)
        return
    if prime:
        play_silence(PRIME_MS)
    subprocess.run(["pw-play", "--target", SINK, "--media-role=communication",
                    "--volume", str(volume), path],
                   check=False, capture_output=True, timeout=15)


def play_wav_cold(path, volume=0.5):
    """Play a WAV file through the badge on a cold (not yet active) SCO link.

    WHY this function exists:
      On a cold HFP link, calling pw-play targeting the badge sink often
      fails silently — PipeWire falls back to the default output device
      (laptop speakers) because the SCO channel hasn't finished negotiating.

      HFP SCO has two halves negotiated independently:
        INPUT side  (microphone):  triggered by opening bluez_input.<MAC>
        OUTPUT side (speaker):     triggered by audio arriving at the sink

      Opening the input side via ffmpeg is the more reliable trigger.  Once
      the input side is up, the SCO channel is active and the output side
      follows.  We exploit this by opening ffmpeg against the microphone
      source before attempting to play anything.

    HOW it works:
      1. Open ffmpeg against the badge microphone source (SOURCE).
      2. ffmpeg writes a standard 44-byte WAV header the moment it
         successfully opens the source — this is our "SCO link is live"
         signal.  We wait for those 44 bytes with a 10 s timeout.
      3. Play PRIME_MS_COLD ms of silence to warm the output side.
      4. Play the actual audio file.
      5. Terminate ffmpeg — we only needed it to establish the link.

    Note: this function is only used for the two startup sounds.  During
    normal tap cycles the link is managed by stream_and_handle_response()
    which keeps ffmpeg running for the full recording session.
    """
    if not os.path.isfile(path):
        print(f"[listener] missing asset: {os.path.basename(path)}", file=sys.stderr)
        return

    ffmpeg = start_sco_capture()
    if not ffmpeg:
        print(f"[listener] SCO link failed — skipping {os.path.basename(path)}",
              file=sys.stderr)
        return
    try:
        # SCO confirmed live — prime the output side, then play.
        play_silence(PRIME_MS_COLD)
        play_wav(path, prime=False, volume=volume)
    finally:
        # We only needed ffmpeg to trigger and hold the SCO link.
        terminate_ffmpeg(ffmpeg)


def start_sco_capture(timeout_s=10):
    """Open ffmpeg against the badge mic and wait for SCO to come up.

    Opening bluez_input triggers HFP SCO negotiation from the capture side —
    the only reliable trigger (see play_wav_cold's original rationale).
    ffmpeg emits its 44-byte WAV header the moment the source opens; that
    header's arrival IS the "SCO link live" confirmation.  Returns the
    running ffmpeg Popen (caller must terminate_ffmpeg() it) or None on
    timeout/failure.  Shared by play_wav_cold() and the downlink prewarm.
    """
    ffmpeg = subprocess.Popen(
        ["ffmpeg", "-f", "pulse", "-i", SOURCE,
         "-ar", "16000", "-ac", "1", "-f", "wav",
         "-loglevel", "quiet", "pipe:1"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    header = b""
    while len(header) < 44:
        r, _, _ = select.select([ffmpeg.stdout], [], [], timeout_s)
        if not r:
            terminate_ffmpeg(ffmpeg)
            return None
        chunk = ffmpeg.stdout.read(44 - len(header))
        if not chunk:
            terminate_ffmpeg(ffmpeg)
            return None
        header += chunk
    return ffmpeg


def terminate_ffmpeg(proc):
    """SIGTERM an ffmpeg, escalating to SIGKILL after 0.5 s."""
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


# ---------------------------------------------------------------------------
# HFP profile management (SCO link lifecycle)
# ---------------------------------------------------------------------------

def force_sco_teardown():
    """Collapse the SCO audio link by setting the card profile to "off".

    When to call this:
      Immediately after the last pw-play subprocess returns.
      subprocess.run(["pw-play", ...]) is synchronous — its return IS the
      "last audio frame left the pipeline" event.  Calling teardown right
      then is instantaneous and provably non-premature; no timer is needed.

    Why tear down at all?
      Left alone, the SCO link lingers for ~5–10 s after the last audio,
      holding the badge microphone open.  On the next tap, ensure_hfp_profile()
      re-establishes it cleanly.  This explicit teardown + re-establishment
      cycle gives a predictable, consistent starting state for every tap.
    """
    subprocess.run(["pactl", "set-card-profile", CARD, "off"],
                   capture_output=True, timeout=5)


def ensure_hfp_profile():
    """Switch the card to headset-head-unit and wait for the audio sink.

    Called at the start of every tap cycle.

    First tap: transceiver.py already set the profile, so the sink is
    likely already present.  The pactl command is a fast no-op and the
    polling loop exits on the first check.

    Subsequent taps: force_sco_teardown() set the profile to "off" after
    the previous tap.  This function restores it and polls until the HFP
    sink reappears in PipeWire (typically 0.5–1 s).

    Why poll instead of sleep a fixed time?
      The profile switch is asynchronous — BlueZ notifies PipeWire/WirePlumber
      via D-Bus and the sink appears shortly after.  Polling for the sink
      every 0.5 s returns as soon as it's ready rather than wasting time on
      a fixed sleep that may be too short or unnecessarily long.
    """
    subprocess.run(["pactl", "set-card-profile", CARD, "headset-head-unit"],
                   capture_output=True, timeout=5)
    for _ in range(15):   # up to 7.5 s total wait
        r = subprocess.run(["pactl", "list", "sinks", "short"],
                           capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and SINK in r.stdout:
            return True
        time.sleep(0.5)
    print("[listener] WARNING: HFP sink did not reappear after profile switch",
          file=sys.stderr)
    return False


# ---------------------------------------------------------------------------
# Badge HID input device discovery
# ---------------------------------------------------------------------------

def find_badge_input():
    """Scan /dev/input/eventX devices and return the path for the badge, or None.

    The combadge registers as a Bluetooth HID device.  We identify it by:
      1. Device name containing "TNG COMBADGE" (matched case-insensitively).
      2. Fallback: the device reports KEY_PAUSECD (keycode 201) or keycode 200
         in its EV_KEY capability set — the badge single-tap fires one of these
         depending on firmware version.

    The /dev/input/eventX path can change across Bluetooth reconnections, so
    this is called in a loop rather than cached at startup.

    Note: this function requires the process to be in the `input` group
    (arranged by transceiver.py via `sg input`) to open HID devices.
    """
    for path in evdev.list_devices():
        try:
            dev = evdev.InputDevice(path)
        except OSError:
            continue   # device vanished or we lack permission; skip it
        if "TNG COMBADGE" in dev.name.upper():
            return path
        # Fallback: identify by the key codes the badge emits on single-tap.
        caps = dev.capabilities(verbose=False)
        keys = caps.get(evdev.ecodes.EV_KEY, [])
        if evdev.ecodes.KEY_PAUSECD in keys or 200 in keys:
            return path
    return None


# ---------------------------------------------------------------------------
# Voice response reception
# ---------------------------------------------------------------------------

def read_voice_payload(sock):
    """Read one framed voice payload from a socket into a temp WAV file.

    Wire format (follows a b'v' signal byte the caller already consumed):
        4 bytes  — WAV payload size, big-endian unsigned int
        N bytes  — the complete WAV file (exactly `size` bytes)

    Returns the temp file path (caller is responsible for deleting it), or
    None if the connection closed before the size header completed.  A
    payload cut short by a premature close still returns the partial file —
    play whatever arrived.  Shared by the tap-cycle voice response and the
    downlink's pushed-audio path, which play through different sequences.
    """
    size_hdr = b""
    while len(size_hdr) < 4:
        chunk = sock.recv(4 - len(size_hdr))
        if not chunk:
            return None   # connection closed before we got the full header
        size_hdr += chunk
    remaining = int.from_bytes(size_hdr, "big")

    # Buffer the WAV payload to a temp file (pw-play requires a file path).
    fd, path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    with open(path, "wb") as f:
        while remaining > 0:
            chunk = sock.recv(min(4096, remaining))
            if not chunk:
                break   # premature server close; keep whatever arrived
            f.write(chunk)
            remaining -= len(chunk)
    return path


def receive_voice_response(sock):
    """Read a framed voice response WAV from the server and play it.

    Called after the leading b'v' signal byte has already been consumed
    by the caller (stream_and_handle_response).

    Why prime=False:
      ffmpeg is still running at this point (it is terminated in the
      caller's finally block).  The SCO link is actively in use, so there
      is no audio-path release gap to bridge.  Inserting a silence prime
      here would CREATE a gap between two pw-play calls, during which
      PipeWire briefly releases the path — and re-acquiring it eats the
      first ~200 ms of the voice response.  A single pw-play with no prime
      eliminates that gap entirely.
    """
    path = read_voice_payload(sock)
    if not path:
        return
    try:
        play_wav(path, prime=False)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def play_pushed_voice(path, volume=None):
    """Play a server-pushed voice WAV through the badge from the idle state.

    `volume` defaults to PUSH_VOLUME (1.0 — hails must match channel
    loudness); pass an explicit value for non-hail audio on this path
    (e.g. the downlink's "main computer online" announce at the standard
    0.5 chirp level).

    Unlike a tap-cycle voice response (SCO already hot, ffmpeg running), a
    pushed frame usually arrives while the badge is idle: the previous
    cycle's force_sco_teardown() left the card profile OFF, so there is no
    HFP sink or source at all.  Full cold sequence: restore the profile,
    cold-start SCO from the capture side (play_wav_cold), then tear down
    again so the idle state is exactly as we found it.

    audio_lock serializes this against an active tap cycle — a hail
    arriving mid-command waits for the command to finish rather than
    playing over it.
    """
    if volume is None:
        volume = PUSH_VOLUME
    with audio_lock:
        ensure_hfp_profile()
        play_wav_cold(path, volume=volume)
        force_sco_teardown()


def downlink_loop():
    """Maintain the persistent downlink to the server forever (thread body).

    Cycle: connect → handshake b'h'+MAC → announce (maincomputeronline.wav)
    → service loop → on any failure, mark the downlink down and retry every
    DOWNLINK_RETRY_S seconds.

    Service loop handles:
      b'k'  — keepalive.  Receiving any byte resets the recv timeout;
              DOWNLINK_TIMEOUT (15 s) of total silence means the server is
              gone even if the TCP stack hasn't noticed — reconnect.
      b'v'  — pushed voice (hail delivery).  The whole frame is read off
              the socket FIRST (so waiting for the audio lock can't stall
              the socket), then played via the cold-SCO sequence.

    The reconnect announcement doubles as restart detection: a server
    restart is audible on the badge with no tap — the same behavior the
    mobile relay gets from its liveness poll.
    """
    while True:
        # --- connect + handshake ---
        sock = None
        try:
            sock = socket.create_connection((SERVER_HOST, SERVER_PORT), timeout=5)
            sock.sendall(b"h" + BADGE_MAC.encode("ascii"))
            sock.settimeout(DOWNLINK_TIMEOUT)
        except OSError as e:
            if sock:
                sock.close()
            print(f"[listener] downlink unavailable ({e}); retry in {DOWNLINK_RETRY_S} s")
            time.sleep(DOWNLINK_RETRY_S)
            continue

        print(f"[listener] downlink established to {SERVER_HOST}:{SERVER_PORT}")
        downlink_up.set()
        # Announce on every (re)connect — audible "the server is (back) up."
        # Standard chirp level, NOT hail level — matches the badge-online
        # announce that precedes it.
        play_pushed_voice(MAINCOMPUTER_ONLINE_WAV, volume=0.5)

        # Prewarm state: on b'W' the server says "audio is coming for this
        # badge in a few seconds" (a hail is being captured).  We bring SCO
        # up NOW — profile + ffmpeg capture-side trigger — and hold
        # audio_lock so nothing tears the link down before the b'v'
        # arrives.  When it does, playback starts near-instantly (hot path,
        # no cold start, no 1 s prime).  If no b'v' arrives in
        # PREWARM_MAX_S the warm state is released (badge freed, lock
        # dropped) and a later b'v' just takes the normal cold path —
        # prewarm is a soft optimization, never a correctness dependency.
        warm = None   # running ffmpeg while prewarmed (audio_lock held)
        warm_expires = 0.0

        def release_warm():
            nonlocal warm
            if warm:
                terminate_ffmpeg(warm)
                force_sco_teardown()
                audio_lock.release()
                warm = None

        # --- service loop ---
        try:
            while True:
                if warm and time.time() > warm_expires:
                    print("[listener] prewarm expired — releasing badge")
                    release_warm()
                sig = sock.recv(1)
                if not sig:
                    print("[listener] downlink closed by server — reconnecting")
                    break
                if sig == b"k":
                    continue          # keepalive — the recv above reset the timeout
                if sig == b"W":
                    if warm:
                        warm_expires = time.time() + PREWARM_MAX_S
                        continue      # already warm — just extend the window
                    print("[listener] prewarm — bringing SCO up")
                    audio_lock.acquire()
                    ensure_hfp_profile()
                    warm = start_sco_capture()
                    if warm:
                        warm_expires = time.time() + PREWARM_MAX_S
                    else:
                        print("[listener] prewarm failed — will fall back to cold path")
                        force_sco_teardown()
                        audio_lock.release()
                    continue
                if sig == b"v":
                    path = read_voice_payload(sock)
                    if path:
                        try:
                            if warm:
                                # Hot path: SCO already live from prewarm.
                                print("[listener] pushed voice received — playing (prewarmed)")
                                play_wav(path, prime=False, volume=PUSH_VOLUME)
                                release_warm()
                            else:
                                print("[listener] pushed voice received — playing")
                                play_pushed_voice(path)
                        finally:
                            try:
                                os.unlink(path)
                            except OSError:
                                pass
                    continue
                print(f"[listener] unknown downlink byte: {sig!r}")
        except socket.timeout:
            print("[listener] downlink keepalive timeout — reconnecting")
        except OSError as e:
            print(f"[listener] downlink error: {e} — reconnecting")
        finally:
            release_warm()
            downlink_up.clear()
            try:
                sock.close()
            except OSError:
                pass
        time.sleep(1)   # brief pause, then the reconnect loop takes over


# ---------------------------------------------------------------------------
# Intercom channel (sdk/INTERCOM.md Phase 4)
# ---------------------------------------------------------------------------

CHANNEL_TAP_GRACE_S = 1.0   # ignore badge taps in the first second of a channel

# Double-tap detection: the badge emits AT+BVRA=1 on the HFP control
# channel (never an evdev event).  Same btmon-under-pty pattern the full
# relay uses in production (relay/relay.py monitor_bluetooth_logs).
BTMON_TRIGGER = "AT+BVRA=1"
_ANSI_RE      = re.compile(r"\x1b\[[0-9;]*m")


def _start_btmon():
    """Spawn btmon under a pty (line-buffered via stdbuf).  Returns
    (pid, master_fd) or (None, None) if unavailable.  The master fd is
    select()able, so run_channel folds it into its main loop."""
    try:
        pid, fd = pty.fork()
    except OSError:
        return None, None
    if pid == 0:   # child
        try:
            os.execvp("stdbuf", ["stdbuf", "-oL", "btmon"])
        except Exception:
            os._exit(127)
    return pid, fd


def _stop_btmon(pid, fd):
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass
    if pid:
        try:
            os.kill(pid, 15)
            os.waitpid(pid, os.WNOHANG)
        except (OSError, ChildProcessError):
            pass


def run_channel(sock, ffmpeg):
    """Live intercom: this tap socket is now a full-duplex audio channel.

    Entered when the server sends b'O' (this badge is one end of an
    answered hail).  The SCO link and mic capture (ffmpeg) from the tap
    cycle are still live and simply keep going:

      uplink    ffmpeg mic PCM -> sock, raw (unchanged from command mode)
      downlink  framed by the server: b'A' + 2-byte BE len + PCM (peer
                audio, piped into a long-running pw-cat playing from
                stdin); b'X' = channel closed by the other side.  Framing
                exists so control bytes stay distinguishable inside a raw
                audio stream.
      close     single badge tap -> half-close the uplink (SHUT_WR); the
                server tears the bridge down and b'X's both sides.
                (Double-tap emits AT+BVRA on the HFP control channel,
                visible only via btmon/root — out of SDK scope.  Single
                tap also matches the future mobile relay, where any tap
                during SCO is a system hang-up.)

    Returns when the channel ends; the caller's normal teardown runs.
    """
    # Player stderr goes to a log file (not DEVNULL) and its stdin pipe is
    # switched to NON-BLOCKING: a blocking write into a full pipe (pw-cat
    # dead or stalled) would freeze this whole loop — no uplink, no tap
    # detection, no close.  Under backpressure we DROP frames instead;
    # dropping is the correct policy for realtime audio.
    # Player candidates, tried in order when one dies.  BOTH output streams
    # go to log/pwcat.log — some tools print usage errors to stdout (which
    # is exactly how the first on-badge failure stayed invisible).
    #   paplay  — PRIMARY: pulse layer (pipewire-pulse exposes the same
    #             sink name).  On-badge validated 2026-07-05.
    #   pw-cat  — fallback only: the Debian pipewire build's pw-cat does
    #             NOT accept raw stdin ('-') — it hands it to sndfile,
    #             which fails with "Format not recognised" (rc=1, seen in
    #             pwcat.log).  Kept in case a future pipewire fixes that.
    player_cmds = [
        ["paplay", "--raw", "--rate=16000", "--channels=1",
         "--format=s16le", f"--device={SINK}"],
        ["pw-cat", "-p", "--target", SINK, "--media-role=communication",
         "--rate", "16000", "--channels", "1", "--format", "s16", "-"],
    ]
    pwcat_log_path = os.path.join(LOG_DIR, "pwcat.log")

    def _pump_player_output(pipe):
        # Drain player output to pwcat.log via transient opens — handing the
        # child a file handle would keep the log locked (unreadable to other
        # processes when the dir is served over sshfs) for the child's whole
        # lifetime. Thread exits on player EOF.
        try:
            for line in iter(pipe.readline, b''):
                try:
                    with open(pwcat_log_path, "ab") as f:
                        f.write(line)
                except OSError:
                    pass
        finally:
            try:
                pipe.close()
            except OSError:
                pass

    def _spawn_player(cmd):
        print(f"[listener] channel player: {cmd[0]}")
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        os.set_blocking(p.stdin.fileno(), False)
        threading.Thread(target=_pump_player_output, args=(p.stdout,),
                         daemon=True).start()
        return p

    player   = _spawn_player(player_cmds[0])
    next_cmd = 1

    # Double-tap monitor — the PRIMARY close gesture (Captain-approved:
    # single taps during SCO emit nothing at all on Linux, validated
    # on-badge 2026-07-05).
    btmon_pid, btmon_fd = _start_btmon()
    btmon_buf = ""
    if btmon_fd is None:
        print("[listener] channel: btmon unavailable — double-tap close disabled")

    dev = None
    try:
        path = find_badge_input()
        if path:
            dev = evdev.InputDevice(path)   # fresh fd — no stale queued taps
    except OSError:
        dev = None
    if not dev:
        print("[listener] channel: no badge input — tap-to-close unavailable")

    print("[listener] CHANNEL OPEN — single tap to close")
    opened  = time.time()
    closing = False
    buf     = b""
    up_b = down_b = dropped_b = 0
    player_dead = False
    last_beat   = time.time()
    try:
        while True:
            # Heartbeat: proves the loop is alive and shows audio movement.
            if time.time() - last_beat >= 5:
                print(f"[listener] channel: up {up_b//1024}KB "
                      f"down {down_b//1024}KB dropped {dropped_b//1024}KB")
                last_beat = time.time()
            if not player_dead and player.poll() is not None:
                print(f"[listener] channel: player exited rc={player.returncode} "
                      f"(see log/pwcat.log)")
                if next_cmd < len(player_cmds):
                    player   = _spawn_player(player_cmds[next_cmd])
                    next_cmd += 1
                else:
                    player_dead = True
                    print("[listener] channel: all players failed — peer audio muted")

            fds = [sock]
            if not closing:
                fds.append(ffmpeg.stdout)
            if dev:
                fds.append(dev.fd)
            if btmon_fd is not None:
                fds.append(btmon_fd)
            r, _, _ = select.select(fds, [], [], 0.5)

            # --- close gesture: double tap (btmon AT+BVRA) ---
            if btmon_fd is not None and btmon_fd in r:
                # DRAIN the pty, not just one read: during an active SCO
                # link btmon emits a torrent of HCI traffic, and a single
                # 1 KB read per loop pass falls behind — the BVRA line
                # drowns in pty backlog (observed: PAN double-tap "did
                # nothing").  Read until would-block, capped per pass.
                chunk = ""
                try:
                    for _ in range(64):                    # ≤64 KB per pass
                        chunk += os.read(btmon_fd, 1024).decode(
                            "utf-8", errors="ignore")
                        r2, _, _ = select.select([btmon_fd], [], [], 0)
                        if not r2:
                            break
                except OSError:
                    if not chunk:
                        chunk = ""
                if not chunk:
                    _stop_btmon(btmon_pid, btmon_fd)
                    btmon_pid = btmon_fd = None
                    print("[listener] channel: btmon ended — double-tap close disabled")
                else:
                    btmon_buf += chunk
                    lines = btmon_buf.split("\n")
                    btmon_buf = lines.pop()
                    for ln in lines:
                        if (BTMON_TRIGGER in _ANSI_RE.sub("", ln)
                                and not closing
                                and time.time() - opened > CHANNEL_TAP_GRACE_S):
                            print("[listener] channel close requested (double-tap)")
                            closing = True
                            try:
                                sock.shutdown(socket.SHUT_WR)
                            except OSError:
                                pass

            # --- close gesture: single tap (kept as secondary; usually
            #     silent during SCO on Linux badges) ---
            if dev and dev.fd in r:
                try:
                    events = list(dev.read())
                except (BlockingIOError, OSError):
                    events = []
                for ev in events:
                    if (ev.type == evdev.ecodes.EV_KEY
                            and ev.code in (200, 201) and ev.value == 1
                            and time.time() - opened > CHANNEL_TAP_GRACE_S
                            and not closing):
                        print("[listener] channel close requested (tap)")
                        closing = True
                        try:
                            sock.shutdown(socket.SHUT_WR)
                        except OSError:
                            pass

            # --- uplink: badge mic -> server ---
            if not closing and ffmpeg.stdout in r:
                data = ffmpeg.stdout.read(4096)
                if data:
                    up_b += len(data)
                    try:
                        sock.sendall(data)
                    except OSError:
                        break
                else:
                    # ffmpeg EOF = the SCO capture ended under us.  During
                    # an active SCO link the badge button belongs to HFP
                    # call control (see the mobile Phase 3 finding), so a
                    # tap may never surface as an evdev event — instead the
                    # badge hangs the SCO link up.  Treat that as the close
                    # gesture, mobile-style.
                    print("[listener] channel: uplink ended (badge dropped "
                          "SCO — tap hang-up?) — closing channel")
                    closing = True
                    try:
                        sock.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass

            # --- downlink: framed peer audio / close signal ---
            if sock in r:
                data = sock.recv(4096)
                if not data:
                    break                    # server closed — channel over
                buf += data
                done = False
                while buf:
                    kind = buf[:1]
                    if kind == b"X":
                        done = True          # closed by the other side
                        break
                    if kind == b"k":
                        buf = buf[1:]
                        continue
                    if kind == b"A":
                        if len(buf) < 3:
                            break            # need the length header
                        size = int.from_bytes(buf[1:3], "big")
                        if len(buf) < 3 + size:
                            break            # incomplete frame
                        payload, buf = buf[3:3 + size], buf[3 + size:]
                        down_b += len(payload)
                        if not player_dead:
                            # Non-blocking raw write; drop under backpressure
                            # (a full pipe must NEVER stall this loop).
                            try:
                                n = os.write(player.stdin.fileno(), payload)
                                if n < len(payload):
                                    dropped_b += len(payload) - n
                            except BlockingIOError:
                                dropped_b += len(payload)
                            except OSError:
                                pass         # player died; poll() logs it
                        continue
                    print(f"[listener] channel: unexpected byte {kind!r}")
                    done = True
                    break
                if done:
                    break
    finally:
        if dev:
            try:
                dev.close()
            except Exception:
                pass
        _stop_btmon(btmon_pid, btmon_fd)
        try:
            player.stdin.close()
        except OSError:
            pass
        terminate_ffmpeg(player)
    print(f"[listener] CHANNEL CLOSED (up {up_b//1024}KB down {down_b//1024}KB "
          f"dropped {dropped_b//1024}KB)")
    play_wav(ACK_WAV, prime=False)   # close confirmation; SCO still hot


# ---------------------------------------------------------------------------
# Core tap cycle: establish audio, stream to server, play response
# ---------------------------------------------------------------------------

def stream_and_handle_response():
    """Serialized entry point for the tap cycle.

    Holds audio_lock for the whole cycle so a pushed hail (downlink thread)
    can never play over the top of an active recording or response — it
    waits its turn, and vice versa.
    """
    with audio_lock:
        _stream_and_handle_response()


def _stream_and_handle_response():
    """Full single-tap cycle: SCO setup → chirp → stream → response → teardown.

    Ordered sequence:
      1. ensure_hfp_profile()       re-establish HFP after previous teardown
      2. Start ffmpeg capture        opening SOURCE triggers SCO negotiation
      3. Wait for 44-byte WAV header SCO link confirmed live
      4. Play chirp                  badge speaker signals "I'm listening"
      5. TCP connect to server       send tap byte + WAV header, then PCM
      6. select() loop               forward audio; watch for response signal
      7. Handle signal byte          play ack/nack/voice response
      8. force_sco_teardown()        clean up immediately after last audio
    """

    # --- Step 1: re-establish HFP profile ---
    # Fast no-op on first tap (profile already active from transceiver.py).
    # On subsequent taps, restores the profile that force_sco_teardown() disabled.
    ensure_hfp_profile()

    # --- Step 2: start ffmpeg BEFORE playing the chirp ---
    # Opening bluez_input (the badge microphone) triggers SCO negotiation from
    # the capture side, which is more reliable than the output-side path.
    # If we played the chirp first, it would likely route to laptop speakers
    # because the SCO output path hasn't finished negotiating yet.
    ffmpeg = subprocess.Popen(
        ["ffmpeg", "-f", "pulse", "-i", SOURCE,
         "-ar", "16000", "-ac", "1", "-f", "wav",
         "-loglevel", "quiet", "pipe:1"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )

    # --- Step 3: wait for the 44-byte WAV header ---
    # ffmpeg writes this header the moment it successfully opens bluez_input.
    # Its arrival means the SCO input path is live and pw-play will route to
    # the badge.  If ffmpeg exits early (badge disconnected), read() returns b""
    # and we fall through with an empty wav_header; the pipeline still runs
    # but the chirp may not route to the badge.
    wav_header = b""
    while len(wav_header) < 44:
        chunk = ffmpeg.stdout.read(44 - len(wav_header))
        if not chunk:
            break
        wav_header += chunk

    # --- Step 4: play the listening chirp ---
    # SCO is confirmed live.  play_silence(0) is a no-op so the chirp
    # plays immediately.  Increase PRIME_MS_LISTENING if it still clips to speakers.
    play_silence(PRIME_MS_LISTENING)
    play_wav(LISTENING_WAV, prime=False)

    # --- Step 5: connect to server ---
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(RECORD_MAX_S + 5)
    try:
        sock.connect((SERVER_HOST, SERVER_PORT))
        # 18-byte handshake: tap-type byte + our badge MAC (17 ASCII chars,
        # colon-separated).  The MAC keys the session on the server so
        # multiple badges on multiple transceivers can be told apart — same
        # dialect as the full maincomputer protocol.  See sdk/INTERCOM.md.
        sock.sendall(b"1" + BADGE_MAC.encode("ascii"))
        if wav_header:
            sock.sendall(wav_header)    # forward buffered WAV header (server discards it)
        print("[listener] transmitting — speak now")
    except OSError as e:
        print(f"[listener] cannot reach server {SERVER_HOST}:{SERVER_PORT} — "
              f"is computer.py running there? ({e})")
        sock.close()
        if ffmpeg.poll() is None:
            ffmpeg.terminate()
        return

    # --- Step 6: select() loop — stream audio and watch for server response ---
    #
    # select() lets us watch two file descriptors simultaneously without
    # blocking on either:
    #   ffmpeg.stdout — new PCM audio chunks to forward to the server
    #   sock          — the server's signal byte (arrives when it has a result)
    #
    # CRITICAL ORDER: always check `sock` before reading from `ffmpeg.stdout`.
    # The server closes the connection immediately after sending the signal byte.
    # If we try to send another audio chunk first, we get BrokenPipeError and
    # miss the signal.  select() tells us which fd is readable — check sock first.
    signal_byte = None
    deadline = time.time() + RECORD_MAX_S
    try:
        while time.time() < deadline:
            r, _, _ = select.select([ffmpeg.stdout, sock], [], [], 0.5)

            if sock in r:
                sig = sock.recv(1)
                if not sig:
                    break   # server closed without sending a signal byte
                if sig == b"k":
                    # Keepalive: the server is holding this session open
                    # past the normal window (hail capture, or the pending-
                    # answer hold while the hailed badge decides).  Slide
                    # the recording deadline and keep streaming — the next
                    # non-keepalive byte is still the terminal signal.
                    deadline = time.time() + RECORD_MAX_S
                    continue
                if sig == b"O":
                    # Answered hail — this socket becomes the live intercom.
                    # No SHUT_WR: the uplink keeps flowing inside the channel.
                    signal_byte = sig
                    run_channel(sock, ffmpeg)
                    break
                signal_byte = sig
                # Half-close our send side right away: the signal byte means
                # the server is done listening, and we will send no more
                # audio.  This delivers a FIN so the server's drain-before-
                # close loop (see computer.py drain_connection) hits EOF
                # immediately instead of waiting out a timeout.  Receiving
                # (the voice WAV below) still works on a half-closed socket.
                try:
                    sock.shutdown(socket.SHUT_WR)
                except OSError:
                    pass
                if sig == b"v":
                    receive_voice_response(sock)   # read and play the voice WAV
                break   # done regardless of signal type

            if ffmpeg.stdout in r:
                data = ffmpeg.stdout.read(4096)
                if not data:
                    break   # ffmpeg ended (badge disconnected or capture device lost)
                try:
                    sock.sendall(data)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    # Server may have sent the signal byte and closed just as we
                    # were sending another chunk.  Make one final recv attempt.
                    try:
                        sock.settimeout(2)
                        sig = sock.recv(1)
                        if sig:
                            signal_byte = sig
                            if sig == b"v":
                                receive_voice_response(sock)
                    except OSError:
                        pass
                    break

    finally:
        # Always clean up both resources, even if an exception occurred above.
        # ffmpeg.terminate() sends SIGTERM; escalate to SIGKILL if it doesn't
        # exit within 0.5 s (rare but possible if the capture device is stuck).
        if ffmpeg.poll() is None:
            ffmpeg.terminate()
            try:
                ffmpeg.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                ffmpeg.kill()
                ffmpeg.wait()
        sock.close()

    # --- Step 7: play ack/nack, or nothing if voice response was already played ---
    if signal_byte == b"c":
        play_wav(ACK_WAV)       # command matched and executed — success chirp
    elif signal_byte == b"f":
        play_wav(NACK_WAV)      # no phrase matched — failure chirp
    elif signal_byte == b"v":
        pass                    # voice response already played in receive_voice_response()
    elif signal_byte == b"O":
        pass                    # channel ran to completion; close chirp already played
    else:
        print(f"[listener] no/unknown signal byte: {signal_byte!r}")

    # --- Step 8: event-driven SCO teardown ---
    # play_wav() uses subprocess.run(), which blocks until pw-play exits.
    # That exit IS the "last audio frame left the pipeline" event — no timer
    # needed.  Tearing down here is instantaneous and cannot be premature.
    force_sco_teardown()


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main():
    print(f"[listener] badge={BADGE_MAC}  server={SERVER_HOST}:{SERVER_PORT}")
    print(f"[listener] source={SOURCE}")
    print(f"[listener] sink={SINK}")

    # Play the badge-online sound (cold SCO start via play_wav_cold).
    # This confirms audio is routing to the badge before we wait for the server.
    with audio_lock:
        play_wav_cold(BADGE_ONLINE_WAV)

    # Establish the persistent downlink (background thread).  It plays
    # maincomputeronline.wav on every successful (re)connect and services
    # server-pushed audio for the life of the session.  The badge should
    # not appear ready before there is a server, so wait for the first
    # connect here before accepting taps.
    threading.Thread(target=downlink_loop, daemon=True).start()
    print(f"[listener] waiting for downlink to {SERVER_HOST}:{SERVER_PORT} — "
          f"start computer.py on that host if it isn't running...")
    downlink_up.wait()
    print(f"[listener] tap badge once → chirp → speak a command")

    last_path = None
    last_tap  = 0.0

    while True:
        # Re-discover the badge input device each iteration.  The /dev/input/
        # path can change on Bluetooth reconnection, so we look it up fresh.
        path = find_badge_input()
        if not path:
            if last_path:
                print("[listener] badge input lost, searching...")
                last_path = None
            time.sleep(0.5)
            continue
        if path != last_path:
            print(f"[listener] badge input at {path}, waiting for tap...")
            last_path = path

        try:
            dev = evdev.InputDevice(path)
        except OSError:
            time.sleep(0.5)
            continue

        print("[listener] waiting for tap...")
        try:
            while True:
                # select() on dev.fd avoids busy-polling and allows a clean
                # exit path if the badge disconnects (OSError on read).
                # 0.5 s timeout keeps the outer loop responsive to reconnects.
                r, _, _ = select.select([dev.fd], [], [], 0.5)
                if not r:
                    continue   # no events in 0.5 s — loop back to select

                try:
                    events = list(dev.read())
                except BlockingIOError:
                    events = []

                for ev in events:
                    # EV_KEY with value 1 = key-down event (key pressed).
                    # The badge single-tap fires keycode 200 or 201 (KEY_PAUSECD)
                    # depending on firmware.  We accept both.
                    if (ev.type == evdev.ecodes.EV_KEY
                            and ev.code in (200, 201)
                            and ev.value == 1):
                        if time.time() - last_tap < TAP_DEBOUNCE:
                            continue   # too soon after last tap — debounce
                        last_tap = time.time()
                        print("[listener] tap")
                        stream_and_handle_response()
                        # NOTE: we do NOT reset last_tap back to 0 here.
                        # The device is closed and reopened after each tap
                        # cycle (see dev.close() in the finally block below).
                        # Closing and reopening flushes the kernel event queue,
                        # discarding any spurious re-fire events that SCO
                        # teardown can trigger.  Resetting last_tap here would
                        # add a needless 2 s blackout after audio playback ends.
                        break

                else:
                    # Python for-else: the `else` clause runs only when the
                    # for loop exhausted its iterator without hitting `break`.
                    # Here that means: no tap event in this batch of events.
                    # Continue the outer while loop to wait for more events.
                    continue

                # A tap was found and handled — break out of the inner while
                # loop so we close and reopen the device (flush the queue).
                break

        except OSError as e:
            print(f"[listener] input device error: {e}; reopening")
        finally:
            # Close the device on every exit from the inner loop: normal tap,
            # error, or device disconnect.  This flushes the kernel event
            # queue so stale events don't trigger a false tap next cycle.
            try:
                dev.close()
            except Exception:
                pass


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[listener] shutting down")
