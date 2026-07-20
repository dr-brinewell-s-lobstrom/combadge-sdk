#!/usr/bin/env python3
"""
Minimal Combadge Server / Main Computer (TOS SDK).

This script runs on any machine that can reach the relay host over TCP —
Linux, macOS, or Windows.  It does not need Bluetooth at all.  All audio
I/O (badge speaker and microphone) happens on the relay side.

What it does for each incoming connection from listener.py:
  1. Read the 18-byte handshake: 1 tap-type byte (b'1') + 17-byte ASCII
     badge MAC (colon-separated).  The MAC keys the session — this is the
     same dialect the full maincomputer speaks, so multiple badges on
     multiple transceivers can be told apart.  A client that sends no MAC
     falls back to the console sentinel 00:00:00:00:00:00.
  2. Discard the 44-byte WAV header forwarded by listener.py.
     (Vosk needs raw PCM, not a WAV container.)
  3. Feed the 16 kHz mono PCM stream into a Vosk speech recognizer.
  4. After each chunk, check both partial and final recognized text
     for a matching phrase in the COMMANDS dict.
  5. On the first match, synthesize a voice response WAV and send it back.
     If no match is found within TIMEOUT_S, send b'f' (failure).

Concurrency: one thread per connection, so two badges never block each
other.  The Vosk Model is loaded once and shared (thread-safe for this);
each connection builds its own KaldiRecognizer (NOT thread-safe, never
shared).

Persistent downlink (push path): a relay may instead open with b'h' + MAC
and hold the connection open.  The server keeps it alive with b'k' every
5 s and can push b'v' voice frames down it at any time — audio that plays
on the badge with no tap.  Console commands (stdin): `badges` lists known
badges; `hail <mac> [text]` pushes TTS to one.  See sdk/INTERCOM.md.

Response signals back to listener.py:
    b'c'                       — command matched (no audio, badge plays ACK chirp)
    b'f'                       — no match      (no audio, badge plays NACK chirp)
    b'v' + 4-byte size + WAV   — voice response (badge plays the WAV)

TTS (text-to-speech) is handled automatically per platform:
    Linux / macOS  →  espeak-ng   (sudo apt install espeak-ng)
    Windows        →  PowerShell System.Speech (built-in, no install required)

Usage:
    python3 computer.py /path/to/vosk-model-small-en-us-0.15

Vosk model: download vosk-model-small-en-us-0.15 (~40 MB) from
    https://alphacephei.com/vosk/models
Unpack the directory anywhere and pass its path on the command line.

To customize commands, edit the COMMANDS dict below.

Stripped down from maincomputer/maincomputer.py: no CSV dispatch, no
captain's log, no identify/auth gating, no large-vocab switch, no theatrics,
no host overrides.  Single-tap command loop plus the persistent downlink
and server console (sdk/INTERCOM.md Phase 2).
"""
import array
import io
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import wave

from vosk import KaldiRecognizer, Model, SetLogLevel

SetLogLevel(-1)   # silence Vosk's verbose initialization chatter

PORT      = int(os.environ.get("SDK_SERVER_PORT", "1701"))
TIMEOUT_S = 10    # max wall-clock seconds to wait for speech before sending b'f'


# ---------------------------------------------------------------------------
# Badge registry — which badges we have heard from, and from where.
#
# Populated on every handshake.  Today this is bookkeeping (per-MAC log
# lines, visibility into who is on the air); the intercom phases build on
# it: the persistent-downlink map (Phase 2) and hail routing (Phase 3) both
# key off the badge MAC.  See sdk/INTERCOM.md.
#
# Guarded by badges_lock because connection handlers run on their own
# threads and may register concurrently.
# ---------------------------------------------------------------------------

badges      = {}                  # MAC -> {"addr": (ip, port), "last_seen": epoch}
badges_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Downlink registry — the server's push path to each badge.
#
# A relay opens a second, long-lived connection at startup (handshake
# b'h' + MAC) and holds it open.  We keep it alive with b'k' keepalives
# and can send a b'v' voice frame down it at ANY time — that is how audio
# reaches a badge whose user did nothing (hail delivery).  Everything
# relay-initiated (taps) still uses ordinary b'1' connections.
#
# Each entry carries its own send lock: keepalives (run_downlink) and
# voice pushes (push_voice) come from different threads, and a keepalive
# byte interleaved into the middle of a b'v' frame would corrupt it.
# ---------------------------------------------------------------------------

downlinks      = {}               # MAC -> {"sock": socket, "lock": Lock, "addr": (ip, port)}
downlinks_lock = threading.Lock()

KEEPALIVE_S    = 5                # downlink keepalive cadence; relay calls 15 s of silence dead

# ---------------------------------------------------------------------------
# Hail state — badge-to-badge calls (sdk/INTERCOM.md Phase 3).
#
# A hail is "<self-alias> to <target-alias>" spoken after a tap
# ("captain to engineering ...").  The caller's whole utterance — their
# actual voice — is delivered to the target badge via its downlink, then a
# pending-hail window opens: if the target's user taps within
# HAIL_ANSWER_S, that tap answers the hail (Phase 4 opens the channel);
# otherwise the caller hears "There is no response from <name>."
#
# Aliases live in sdk/aliases.conf (alias -> MAC, many-to-one), reloaded on
# every tap.  The first alias of a badge is its spoken name.
# ---------------------------------------------------------------------------

ALIASES_FILE       = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "aliases.conf")
HAIL_SILENCE_S     = float(os.environ.get("SDK_HAIL_SILENCE_S", "1.5"))
                                  # end-of-hail: seconds without new recognized words
HAIL_ANSWER_S      = float(os.environ.get("SDK_HAIL_ANSWER_S", "30"))
                                  # window for the target's user to tap and answer
HAIL_MAX_CAPTURE_S = 20           # hard cap on hail capture (noisy-room backstop)

pending_hails      = {}           # target MAC -> channel entry dict (see handle_hail)
pending_hails_lock = threading.Lock()

CHANNEL_GAIN       = float(os.environ.get("SDK_CHANNEL_GAIN", "6"))
                                  # per-chunk software gain on bridged mic PCM —
                                  # SCO mic level vs speaker level, the same
                                  # mismatch hail normalization fixes, applied
                                  # per-chunk here for realtime.  Lowered
                                  # from 12 to 6 alongside the gate-threshold
                                  # drop: passing more borderline audio and
                                  # amplifying less keeps the noise floor
                                  # inaudible without chopping the signal.

# 17-char colon-separated MAC, e.g. "2C:F2:DF:45:EC:28" (case-insensitive).
MAC_RE      = re.compile(r"^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$")

# Sentinel identity for clients that send no MAC handshake (matches the
# full maincomputer's convention for console/legacy sessions).
CONSOLE_MAC = "00:00:00:00:00:00"


# ---------------------------------------------------------------------------
# COMMANDS — edit this dict to define your own voice commands.
#
# Key:   the phrase to listen for (matched as a substring of recognized text)
# Value: the spoken response — either a plain string, or a callable that
#        returns a string (for responses computed at match time)
#
# Matching notes:
#   - Matching is a simple `phrase in recognized_text` substring check.
#     "computer hello" matches "computer hello there" or "hey computer hello".
#   - Matching fires as soon as Vosk's partial OR final result contains the
#     phrase — you do not have to wait for the speaker to stop talking.
#     This cuts latency by ~0.5–1 s on short commands.
#   - The small Vosk model uses general en-US acoustics, not a custom grammar.
#     Unusual words (proper nouns, acronyms) may not recognize
#     reliably.  Stick to common English words for best results.
#   - To see what Vosk actually hears, watch the "[computer] final text:" log
#     line while speaking.  Use that to calibrate your phrases.
#
# Adding a new command:
#   "computer lights red": "Aye, switching to red alert.",
#   "computer play music": lambda: play_something_and_return_confirmation(),
# ---------------------------------------------------------------------------

def _time_phrase():
    """Current time phrased for TTS, military style: "sixteen eleven hours."

    A raw strftime("%H%M") like "1611" is read by TTS engines as the number
    "one thousand six hundred eleven".  Splitting hour and minute into two
    small numbers makes every engine read it as spoken military time:
        16:11 -> "16 11 hours."        (reads: sixteen eleven hours)
        11:08 -> "11 oh 8 hours."      (reads: eleven oh eight hours)
        14:00 -> "14 hundred hours."   (reads: fourteen hundred hours)
    """
    t = time.localtime()
    if t.tm_min == 0:
        return f"{t.tm_hour} hundred hours."
    if t.tm_min < 10:
        return f"{t.tm_hour} oh {t.tm_min} hours."
    return f"{t.tm_hour} {t.tm_min} hours."


COMMANDS = {
    "computer hello":   "Hello.",
    "computer status":  "All systems nominal.",
    "computer time":    _time_phrase,   # e.g. "sixteen eleven hours."
    "computer goodbye": "Acknowledged.",
}


# ---------------------------------------------------------------------------
# Text-to-speech synthesis
# ---------------------------------------------------------------------------

def _synth_sapi(text, path):
    """Synthesize `text` to a WAV file at `path` using Windows SAPI via PowerShell.

    Uses System.Speech.SpeechSynthesizer, which ships with .NET on all
    modern Windows versions — no extra packages needed.

    Rate = 2 is slightly faster than the default (0); valid range is -10 to +10.

    Single-quotes in `text` are escaped by doubling them ("''") because the
    text is embedded in a PowerShell single-quoted string literal.
    """
    safe = text.replace("'", "''")   # escape for PS single-quoted string
    ps = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        # Prefer a female voice (Microsoft Zira on stock Windows).  Without
        # this hint SAPI uses the system default, typically David (male).
        "$s.SelectVoiceByHints('Female'); "
        "$s.Rate = 2; "
        f"$s.SetOutputToWaveFile('{path}'); "
        f"$s.Speak('{safe}'); "
        "$s.Dispose()"
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps],
        check=True, capture_output=True, timeout=15,
    )


def synth_wav(text):
    """Generate a WAV file from `text` using the platform's TTS engine.

    Returns the path to a temporary WAV file, or None if synthesis fails.
    The caller is responsible for deleting the file after use.

    Platform dispatch:
      Windows  → _synth_sapi()  (PowerShell System.Speech, no install needed)
      Linux    → espeak-ng      (sudo apt install espeak-ng)
      macOS    → espeak-ng      (brew install espeak-ng, or adapt to use `say`)

    espeak-ng flags used:
      -v en-us   US English voice
      -s 165     speech rate in words per minute (default ~160; 165 is natural)
      -w path    write output to WAV file (instead of playing through speakers)
    """
    fd, path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    try:
        if sys.platform == "win32":
            _synth_sapi(text, path)
        else:
            subprocess.run(
                ["espeak-ng", "-v", "en-us", "-s", "165", "-w", path, text],
                check=True, capture_output=True, timeout=10,
            )
        return path
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        print(f"[computer] TTS failed: {e}", file=sys.stderr)
        try:
            os.unlink(path)
        except OSError:
            pass
        return None


# ---------------------------------------------------------------------------
# Response framing and dispatch
# ---------------------------------------------------------------------------

def send_voice(conn, text, mac=CONSOLE_MAC):
    """Synthesize `text` as a WAV and send it over the connection as a voice frame.

    `mac` is the badge this response is going to — used only for the log
    line, so concurrent sessions from different badges are distinguishable
    on the console.

    Wire format sent to listener.py:
        b'v'            — 1 byte: signals a voice response follows
        <4-byte size>   — big-endian unsigned int: byte length of the WAV data
        <WAV bytes>     — exactly `size` bytes of WAV file content

    listener.py reads the size, buffers the WAV to a temp file, and plays it
    through the badge speaker.

    If TTS synthesis fails, sends b'f' (failure signal) so the badge plays
    the error chirp instead of silently doing nothing.
    """
    path = synth_wav(text)
    if not path:
        conn.sendall(b"f")   # TTS failed — badge will play failure chirp
        return
    try:
        with open(path, "rb") as f:
            data = f.read()
        conn.sendall(b"v")
        conn.sendall(len(data).to_bytes(4, "big"))   # 4-byte big-endian size
        conn.sendall(data)
        print(f"[computer] [{mac}] sent voice response: {text!r} ({len(data)} bytes)")
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def drain_connection(conn):
    """Read and discard any leftover inbound audio, then let the caller close.

    WHY THIS IS REQUIRED (TCP RST truncation):
      listener.py streams mic PCM continuously and only stops the moment it
      receives our signal byte.  That means when handle_connection() returns,
      there is almost always unread PCM sitting in this socket's receive
      buffer.  Closing a socket with unread data pending makes the OS send a
      TCP RST (abortive close) instead of a graceful FIN — and RST discards
      the voice-response WAV still in transit to the relay.  The audible
      symptom: the badge starts playing the response, then it cuts off
      mid-word at a random point.

      Draining until EOF (the relay half-closes its send side once it has
      our signal byte) guarantees a graceful FIN close and full delivery of
      the response.  The 3 s timeout bounds the wait for relays that don't
      half-close promptly; by then the in-flight audio has been consumed, so
      the close is clean either way.

    The full maincomputer.py applies this same drain-before-close for the
    same reason — see MAINCOMPUTER.md / RELAY-MOBILE.md "Windows-safe
    socket close".
    """
    conn.settimeout(3)
    try:
        while conn.recv(4096):
            pass
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Alias directory and hail matching
# ---------------------------------------------------------------------------

def load_aliases():
    """Parse aliases.conf → (alias_to_mac, mac_to_aliases).

    Called on every tap so edits take effect live.  A missing file simply
    disables hails (both maps empty) — the command loop is unaffected.
    Aliases are normalized to lowercase single-spaced (matching Vosk
    output); an alias claimed by two badges is reported and the first
    mapping wins.  mac_to_aliases preserves file order — the FIRST alias is
    the badge's spoken name, used in responses about it.
    """
    alias_to_mac   = {}
    mac_to_aliases = {}
    if not os.path.isfile(ALIASES_FILE):
        return alias_to_mac, mac_to_aliases
    with open(ALIASES_FILE) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            mac_part, alias_part = line.split("=", 1)
            file_mac = mac_part.strip().upper()
            if not MAC_RE.match(file_mac):
                print(f"[computer] aliases.conf: bad MAC {mac_part.strip()!r} — line skipped")
                continue
            for raw in alias_part.split(","):
                alias = " ".join(raw.lower().split())
                if not alias:
                    continue
                if alias in alias_to_mac and alias_to_mac[alias] != file_mac:
                    print(f"[computer] aliases.conf: {alias!r} maps to both "
                          f"{alias_to_mac[alias]} and {file_mac} — keeping first")
                    continue
                alias_to_mac[alias] = file_mac
                mac_to_aliases.setdefault(file_mac, []).append(alias)
    return alias_to_mac, mac_to_aliases


def badge_name(mac, mac_to_aliases):
    """The badge's spoken name: its first alias, or the bare MAC if none."""
    aliases = mac_to_aliases.get(mac)
    return aliases[0] if aliases else mac


def hail_phrases(caller_mac, alias_to_mac, mac_to_aliases):
    """Every valid hail phrase for this caller, longest first.

    Cross-product of the caller's own aliases with every OTHER badge's
    aliases: "<self> to <target>".  Validity is by construction — a phrase
    using someone else's self-alias, or targeting the caller's own badge,
    is simply never generated, so it can never match.  Longest-first
    ordering makes the most specific target win if one alias happens to be
    a prefix of another.
    """
    pairs = []
    for self_alias in mac_to_aliases.get(caller_mac, []):
        for target_alias, target_mac in alias_to_mac.items():
            if target_mac == caller_mac:
                continue
            pairs.append((f"{self_alias} to {target_alias}", target_mac))
    pairs.sort(key=lambda p: len(p[0]), reverse=True)
    return pairs


def match_hail(text, hails):
    """Return (phrase, target_mac) for the first hail phrase in `text`, or None."""
    for phrase, target_mac in hails:
        if phrase in text:
            return phrase, target_mac
    return None


def detect_unknown_hail(final_text, caller_mac, alias_to_mac, mac_to_aliases):
    """Detect '<self-alias> to <unknown-name>' in FINAL text only.

    Returns the unknown name (for "There is no listing for X.") or None.
    Final-text only by design: evaluating this on partials would
    false-positive on half-spoken alias names ("captain to engi...").
    """
    for self_alias in mac_to_aliases.get(caller_mac, []):
        marker = f"{self_alias} to "
        if marker not in final_text:
            continue
        remainder = final_text.split(marker, 1)[1].strip()
        if not remainder:
            continue
        for alias in alias_to_mac:
            if remainder.startswith(alias):
                return None   # known alias — a real hail path handles it
        # Cap at a few words: trailing noise segments (Vosk hears silence
        # as 'huh' etc.) shouldn't ride into the spoken response.
        return " ".join(remainder.split()[:4])
    return None


HAIL_TARGET_PEAK   = 29000  # normalize to ~90% of 16-bit full scale
HAIL_MAX_GAIN      = 20.0   # never amplify more than this (dead-air guard)
HAIL_TRIM_GRACE_MS = 200    # audio kept either side of detected speech


def prepare_hail_pcm(pcm_bytes, tag=""):
    """Trim dead air and loudness-normalize a hail capture (one pass).

    TRIM (Captain's directive: any mechanism that shortens silence padding
    after the last spoken word is worth pursuing and eliminating):
      The capture buffer starts at the tap, so the front carries
      chirp-gap/breath dead air; and silence-finalize GUARANTEES ~1.5 s+ of
      dead air at the tail.  Both pad the badge-occupied playback window on
      the receiving side and delay the earliest possible answer tap.  We
      find the first and last sample whose magnitude exceeds an adaptive
      threshold (max(250, ref/8) — scales with capture level, floors above
      the SCO noise floor) and keep HAIL_TRIM_GRACE_MS around them so word
      onsets/decays aren't clipped.

    NORMALIZE: badge-mic SCO captures are far quieter than the near-full-
    scale TTS/chirps badges otherwise play.  Gain references the
    99.5th-percentile magnitude, NOT the absolute peak — SCO captures carry
    near-full-scale transients (link pops, tap clicks) and a single spike
    makes peak-based gain compute ~1.0x and silently do nothing (observed
    on-badge).  Spikes driven past full scale simply clip.  Gain capped at
    HAIL_MAX_GAIN so dead air is never amplified into hiss.
    """
    samples = array.array("h")
    samples.frombytes(pcm_bytes)
    if not samples:
        return pcm_bytes
    magnitudes = sorted(abs(s) for s in samples)
    ref  = magnitudes[int(len(magnitudes) * 0.995)]
    peak = magnitudes[-1]
    if ref == 0:
        return pcm_bytes

    # --- trim ---
    threshold = max(250, ref // 8)
    first = next((i for i, s in enumerate(samples) if abs(s) > threshold), None)
    if first is None:
        return pcm_bytes          # nothing but dead air — deliver untouched
    last  = next(i for i in range(len(samples) - 1, -1, -1)
                 if abs(samples[i]) > threshold)
    grace = int(16000 * HAIL_TRIM_GRACE_MS / 1000)
    lo    = max(0, first - grace)
    hi    = min(len(samples), last + 1 + grace)
    lead_ms = int(lo / 16)
    tail_ms = int((len(samples) - hi) / 16)
    samples = samples[lo:hi]

    # --- normalize ---
    gain = min(HAIL_TARGET_PEAK / ref, HAIL_MAX_GAIN)
    print(f"[computer] {tag}audio: trimmed lead {lead_ms}ms tail {tail_ms}ms, "
          f"ref(99.5%)={ref} peak={peak} gain={gain:.1f}x")
    if gain > 1.0:
        samples = array.array("h", (max(-32768, min(32767, int(s * gain)))
                                    for s in samples))
    return samples.tobytes()


def wav_from_pcm(pcm_bytes):
    """Wrap raw 16 kHz mono 16-bit PCM in a WAV container, in memory.

    Used to frame the caller's buffered hail utterance for delivery — the
    relay's downlink player expects a complete WAV file, same as TTS pushes.
    """
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(pcm_bytes)
    return buf.getvalue()


def match_command(text):
    """Return the response for the first COMMANDS phrase found in `text`, or None.

    Iterates COMMANDS in insertion order (Python 3.7+ dict guarantee) and
    returns the first match.  Calls the value if it's callable (lambda),
    otherwise returns it directly as a string.
    """
    for phrase, response in COMMANDS.items():
        if phrase in text:
            return response() if callable(response) else response
    return None


# ---------------------------------------------------------------------------
# Per-connection handler
# ---------------------------------------------------------------------------

def handle_connection(conn, addr, model):
    """Handle one complete tap session.

    Called once per accepted TCP connection (on its own thread).  Reads the
    protocol handshake, runs Vosk recognition on the streaming PCM, and
    sends a response.  The connection is drained and closed by
    serve_connection() after this function returns.

    Vosk recognition approach:
      AcceptWaveform(data) processes a chunk of raw PCM.  It returns True
      when Vosk decides a segment of speech has ended (a "finalized" utterance).
        → rec.Result()        JSON {"text": "..."}    the finalized text

      When it returns False, the utterance is still in progress.
        → rec.PartialResult() JSON {"partial": "..."} interim running text

      We check BOTH partial and final on every chunk so the match can fire
      as soon as the key phrase is recognized — without waiting for the
      speaker to pause.  This typically saves 0.5–1 s of latency.

      After the audio stream ends (or TIMEOUT_S wall-clock seconds pass),
      we call FinalResult() one last time to flush any buffered audio.
    """
    conn.settimeout(TIMEOUT_S + 5)   # outer safety net against hung connections
    try:
        # --- read tap byte ---
        tap = conn.recv(1)
        if not tap:
            # Connection opened and immediately closed with no data (port
            # scan, liveness probe).  Not an error; ignore silently.
            return
        if tap not in (b"1", b"h"):
            print(f"[computer] unexpected tap byte: {tap!r}")
            return

        # --- read the 17-byte ASCII badge MAC (handshake) ---
        # listener.py sends its badge MAC right after the tap byte so the
        # server can key the session by badge identity.  Legacy fallback:
        # if these 17 bytes are not a MAC, the client skipped the handshake
        # and they are actually the start of the WAV header ("RIFF...").
        # In that case adopt the console sentinel and count the 17 bytes
        # against the 44-byte header discard below.
        ident = b""
        while len(ident) < 17:
            chunk = conn.recv(17 - len(ident))
            if not chunk:
                return   # connection closed mid-handshake
            ident += chunk
        try:
            ident_text = ident.decode("ascii")
        except UnicodeDecodeError:
            ident_text = ""
        if MAC_RE.match(ident_text):
            mac = ident_text.upper()
            header_remaining = 44
        else:
            mac = CONSOLE_MAC
            header_remaining = 44 - 17   # the 17 bytes were WAV header
            print(f"[computer] [{mac}] no MAC handshake from {addr} — legacy client?")

        # Register/refresh this badge in the shared registry.
        with badges_lock:
            badges[mac] = {"addr": addr, "last_seen": time.time()}

        # --- downlink registration (b'h') ---
        # This is not a tap: the relay is offering us a persistent push
        # channel.  Hand the connection to run_downlink(), which holds it
        # until it dies.  A valid MAC is mandatory — the whole point of the
        # downlink is knowing which badge it reaches.
        if tap == b"h":
            if mac == CONSOLE_MAC:
                print(f"[computer] downlink from {addr} rejected: no valid MAC handshake")
                return
            run_downlink(conn, addr, mac)
            return

        print(f"[computer] [{mac}] tap session from {addr[0]}:{addr[1]}")

        # --- discard the (rest of the) 44-byte WAV header ---
        # listener.py forwards ffmpeg's output verbatim, which starts with a
        # standard 44-byte WAV header before the raw PCM samples.  Vosk's
        # KaldiRecognizer expects raw PCM, so we read and discard the header.
        # (The 44-byte size is fixed for the ffmpeg output format we use.)
        header = b""
        while len(header) < header_remaining:
            chunk = conn.recv(header_remaining - len(header))
            if not chunk:
                return   # connection closed before header was complete
            header += chunk

        # --- answer-tap check (pending inbound hail) ---
        # A tap from a badge with a pending hail is an ANSWER, not a
        # command: consume it immediately — no speech required — and become
        # one end of the live channel (this thread pumps answerer->caller
        # until the channel closes).  Benign race: a tap landing at the
        # exact moment the window expires gets a failure chirp while the
        # caller hears "no response" — both sides terminate cleanly.
        with pending_hails_lock:
            hail_entry = pending_hails.pop(mac, None)
        if hail_entry:
            print(f"[computer] [{mac}] tap answers pending hail from {hail_entry['from']}")
            run_channel_answer(conn, mac, hail_entry)
            return

        # --- Vosk recognition loop ---
        # KaldiRecognizer(model, sample_rate): 16000 Hz matches the HFP SCO rate.
        rec         = KaldiRecognizer(model, 16000)
        start       = time.time()
        pcm         = []   # every raw chunk since stream start — a hail
                           # replays the caller's actual voice, so the full
                           # utterance is kept from the very beginning
        segments    = []   # every FINALIZED Vosk segment, in order.  Vosk
                           # closes a segment at each pause; keeping only
                           # the latest would discard earlier speech, and
                           # FinalResult() at stream end only flushes the
                           # LAST segment — so speech followed by silence
                           # would otherwise vanish before the final checks
                           # (observed as final text 'huh' on real hails).
        alias_to_mac, mac_to_aliases = load_aliases()
        hails       = hail_phrases(mac, alias_to_mac, mac_to_aliases)
        print(f"[computer] [{mac}] receiving audio")

        while time.time() - start < TIMEOUT_S:
            try:
                data = conn.recv(4096)   # raw PCM chunks from listener.py
            except socket.timeout:
                break
            if not data:
                break   # listener.py closed the connection (end of speech or disconnect)
            pcm.append(data)

            if rec.AcceptWaveform(data):
                # Vosk finalized a segment — bank it and clear the partial.
                seg = json.loads(rec.Result()).get("text", "")
                if seg:
                    segments.append(seg)
                partial = ""
            else:
                # Utterance still in progress — interim partial text.
                partial = json.loads(rec.PartialResult()).get("partial", "")

            # Match against everything heard so far: banked segments plus
            # the live partial — so phrases spanning a segment boundary
            # ("captain to" [pause] "engineering") still match.
            accumulated = " ".join(segments + ([partial] if partial else []))
            if accumulated:
                # Hail check first: hail phrases are cross-products of the
                # alias directory and never collide with COMMANDS entries.
                hit = match_hail(accumulated, hails)
                if hit:
                    handle_hail(conn, mac, hit[1], hit[0], rec, pcm, mac_to_aliases)
                    return
                response = match_command(accumulated)
                if response is not None:
                    print(f"[computer] [{mac}] match on {accumulated!r} -> {response!r}")
                    send_voice(conn, response, mac)
                    return   # done — serve_connection() drains and closes

        # --- final flush after stream ends or timeout ---
        # FinalResult() forces Vosk to emit whatever it has buffered.
        # This catches commands spoken near the end of the RECORD_MAX_S window.
        tail = json.loads(rec.FinalResult()).get("text", "")
        if tail:
            segments.append(tail)
        final = " ".join(segments)   # the WHOLE utterance, all segments
        if final:
            print(f"[computer] [{mac}] final text: {final!r}")
            # Late hail catch — spoken too fast for the real-time loop.
            # The stream may already be over; handle_hail copes (its capture
            # loop finalizes immediately on EOF).
            hit = match_hail(final, hails)
            if hit:
                handle_hail(conn, mac, hit[1], hit[0], rec, pcm, mac_to_aliases)
                return
            response = match_command(final)
            if response is not None:
                send_voice(conn, response, mac)
                return
            # "<self-alias> to <name>" with an unknown name → say so, rather
            # than a bare failure chirp.  Final text only (see the helper).
            unknown = detect_unknown_hail(final, mac, alias_to_mac, mac_to_aliases)
            if unknown:
                print(f"[computer] [{mac}] hail to unknown name {unknown!r}")
                send_voice(conn, f"There is no listing for {unknown}.", mac)
                return

        # No phrase matched anywhere — tell the badge to play the failure chirp.
        print(f"[computer] [{mac}] no match")
        conn.sendall(b"f")

    except OSError as e:
        print(f"[computer] connection error: {e}", file=sys.stderr)


def serve_connection(conn, addr, model):
    """Thread body for one connection: handle it, then drain and close.

    Runs as a daemon thread — one per accepted connection — so a slow tap
    session on one badge (up to TIMEOUT_S of recognition plus TTS) never
    blocks another badge's session or startup probe.  The drain-before-close
    lives here so every exit path (match, no-match, error) gets the graceful
    FIN close; see drain_connection() for why that matters.
    """
    try:
        handle_connection(conn, addr, model)
    finally:
        drain_connection(conn)
        conn.close()


# ---------------------------------------------------------------------------
# Persistent downlink: hold, keepalive, push
# ---------------------------------------------------------------------------

def run_downlink(conn, addr, mac):
    """Hold a relay's persistent downlink open until it dies (thread body).

    Registers the socket in `downlinks` so push_voice() can reach this
    badge at any time, then alternates between watching for EOF and
    sending b'k' keepalives.  The recv timeout doubles as the keepalive
    cadence: the relay sends nothing after the handshake, so every
    KEEPALIVE_S the recv times out and we send one keepalive.  The relay
    treats 15 s without a byte as server-dead and reconnects.

    Reconnects: a relay whose old downlink is still registered (it saw a
    timeout we haven't noticed yet) simply connects again — the new entry
    replaces the old, the old socket is closed, and the old thread exits
    through the `is entry` guard below without deleting the new
    registration.

    Single-writer discipline: every send on this socket (keepalives here,
    voice frames in push_voice) holds entry["lock"], so a keepalive byte
    can never interleave into the middle of a pushed b'v' frame.
    """
    entry = {"sock": conn, "lock": threading.Lock(), "addr": addr}
    with downlinks_lock:
        old = downlinks.get(mac)
        downlinks[mac] = entry
    if old:
        try:
            old["sock"].close()
        except OSError:
            pass
    print(f"[computer] [{mac}] downlink registered from {addr[0]}:{addr[1]}")

    try:
        conn.settimeout(KEEPALIVE_S)
        while True:
            try:
                data = conn.recv(1)
                if not data:
                    break            # relay closed its end
                # The relay sends nothing after the handshake — ignore strays.
            except socket.timeout:
                # Quiet interval elapsed — keepalive time.
                try:
                    with entry["lock"]:
                        conn.sendall(b"k")
                except OSError:
                    break            # send failed — connection is dead
    except OSError:
        pass                         # any other socket error — treat as dead
    finally:
        with downlinks_lock:
            if downlinks.get(mac) is entry:
                del downlinks[mac]
        print(f"[computer] [{mac}] downlink closed")


def push_frame(mac, wav_bytes):
    """Push one b'v' voice frame down a badge's downlink.

    The shared low-level push: unsolicited playback on that badge, no tap
    required.  Same b'v' + 4-byte size + WAV framing as a tap-session voice
    response; the relay's downlink thread plays it with a cold-SCO start.
    Holds the entry's send lock so a keepalive can't interleave mid-frame.
    Returns True if the frame was written to the socket.
    """
    with downlinks_lock:
        entry = downlinks.get(mac)
    if not entry:
        print(f"[computer] [{mac}] no live downlink — cannot push")
        return False
    try:
        with entry["lock"]:
            entry["sock"].sendall(b"v")
            entry["sock"].sendall(len(wav_bytes).to_bytes(4, "big"))
            entry["sock"].sendall(wav_bytes)
        return True
    except OSError as e:
        print(f"[computer] [{mac}] push failed: {e}")
        return False


def push_voice(mac, text):
    """Synthesize `text` and push it to a badge — TTS front-end to
    push_frame().  Used by the console `hail` command and failure notices.
    """
    path = synth_wav(text)
    if not path:
        print(f"[computer] [{mac}] TTS failed — nothing pushed")
        return False
    try:
        with open(path, "rb") as f:
            data = f.read()
        if push_frame(mac, data):
            print(f"[computer] [{mac}] pushed voice: {text!r} ({len(data)} bytes)")
            return True
        return False
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Channel bridge (sdk/INTERCOM.md Phase 4)
#
# After an answered hail, the caller's tap socket and the answerer's tap
# socket become the two ends of a live intercom.  Each end's connection
# thread pumps ONE direction (caller thread: caller->answerer; answer
# thread: answerer->caller), so every socket has exactly one PCM writer.
# Relay->server audio stays raw (as in command mode); server->relay audio
# is framed b'A' + 2-byte BE len + PCM so the b'X' close byte stays
# distinguishable inside the stream.  Per-conn send locks serialize the
# b'X' against in-flight frames.
# ---------------------------------------------------------------------------

CHANNEL_GATE_OPEN  = float(os.environ.get("SDK_CHANNEL_GATE", "40"))
                                  # AVERAGE |sample| per chunk at/above which
                                  # the gate OPENS (not peak: noise spikes
                                  # have high peaks but low average; speech
                                  # has sustained average).  Below the gate,
                                  # chunks are sent as TRUE SILENCE — without
                                  # the gate, CHANNEL_GAIN amplifies the SCO
                                  # mic noise floor into constant static.
                                  # Halved from 80 alongside half-duplex mode:
                                  # peer-audio bleed into the local mic is
                                  # now suppressed structurally
                                  # (SDK_CHANNEL_HALF_DUPLEX_MS), so OPEN can
                                  # be sensitive to soft word onsets without
                                  # amplifying the peer's spillover.
CHANNEL_GATE_CLOSE = float(os.environ.get("SDK_CHANNEL_GATE_CLOSE",
                                            str(CHANNEL_GATE_OPEN * 0.4)))
                                  # Hysteresis: once open, the gate stays
                                  # open until the average drops below THIS
                                  # (lower) threshold for CHANNEL_GATE_HOLD
                                  # seconds.  A single-threshold gate flaps
                                  # on marginal signals (distant audio, quiet
                                  # room speech) — the second threshold
                                  # blocks that flap.  Env-var default is
                                  # 40% of OPEN; override to tune per-room.
CHANNEL_GATE_HOLD  = float(os.environ.get("SDK_CHANNEL_GATE_HOLD", "0.8"))
                                  # seconds the average must stay under CLOSE
                                  # before the gate actually shuts (replaces
                                  # the old CHANNEL_GATE_HANG hangover — this
                                  # is the same idea, longer default because
                                  # hysteresis already prevents rapid reopens)
CHANNEL_GATE_FADE_MS = int(os.environ.get("SDK_CHANNEL_GATE_FADE_MS", "10"))
                                  # linear fade over the boundary chunk on
                                  # every gate open/close transition — avoids
                                  # the click that a hard silence->audio
                                  # (or audio->silence) edge produces
CHANNEL_HALF_DUPLEX_MS = float(os.environ.get("SDK_CHANNEL_HALF_DUPLEX_MS", "800"))
                                  # HALF-DUPLEX mute: while the local badge's
                                  # speaker played peer audio within the last
                                  # N ms, its mic uplink is force-silenced
                                  # (regardless of the gate) so playback
                                  # bleeding into the mic can't feed back as
                                  # static.  Somewhat canonical behavior: i.e.
                                  # users learn to say "over".  Set to 0 to
                                  # disable and return to full duplex.
CHANNEL_FLOOR_RELEASE_MS = float(os.environ.get("SDK_CHANNEL_FLOOR_RELEASE_MS", "1000"))
                                  # FLOOR CONTROL (adjacent-badge / same-room
                                  # use, e.g. filming both ends): one talker at
                                  # a time — the first gate to open holds the
                                  # floor, the peer's uplink is hard-muted for
                                  # the whole hold; on release BOTH uplinks
                                  # stay muted for this guard window (longer
                                  # than the audio round-trip) so the tail
                                  # echo playing on the peer badge can't grab
                                  # a gate and sustain a feedback loop.  Half-
                                  # duplex alone can't stop cross-badge
                                  # coupling: the bleed enters the TALKER's
                                  # own open mic.  0 disables floor control.
CHANNEL_FLOOR_MAX_S = float(os.environ.get("SDK_CHANNEL_FLOOR_MAX_S", "12"))
                                  # cap on continuous floor hold — adjacent-
                                  # badge echo can pin the holder's gate open
                                  # past speech end; after this many seconds
                                  # the floor force-releases into the guard,
                                  # breaking any runaway.  0 = unlimited.


def _fade(samples, kind, n):
    """Linear fade over the first (kind='in') or last (kind='out') `n`
    samples of a signed-16 array, in place.  Zero-length fade or empty
    array is a no-op.  Used at gate open/close transitions to hide the
    silence<->audio edge click.
    """
    n = min(n, len(samples))
    if n <= 0:
        return
    if kind == "in":
        for i in range(n):
            samples[i] = int(samples[i] * i / n)
    else:  # "out"
        base = len(samples) - n
        for i in range(n):
            samples[base + i] = int(samples[base + i] * (n - 1 - i) / n)


def _pump_audio(src, dst, dst_lock, stop, entry, my_side, peer_side):
    """One bridge direction: raw mic PCM from src -> b'A' frames to dst.

    Per chunk: 16-bit alignment (odd-byte carry across recv boundaries),
    HYSTERETIC noise gate, HALF-DUPLEX check, then CHANNEL_GAIN
    amplification with clipping.  Gain only ever applies to audio that
    passed the gate, so the noise floor is never amplified.  Silence is
    still SENT (not skipped) to keep the peer's player fed at a constant
    rate.

    Gate state machine (per chunk, decided from `last_avg` = mean |sample|):
      CLOSED  →  OPEN   when last_avg >= CHANNEL_GATE_OPEN
                        (emit chunk with a fade-in over the leading edge)
      OPEN    →  CLOSED when last_avg has stayed <  CHANNEL_GATE_CLOSE
                        for CHANNEL_GATE_HOLD seconds
                        (emit chunk with a fade-out over the trailing edge)

    Half-duplex: while the LOCAL badge's speaker was fed peer audio within
    the last CHANNEL_HALF_DUPLEX_MS, its mic uplink is force-silenced
    (regardless of the gate).  This kills the feedback path where the
    badge speaker leaks into its own mic and echoes the peer back to
    themselves as static.  Users say "over" in canonical style; the
    system enforces it.

    Floor control (CHANNEL_FLOOR_RELEASE_MS > 0): one talker at a time.
    The first side whose gate opens claims the floor; the peer's uplink
    is hard-muted for the whole hold.  On the holder's gate close — or
    after CHANNEL_FLOOR_MAX_S of continuous hold — the floor releases
    into a guard window during which BOTH uplinks are muted.  Exists for
    ADJACENT badges (same-room filming): half-duplex cannot stop
    cross-badge coupling, because the peer-speaker bleed enters the
    TALKER's own open mic and round-trips as echo; the guard outlasts
    the round-trip so the echo dies instead of grabbing a gate.  Inert
    when badges are in separate rooms.

    `entry["speaker_last_audio"]` and `entry["floor"]` are shared state
    both pumps read/write; `my_side`/`peer_side` are the keys naming this
    thread's local badge and its peer respectively ("caller"/"answer").

    Runs until the channel stops, src ends, or dst rejects.
    """
    fade_samples  = int(16000 * CHANNEL_GATE_FADE_MS / 1000)
    hd_hold_s     = CHANNEL_HALF_DUPLEX_MS / 1000.0
    floor_guard_s = CHANNEL_FLOOR_RELEASE_MS / 1000.0
    floor_max_s   = CHANNEL_FLOOR_MAX_S
    speaker_last  = entry["speaker_last_audio"]
    floor         = entry["floor"]

    src.settimeout(0.5)
    carry        = b""
    gate_open    = False
    below_since  = None                   # first time last_avg dropped below CLOSE
    was_muted    = False                  # last emit was silenced by half-duplex —
                                          # fade in when we resume
    chunks = opened = hd_muted = fl_muted = 0
    last_avg = last_stat = 0.0
    while not stop.is_set():
        # Gate stats every 5 s — tuning instrument for the thresholds.
        if time.time() - last_stat >= 5:
            if chunks:
                print(f"[computer] gate: open {opened}/{chunks} chunks, "
                      f"hd-muted {hd_muted}, floor-muted {fl_muted}, "
                      f"last avg {int(last_avg)} "
                      f"(open ≥{int(CHANNEL_GATE_OPEN)}, "
                      f"close <{int(CHANNEL_GATE_CLOSE)})")
            chunks = opened = hd_muted = fl_muted = 0
            last_stat = time.time()
        try:
            data = src.recv(4096)
        except socket.timeout:
            continue
        except OSError:
            return
        if not data:
            return                       # this side hung up (close gesture)
        data  = carry + data
        cut   = len(data) // 2 * 2
        data, carry = data[:cut], data[cut:]
        if not data:
            continue
        s = array.array("h")
        s.frombytes(data)
        last_avg = sum(abs(x) for x in s) / len(s)
        now = time.time()
        chunks += 1

        # --- gate state transition decision ---
        transition = None                # "open" | "close" | None
        if gate_open:
            if last_avg >= CHANNEL_GATE_CLOSE:
                below_since = None       # signal still present; reset hold timer
            else:
                if below_since is None:
                    below_since = now
                elif now - below_since >= CHANNEL_GATE_HOLD:
                    transition = "close"
        else:
            if last_avg >= CHANNEL_GATE_OPEN:
                transition = "open"

        # --- half-duplex check: is our local speaker currently active? ---
        gate_would_emit = gate_open or transition == "open"
        hd_muted_now = (gate_would_emit
                        and hd_hold_s > 0
                        and now - speaker_last[my_side] < hd_hold_s)

        # --- floor control: one talker at a time (adjacent-badge coupling) ---
        floor_muted_now = False
        if floor_guard_s > 0 and gate_would_emit and not hd_muted_now:
            if now < floor["guard_until"]:
                floor_muted_now = True       # release guard — nobody transmits
            elif floor["holder"] == peer_side:
                floor_muted_now = True       # peer holds the floor
            elif floor["holder"] is None:
                floor["holder"] = my_side    # floor free — claim it
                floor["held_since"] = now
            elif floor_max_s > 0 and now - floor["held_since"] >= floor_max_s:
                # Held too long — adjacent-badge echo can pin our gate open
                # forever; force-release into the guard to break the loop.
                floor["holder"] = None
                floor["guard_until"] = now + floor_guard_s
                floor_muted_now = True

        # --- emit ---
        if gate_would_emit and not hd_muted_now and not floor_muted_now:
            if CHANNEL_GAIN > 1.0:
                s = array.array("h", (max(-32768, min(32767, int(x * CHANNEL_GAIN)))
                                      for x in s))
            # Fade-in on natural gate-open OR half-duplex release; fade-out
            # on natural gate-close.  (An HD-onset mid-transmission cuts
            # hard — no look-ahead — but the click is rarely audible over
            # the peer's speech.)
            if transition == "open" or was_muted:
                _fade(s, "in", fade_samples)
            elif transition == "close":
                _fade(s, "out", fade_samples)
            out = s.tobytes()
            opened += 1
            speaker_last[peer_side] = now
            was_muted = False
        else:
            out = b"\x00" * len(data)    # gated, HD-muted, or floor-muted: true silence
            if hd_muted_now:
                hd_muted += 1
                was_muted = True
            elif floor_muted_now:
                fl_muted += 1
                was_muted = True
            else:
                was_muted = False

        # --- commit state after emitting the transition chunk ---
        if transition == "open":
            gate_open   = True
            below_since = None
        elif transition == "close":
            gate_open   = False
            below_since = None
            if floor_guard_s > 0 and floor["holder"] == my_side:
                # Natural end of our transmission — release the floor into
                # the guard so the tail echo playing on the peer badge can't
                # grab a gate before it decays.
                floor["holder"] = None
                floor["guard_until"] = now + floor_guard_s

        try:
            with dst_lock:
                dst.sendall(b"A" + len(out).to_bytes(2, "big") + out)
        except OSError:
            return                       # other side is gone


def _close_channel(entry):
    """Idempotent channel shutdown: stop both pumps, b'X' both relays."""
    with entry["close_lock"]:
        if entry["closed"]:
            return
        entry["closed"] = True
    entry["stop"].set()
    for conn, lock in ((entry["caller_conn"], entry["lock_caller"]),
                       (entry["answer_conn"], entry["lock_answer"])):
        if conn:
            try:
                with lock:
                    conn.sendall(b"X")
            except OSError:
                pass


active_channels      = []            # live bridge entries (console `close`)
active_channels_lock = threading.Lock()


def run_channel_bridge(entry, caller_mac, target_mac):
    """Caller-thread half of the bridge.  Sends b'O' to both sides (the
    only moment both sockets are written by one thread — the answer thread
    is still parked on bridge_ready), releases the answer thread, then
    pumps caller->answerer until either side ends."""
    caller_conn, answer_conn = entry["caller_conn"], entry["answer_conn"]
    print(f"[computer] CHANNEL OPEN {caller_mac} <-> {target_mac}")
    try:
        caller_conn.sendall(b"O")
        answer_conn.sendall(b"O")
    except OSError:
        print("[computer] channel: b'O' delivery failed — closing")
        _close_channel(entry)
        entry["bridge_ready"].set()
        return
    entry["bridge_ready"].set()
    with active_channels_lock:
        active_channels.append(entry)
    try:
        _pump_audio(caller_conn, answer_conn, entry["lock_answer"], entry["stop"],
                    entry, my_side="caller", peer_side="answer")
    finally:
        _close_channel(entry)
        with active_channels_lock:
            if entry in active_channels:
                active_channels.remove(entry)
    print(f"[computer] CHANNEL CLOSED {caller_mac} <-> {target_mac}")


def run_channel_answer(conn, mac, entry):
    """Answer-thread half: register our socket, wake the caller thread,
    wait for the bridge, then pump answerer->caller."""
    entry["answer_conn"] = conn
    entry["answer_mac"]  = mac
    entry["answered"].set()
    if not entry["bridge_ready"].wait(10):
        # Raced the window expiry (or the caller thread died) — failure
        # chirp rather than silence.
        print(f"[computer] [{mac}] answer raced hail expiry — no channel")
        try:
            conn.sendall(b"f")
        except OSError:
            pass
        return
    if entry["closed"]:
        try:
            conn.sendall(b"f")
        except OSError:
            pass
        return
    _pump_audio(conn, entry["caller_conn"], entry["lock_caller"], entry["stop"],
                entry, my_side="answer", peer_side="caller")
    _close_channel(entry)


# ---------------------------------------------------------------------------
# Hail flow (sdk/INTERCOM.md Phase 3)
# ---------------------------------------------------------------------------

def handle_hail(conn, caller_mac, target_mac, phrase, rec, pcm, mac_to_aliases):
    """Run a matched hail to completion.  Steps:

      1. Target reachability — no live downlink → "X is not available."
      2. Capture the REST of the caller's utterance until HAIL_SILENCE_S
         passes with no new recognized words (so "captain to engineering,
         status report" is captured whole).  b'k' keepalives every ~4 s
         slide the relay's recording deadline during long messages.
      3. Deliver: the entire buffered utterance (the caller's actual voice,
         from tap start) is WAV-framed and pushed down the target's
         downlink — it plays on the target badge immediately.
      4. Pending window: register the hail and hold the caller's socket
         open (keepalives + draining their still-streaming mic) for
         HAIL_ANSWER_S.  A target tap answers it (Phase 4 opens the channel
         there); expiry → "There is no response from X."

    The caller's tap socket deliberately stays open the whole time — per
    the locked transport decision, it becomes the channel socket when the
    hail is answered.
    """
    target_name = badge_name(target_mac, mac_to_aliases)
    print(f"[computer] [{caller_mac}] hail matched: {phrase!r} -> {target_mac}")

    # --- 1. target reachable? ---
    with downlinks_lock:
        target_entry = downlinks.get(target_mac)
    if not target_entry:
        print(f"[computer] [{caller_mac}] hail target {target_mac} has no downlink")
        send_voice(conn, f"{target_name} is not available.", caller_mac)
        return

    # Prewarm the target NOW: the caller is still speaking and the silence
    # gate hasn't run yet — several seconds the target relay can spend
    # bringing SCO up.  By delivery time its sink is hot and the hail plays
    # near-instantly (no cold start, no 1 s prime).  Best-effort: a failed
    # or expired prewarm just means the relay falls back to its cold path.
    try:
        with target_entry["lock"]:
            target_entry["sock"].sendall(b"W")
    except OSError:
        pass

    # --- 2. capture the rest of the utterance (silence finalize) ---
    conn.settimeout(0.2)   # tight poll: the silence gate is checked per beat
    capture_start  = time.time()
    last_text      = ""
    last_progress  = time.time()
    last_keepalive = time.time()
    while True:
        if time.time() - last_progress >= HAIL_SILENCE_S:
            break                                    # end of speech
        if time.time() - capture_start >= HAIL_MAX_CAPTURE_S:
            print(f"[computer] [{caller_mac}] hail capture hard cap reached")
            break                                    # noisy-room backstop
        if time.time() - last_keepalive >= 4:
            try:
                conn.sendall(b"k")                   # slide relay's deadline
            except OSError:
                break                                # caller gone — deliver what we have
            last_keepalive = time.time()
        try:
            data = conn.recv(4096)
        except socket.timeout:
            continue                                 # no audio this beat; recheck timers
        except OSError:
            break
        if not data:
            break                                    # stream ended — finalize with what we have
        pcm.append(data)
        if rec.AcceptWaveform(data):
            text = json.loads(rec.Result()).get("text", "")
        else:
            text = json.loads(rec.PartialResult()).get("partial", "")
        if text and text != last_text:
            last_text     = text
            last_progress = time.time()              # still talking — slide the gate

    # --- 3. deliver the utterance to the target badge ---
    # Trimmed + normalized: see prepare_hail_pcm() — dead air at either end
    # delays the earliest answer tap; mic captures are quiet.
    hail_wav = wav_from_pcm(prepare_hail_pcm(b"".join(pcm),
                                             tag=f"[{caller_mac}] hail "))
    if not push_frame(target_mac, hail_wav):
        conn.settimeout(10)
        send_voice(conn, f"{target_name} is not available.", caller_mac)
        return
    print(f"[computer] [{caller_mac}] hail delivered to {target_mac} "
          f"({len(hail_wav)} bytes, {int(len(hail_wav) / 32000)}s audio)")

    # --- 4. pending window: hold the caller for the answer ---
    # The entry doubles as the channel-bridge state (Phase 4): sockets,
    # per-socket send locks, and the events coordinating the two threads.
    answered = threading.Event()
    entry    = {"from": caller_mac, "answered": answered,
                "caller_conn": conn, "answer_conn": None, "answer_mac": None,
                "bridge_ready": threading.Event(), "stop": threading.Event(),
                "lock_caller": threading.Lock(), "lock_answer": threading.Lock(),
                "close_lock": threading.Lock(), "closed": False,
                # Half-duplex bookkeeping — timestamps of the last b'A' frame
                # carrying real (non-silence) audio we sent to each side's
                # speaker.  Each pump checks its own side's slot before
                # emitting; a recent update means our local speaker is playing
                # peer audio, so our mic is picking up echo → suppress uplink.
                # Concurrent reads/writes of scalar float slots need no lock;
                # a stale read at worst mistimes the mute by one 128 ms chunk.
                "speaker_last_audio": {"caller": 0.0, "answer": 0.0},
                # Floor-control state shared by both pumps (see _pump_audio).
                "floor": {"holder": None, "guard_until": 0.0, "held_since": 0.0}}
    with pending_hails_lock:
        pending_hails[target_mac] = entry
    print(f"[computer] [{caller_mac}] awaiting answer from {target_mac} "
          f"({int(HAIL_ANSWER_S)}s window)")
    deadline       = time.time() + HAIL_ANSWER_S
    last_keepalive = time.time()
    try:
        while time.time() < deadline and not answered.is_set():
            if time.time() - last_keepalive >= 4:
                try:
                    conn.sendall(b"k")
                except OSError:
                    print(f"[computer] [{caller_mac}] caller lost during answer window")
                    return                           # no one left to answer to
                last_keepalive = time.time()
            try:
                data = conn.recv(4096)               # drain the still-open mic stream
            except socket.timeout:
                continue
            except OSError:
                print(f"[computer] [{caller_mac}] caller lost during answer window")
                return
            if not data:
                print(f"[computer] [{caller_mac}] caller closed during answer window")
                return

        conn.settimeout(10)                          # generous send window for the verdict
        if answered.is_set():
            print(f"[computer] [{caller_mac}] hail ANSWERED by {target_mac}")
            run_channel_bridge(entry, caller_mac, target_mac)
        else:
            print(f"[computer] [{caller_mac}] hail to {target_mac} expired unanswered")
            send_voice(conn, f"There is no response from {target_name}.", caller_mac)
    finally:
        with pending_hails_lock:
            if pending_hails.get(target_mac) is entry:
                del pending_hails[target_mac]


# ---------------------------------------------------------------------------
# Server console (background thread)
# ---------------------------------------------------------------------------

def resolve_mac(fragment):
    """Resolve a MAC fragment against live downlinks; must match exactly one.

    Lets the console say `hail 2F:60` instead of typing the full MAC.
    """
    frag = fragment.upper()
    with downlinks_lock:
        matches = [m for m in downlinks if frag in m]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        print(f"[console] no live downlink matches {fragment!r} (try 'badges')")
    else:
        print(f"[console] ambiguous {fragment!r}: {', '.join(matches)}")
    return None


def console_loop():
    """Interactive server console.  Commands:

        badges               list every badge heard from + downlink status
        hail <mac> [text]    push TTS to a badge's downlink — the audio
                             plays on that badge with no tap.  <mac> may be
                             any unique substring (e.g. "2F:60").  Default
                             text: "Incoming hail."

    Runs as a daemon thread reading stdin; exits quietly if stdin closes
    (e.g. when the server runs headless).
    """
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 2)
        cmd = parts[0].lower()

        if cmd == "badges":
            with badges_lock:
                snapshot = dict(badges)
            with downlinks_lock:
                live = set(downlinks)
            if not snapshot:
                print("[console] no badges seen yet")
            for mac, info in snapshot.items():
                dl  = "downlink UP" if mac in live else "downlink DOWN"
                age = int(time.time() - info["last_seen"])
                print(f"[console] {mac}  {info['addr'][0]}  last seen {age}s ago  {dl}")

        elif cmd == "hail":
            if len(parts) < 2:
                print("[console] usage: hail <mac-substring> [text]")
                continue
            mac = resolve_mac(parts[1])
            if mac:
                text = parts[2] if len(parts) > 2 else "Incoming hail."
                push_voice(mac, text)

        elif cmd == "close":
            # Force-close any live intercom channel (both relays get b'X').
            # Recovery hatch while badge-side close gestures are unreliable.
            with active_channels_lock:
                targets = list(active_channels)
            if not targets:
                print("[console] no open channel")
            for chan in targets:
                _close_channel(chan)
                print(f"[console] channel closed ({chan['from']} <-> "
                      f"{chan.get('answer_mac')})")

        else:
            print("[console] commands: badges | hail <mac> [text] | close")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 2:
        sys.exit("Usage: computer.py /path/to/vosk-model-small-en-us-0.15")
    model_path = sys.argv[1]
    if not os.path.isdir(model_path):
        sys.exit(f"Model dir not found: {model_path}")

    print(f"[computer] loading Vosk model {model_path}...")
    model = Model(model_path)   # takes ~1–3 s; model is reused for all connections
    print(f"[computer] listening on 0.0.0.0:{PORT}")
    print(f"[computer] commands: {' | '.join(COMMANDS)}")
    print(f"[computer] tap badge → speak one of the above → voice response plays through badge")
    print(f"[computer] console: badges | hail <mac> [text]")

    # Server console: `badges` and `hail <mac> [text]` — see console_loop().
    threading.Thread(target=console_loop, daemon=True).start()

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # SO_REUSEADDR: allow restarting the server immediately after a crash or
    # Ctrl-C without waiting ~60 s for the OS to reclaim the port.
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", PORT))
    srv.listen(8)   # room for several relays connecting/probing concurrently
    # 1 s timeout on accept() so Ctrl-C is delivered between accept attempts.
    # Without this, Winsock's accept() blocks indefinitely on Windows and
    # KeyboardInterrupt is never delivered.
    srv.settimeout(1.0)

    try:
        while True:
            try:
                conn, addr = srv.accept()
            except socket.timeout:
                continue   # no connection this second — loop back and check for Ctrl-C
            # One daemon thread per connection: multiple badges (PAN + BOX
            # transceivers, startup probes, future downlinks) are served
            # concurrently and never block each other.  Daemon threads die
            # with the process, so Ctrl-C still exits promptly even if a
            # session is mid-recognition.  No accept-time logging: probe
            # connections (listener.py's probe_server) open and close
            # without data, and logging them would spam the console — the
            # per-session log line is printed after the handshake instead.
            threading.Thread(target=serve_connection, args=(conn, addr, model),
                             daemon=True).start()
    except KeyboardInterrupt:
        print("\n[computer] shutting down")
    finally:
        srv.close()


if __name__ == "__main__":
    main()
