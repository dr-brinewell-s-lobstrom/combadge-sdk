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

A command returns either a string (spoken back) or the ACK sentinel (acted
on, chirp only — for commands that DO something rather than answer
something).  Vibe Control, an optional Windows-only mode that drives a
terminal session on this machine by voice, is built entirely out of ACK
commands; see VIBE_COMMANDS below and sdk/VIBE.md.

Two recognizers, not one.  The small model matches commands on every tap.
Pass a large model as well and DICTATION TRIGGERS become available —
`captain's log ...` always, plus `computer transcribe ...` while Vibe Control
holds a window.  A trigger switches the recognizer mid-utterance and captures
free-form speech until you stop talking.  See detect_trigger() and
handle_large_vocab_phase() below, and §11 of sdk/README.md.

TTS (text-to-speech) is handled automatically per platform:
    Linux / macOS  →  espeak-ng   (sudo apt install espeak-ng)
    Windows        →  PowerShell System.Speech (built-in, no install required)

Usage:
    python3 computer.py <small-model-dir> [large-model-dir]

    python3 computer.py ~/vosk-model-small-en-us-0.15
    python3 computer.py ~/vosk-model-small-en-us-0.15 ~/vosk-model-en-us-0.22

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
import datetime
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

# Vibe Control — drive a terminal session on THIS machine by voice (see
# sdk/VIBE.md).  Windows-only and entirely optional: vibekeys raises
# ImportError on other platforms, and this server runs anywhere Python and
# Vosk run.  A failed import is not an error — it just means the Vibe Control
# commands are omitted from the vocabulary and everything else is unaffected.
try:
    import vibekeys
except ImportError:
    vibekeys = None

SetLogLevel(-1)   # silence Vosk's verbose initialization chatter

PORT      = int(os.environ.get("SDK_SERVER_PORT", "1701"))
TIMEOUT_S = 10    # max wall-clock seconds to wait for speech before sending b'f'


# ---------------------------------------------------------------------------
# The large-vocabulary model — free-form dictation (§11).
#
# Two models, two jobs.  The SMALL model runs every tap: it is fast, loads in
# a couple of seconds, and is accurate enough to pick a known phrase out of a
# handful of candidates.  It is poor at open dictation, because a 3 MB
# vocabulary has to guess at words it does not really know.  The LARGE model
# (vosk-model-en-us-0.22, ~1.8 GB unpacked) transcribes arbitrary English
# well, but is far too heavy to be the everyday recognizer.
#
# So the server keeps both and SWITCHES between them: small for command
# matching, large for the span of one dictation, then back.  That is the whole
# mechanism behind `captain's log` and `computer transcribe`.
#
# LOADED EAGERLY AT STARTUP, NEVER ON DEMAND.  This is the single most
# important decision in the feature and it is not negotiable:
#
#   * Loading the large model takes tens of seconds and gigabytes of RAM.
#   * A dictation begins INSIDE a live badge transaction — the relay is
#     already recording, and listener.py gives up RECORD_MAX_S (13 s) after
#     the last keepalive.
#
# Loading lazily on first use would therefore blow the recording window every
# time, and the badge would fail the very command that triggered the load.
# Paying the cost once at startup is what makes the switch feel instant.
#
# OPTIONAL.  The path is the second positional argument.  Omit it and the
# server runs exactly as before, minus the two dictation commands — the same
# defensive shape as the vibekeys import above.  Nobody should have to
# download 1.8 GB to run the quick start.
#
# `large_model` is assigned once in main(), before the listening socket is
# opened, and only ever read afterwards — so no lock is needed despite the
# per-connection threads.
# ---------------------------------------------------------------------------

large_model = None

# Where `captain's log` entries are appended.  A plain text file beside this
# script, one timestamped line per entry.
CAPTAINSLOG_FILE = os.environ.get(
    "SDK_CAPTAINSLOG_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "captainslog.txt"))

# End of dictation: seconds without a NEW recognized word.  Two different
# values because the two features are spoken differently, both inherited from
# the full system's measured tuning rather than guessed:
#
#   A log entry is composed before you start talking and delivered in one go,
#   so a short gap means you are finished.  1.5 s (4 s and 1.0 s were both
#   tried there; 1.0 s clipped mid-thought on Vosk's partial latency).
#
#   A prompt is THOUGHT OUT WHILE SPEAKING.  You stop to consider the next
#   clause, and 1.5 s would paste half a sentence into the terminal.  6 s.
DICTATION_SILENCE_S = float(os.environ.get("SDK_DICTATION_SILENCE_S", "1.5"))
PROMPT_SILENCE_S    = float(os.environ.get("SDK_PROMPT_SILENCE_S", "6"))

# Hard cap on one dictation, whatever the silence gate is doing — the backstop
# for a room noisy enough to keep resetting it.
DICTATION_MAX_S     = float(os.environ.get("SDK_DICTATION_MAX_S", "110"))

# Keepalive cadence during dictation.  MANDATORY, not an optimization:
# listener.py stops recording RECORD_MAX_S (13 s) after the last byte from us,
# and a dictation routinely runs longer than that.  Each b'k' slides its
# deadline.  Same 4 s cadence the hail capture loop uses.
DICTATION_KEEPALIVE_S = 4


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


# ACK — return this from a command instead of a string to act WITHOUT
# speaking.  The badge plays its short "command executed" chirp (signal byte
# b'c') and nothing is synthesized.
#
# The distinction matters as soon as commands DO things rather than answer
# questions.  A spoken "Acknowledged." after every keystroke of Vibe Control
# would make the mode unusable — you would hear a sentence for every Enter.
# It is also the right answer for anything whose effect you can already see:
# lights, media, a script.
#
# None still means NO MATCH, so a distinct sentinel is required; the two are
# genuinely different outcomes (chirp vs. failure chirp).
ACK = object()


COMMANDS = {
    "computer hello":   "Hello.",
    "computer status":  "All systems nominal.",
    "computer time":    _time_phrase,   # e.g. "sixteen eleven hours."
    "computer goodbye": "Acknowledged.",
}


# ---------------------------------------------------------------------------
# VIBE_COMMANDS — the Vibe Control vocabulary (sdk/VIBE.md).
#
# AN OVERLAY, not a replacement: while Vibe Control is active these phrases
# are added to COMMANDS, and everything already there keeps working.  See
# active_commands() for the merge and for why this SDK diverges from the full
# system, which suspends its normal vocabulary outright while the mode is up.
#
# Hails are likewise not suppressed: they are matched before any command in
# handle_connection(), so an incoming call still reaches its badge whatever
# mode this desk is in.  A comms system that goes deaf in a submode is a
# broken comms system.
#
# Every entry returns ACK (chirp, no speech) except activation and
# deactivation, which speak — activation because its reply is DYNAMIC (it
# names the session it latched onto, or explains why it refused), and
# deactivation to confirm the release.
#
# NUMBER WORDS, NOT DIGITS.  Vosk's small model has no token for "1"; a phrase
# containing a bare digit becomes unmatchable by voice.  Hence "computer
# option one" ... "computer option nine".
#
# PHONETIC DISTANCE IS NOT FREE.  A small vocabulary reduces confusions but
# cannot invent acoustic distance that isn't there.  A "computer prompt"
# phrase in an earlier version of this vocabulary had to be renamed because it
# was misheard as "computer proceed" often enough to matter — both carry the
# same stressed "pro-" onset, and a constrained recognizer still has to pick
# one.  When adding a phrase here, check it against BOTH dicts — these phrases
# and COMMANDS are live simultaneously, so a collision with either one is a
# collision you will hear.
# ---------------------------------------------------------------------------

def _vibe(action, *args):
    """Run a vibekeys action and ACK.  ACK regardless of outcome: the failure
    detail is on the server console, and the badge chirp only reports that the
    command was heard and dispatched.  A refusal to inject is not a
    recognition failure, and reporting it as one would be misleading."""
    action(*args)
    return ACK


VIBE_COMMANDS = {} if vibekeys is None else {
    "computer deactivate vibe control": lambda: vibekeys.deactivate(),

    # Enter.
    "computer proceed":   lambda: _vibe(vibekeys.press_enter),

    # Right Arrow then Enter: accept the suggested next action and submit it.
    "computer continue":  lambda: _vibe(vibekeys.press_continue),
    "computer carry on":  lambda: _vibe(vibekeys.press_continue),

    # Escape, sent TWICE — a single press does not clear Claude Code's box.
    "computer cancel":    lambda: _vibe(vibekeys.press_escape),

    # Screensaver / blanked-display recovery.  Also in COMMANDS below, so a
    # blank screen is recoverable whether or not the mode is up — the one
    # phrase deliberately present in both vocabularies.
    "computer wake up":   lambda: _vibe(vibekeys.wake),

    # Explicit selection.  Nine is the ceiling; Claude Code offers 3-4.
    "computer option one":   lambda: _vibe(vibekeys.press_digit, 1),
    "computer option two":   lambda: _vibe(vibekeys.press_digit, 2),
    "computer option three": lambda: _vibe(vibekeys.press_digit, 3),
    "computer option four":  lambda: _vibe(vibekeys.press_digit, 4),
    "computer option five":  lambda: _vibe(vibekeys.press_digit, 5),
    "computer option six":   lambda: _vibe(vibekeys.press_digit, 6),
    "computer option seven": lambda: _vibe(vibekeys.press_digit, 7),
    "computer option eight": lambda: _vibe(vibekeys.press_digit, 8),
    "computer option nine":  lambda: _vibe(vibekeys.press_digit, 9),
}

# Playback of the last captain's log entry.  An ORDINARY command, not a
# trigger — a fixed phrase with a spoken answer, which is exactly what
# COMMANDS is for; only the recording half needs the large model.  Registered
# in main() alongside the dictation feature all the same, so the log commands
# appear and disappear together rather than offering playback of entries this
# server has no way to record.
REPLAY_LOG_PHRASE = "computer replay last log entry"

# The entry point is a named constant because it is the one Vibe Control
# phrase that does NOT live in VIBE_COMMANDS, and it therefore has to be
# mentioned separately wherever the mode is listed.  A literal repeated in
# those places would be free to drift from the key that actually dispatches.
VIBE_ACTIVATE = "computer activate vibe control"

if vibekeys is not None:
    # Entry point and the wake exemption live in the NORMAL vocabulary.
    # Activation speaks a dynamic phrase — the session it latched onto, or the
    # reason it refused — so activation always produces a spoken outcome.
    COMMANDS[VIBE_ACTIVATE] = lambda: vibekeys.activate()
    COMMANDS["computer wake up"] = lambda: _vibe(vibekeys.wake)


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


def active_commands():
    """The vocabulary currently in force.

    Vibe Control ADDS its phrases to the normal vocabulary rather than
    replacing it: while the mode is latched everything in COMMANDS still
    works, and VIBE_COMMANDS is available on top.

    THIS IS A DELIBERATE DIVERGENCE from the full system, which suspends its
    normal vocabulary entirely while the mode is up.  That gate is worth
    having *there* — that system has a large vocabulary, and narrowing it to
    ~15 phrases both sharpens recognition and stops a stray command firing
    while you are concentrating on a terminal.  Neither argument survives the
    trip here: this vocabulary is a handful of phrases to begin with, so there
    is little to sharpen and little to fire by accident, and suspending it
    only means you cannot ask the time without dropping the latch.
    (Captain's call, 2026-08-01.)

    VIBE_COMMANDS is merged FIRST so its phrases win on a tie — while the mode
    is latched, the mode's meaning of a phrase is the operative one.  Rebuilt
    per call rather than cached because main() adds entries to COMMANDS at
    startup; a snapshot taken at import time would miss them.  It is a
    ~25-entry dict, so this is not a cost worth engineering around.
    """
    if vibekeys is not None and vibekeys.is_active():
        return {**VIBE_COMMANDS, **COMMANDS}
    return COMMANDS


def match_command(text):
    """Return the response for the first matching phrase in `text`, or None.

    Iterates the active vocabulary in insertion order (Python 3.7+ dict
    guarantee) and returns the first match.  Calls the value if it's callable
    (lambda), otherwise returns it directly as a string.

    Three distinct outcomes, and they must not be conflated:
        None      no phrase matched      → caller keeps listening / sends b'f'
        ACK       matched, acted, silent → caller sends b'c'
        a string  matched, speak this    → caller synthesizes and sends b'v'
    """
    for phrase, response in active_commands().items():
        if phrase in text:
            return response() if callable(response) else response
    return None


def respond(conn, response, mac):
    """Deliver a matched command's outcome to the badge.

    ACK sends the bare b'c' signal — the badge plays its short "command
    executed" chirp and nothing is synthesized.  Anything else is spoken.
    Factored out because both the real-time match and the final-flush match
    need identical handling, and letting them drift would mean a command
    behaved differently depending on how fast it was recognized.
    """
    if response is ACK:
        conn.sendall(b"c")
    else:
        send_voice(conn, response, mac)


# ---------------------------------------------------------------------------
# Large-vocabulary triggers — the dictation vocabulary (§11).
#
# A TRIGGER IS NOT A COMMAND, and the difference is why these cannot live in
# COMMANDS.  A command is a fixed phrase that maps to a fixed outcome; the
# whole utterance is the command.  A trigger is a PREFIX followed by open
# speech that nobody can enumerate in advance — "captain's log, stardate
# forty-seven six three four, the away team has returned".  Matching it does
# not end the transaction; it CHANGES THE RECOGNIZER and keeps listening.
#
# So triggers are checked ahead of match_command() in the recognition loop,
# and their handler takes over the connection rather than returning a string.
#
# AVAILABILITY.  `captain's log` is live in both states.  `computer
# transcribe` needs Vibe Control latched, because it pastes into the latched
# window — with no latch there is nowhere for the text to go.
#
# That is a REAL DEPENDENCY, and it is the only reason either trigger is ever
# unavailable.  An earlier version also suspended `captain's log` during Vibe
# Control, mirroring the full system, where activating the mode replaces the
# whole vocabulary.  That gating was removed: this SDK merges the vocabularies
# instead (see active_commands()), and a log entry writes a file and touches
# no window, so nothing about driving a terminal makes it unsafe to reach.
# ---------------------------------------------------------------------------

# Vosk's small model renders the possessive inconsistently depending on how
# clearly the "s" lands, and unlike the full system this server runs the small
# model UNCONSTRAINED (no grammar), so we cannot force one spelling.  Accept
# both rather than lose a log entry to an apostrophe.
CAPTAINSLOG_PHRASES = ("captain's log", "captains log")
TRANSCRIBE_PHRASE   = "computer transcribe"


def detect_trigger(text):
    """Return (trigger_type, matched_phrase) for a dictation trigger in `text`,
    or None.

    Substring containment, not startswith.  The full system anchors these to
    the start of the utterance because its recognizer is grammar-constrained
    and its text is therefore clean.  This server runs the small model open,
    so a stray decoded syllable ahead of the trigger is ordinary — anchoring
    would drop real commands.  Consistent with match_command(), which is
    substring-matched for the same reason.
    """
    if large_model is None:
        return None

    # Transcribe is checked first and is available ONLY while the mode is
    # latched — it pastes into the latched window, so with no latch there is
    # nowhere for the text to go.  That is a genuine dependency, unlike the
    # suspension of captain's log, which was only ever a vocabulary rule.
    if vibekeys is not None and vibekeys.is_active() and TRANSCRIBE_PHRASE in text:
        return ("transcribe", TRANSCRIBE_PHRASE)

    # Captain's log is available in BOTH states — see active_commands() for
    # why this SDK does not suspend the normal vocabulary during Vibe Control.
    # It writes a file and touches no window, so nothing about driving a
    # terminal makes it unsafe to reach.
    for phrase in CAPTAINSLOG_PHRASES:
        if phrase in text:
            return ("captains_log", phrase)
    return None


def strip_trigger(text, phrases):
    """Remove the trigger phrase and everything before it from `text`.

    Located with find() rather than sliced at a known offset because the LARGE
    model re-transcribes the audio from the beginning and may render the
    trigger differently than the small model matched it — a different
    possessive, a swallowed word.

    ALL spellings are tried, not just the one that matched.  The two models
    disagree independently: the small model can match "captains log" on audio
    the large model renders "captain's log", and searching only for what
    matched would then leave the trigger sitting in the entry.  Earliest hit
    wins, so a trigger word that also occurs later in the dictation cannot
    truncate it.

    If none is found the whole transcript is returned rather than nothing — a
    log entry with a stray leading word beats one that silently lost its first
    sentence.
    """
    if isinstance(phrases, str):
        phrases = (phrases,)
    best_idx, best_len = -1, 0
    for p in phrases:
        idx = text.find(p)
        if idx >= 0 and (best_idx < 0 or idx < best_idx):
            best_idx, best_len = idx, len(p)
    if best_idx < 0:
        return text.strip()
    return text[best_idx + best_len:].strip()


def handle_large_vocab_phase(conn, mac, pcm, trigger_type, phrase):
    """Switch to the large model and capture free-form speech to completion.

    The mode switch itself is three lines — a fresh KaldiRecognizer on the
    large model, then replay every buffered chunk into it.  THE REPLAY IS THE
    POINT: the trigger is only recognized partway through the utterance, so
    the audio that carried the trigger (and often the first words after it)
    has already been consumed by the small recognizer.  Feeding the buffer
    back means the large model transcribes the utterance from the tap, and
    nothing spoken before the switch is lost.

    Capture then continues on the live socket until the silence gate closes
    it, exactly like the hail capture loop above.
    """
    print(f"[computer] [{mac}] large-vocab trigger: {trigger_type}")
    # Opens the live-transcription line written inside the capture loop.  No
    # newline: the words stream onto the end of this prefix as they arrive.
    sys.stdout.write(f"[computer] [{mac}] > ")
    sys.stdout.flush()

    rec = KaldiRecognizer(large_model, 16000)
    for chunk in pcm:
        rec.AcceptWaveform(chunk)

    silence_s = PROMPT_SILENCE_S if trigger_type == "transcribe" else DICTATION_SILENCE_S

    conn.settimeout(0.3)
    start          = time.time()
    last_partial   = ""
    last_progress  = time.time()
    last_keepalive = time.time()

    # The live-transcription line stays open across the whole loop, so every
    # exit has to close it BEFORE printing anything else — otherwise the
    # reason for finishing gets spliced onto the end of the dictation and
    # reads as if it were spoken.  Idempotent, so the exits do not have to
    # care whether some earlier path already closed it.
    line_open = True

    def close_line():
        nonlocal line_open
        if line_open:
            sys.stdout.write("\n")
            sys.stdout.flush()
            line_open = False

    while True:
        # Silence finalize, GATED ON HAVING HEARD SOMETHING.  Until the first
        # word is recognized the timer is held open, so a pause between the
        # tap and the first syllable never ends the capture — only a gap
        # BETWEEN words does.  Continuous speech with sub-second gaps runs as
        # long as you like, up to the hard cap.
        if last_partial and time.time() - last_progress >= silence_s:
            close_line()
            print(f"[computer] [{mac}] silence {silence_s}s — finalizing")
            break
        if time.time() - start >= DICTATION_MAX_S:
            close_line()
            print(f"[computer] [{mac}] dictation cap {DICTATION_MAX_S}s reached")
            break
        if time.time() - last_keepalive >= DICTATION_KEEPALIVE_S:
            try:
                conn.sendall(b"k")   # slide listener.py's recording deadline
            except OSError:
                break                # relay gone — finalize what we have
            last_keepalive = time.time()

        try:
            data = conn.recv(4096)
        except socket.timeout:
            continue                 # no audio this beat; recheck the timers
        except OSError:
            break
        if not data:
            break                    # stream ended

        rec.AcceptWaveform(data)
        partial = json.loads(rec.PartialResult()).get("partial", "").strip()
        if partial and partial != last_partial:
            # Live transcription to the console, word by word as it lands.
            # This server has no display of its own, so the console IS the
            # readout — you watch the words arrive and know the dictation is
            # working long before the final text exists.
            #
            # Vosk REVISES its hypothesis: a partial does not always extend
            # the previous one, it can rewrite words already emitted.  So the
            # cheap `partial[len(last_partial):]` delta is only valid when the
            # new hypothesis still starts with the old one.  When it does not,
            # break the line and reprint the corrected hypothesis whole —
            # otherwise a revision splices a fragment mid-word and the console
            # shows text that was never spoken.
            if partial.startswith(last_partial):
                sys.stdout.write(partial[len(last_partial):])
            else:
                sys.stdout.write("\n           " + partial)
            sys.stdout.flush()
            last_partial  = partial
            last_progress = time.time()

    # Close it for the exits that did not (EOF, socket error) — including the
    # case where nothing was ever recognized, or the dangling "> " prefix
    # would swallow the next message.
    close_line()

    text = json.loads(rec.FinalResult()).get("text", "").strip()
    # Strip against every spelling of THIS trigger, not just the one the small
    # model happened to match — see strip_trigger().
    body = strip_trigger(text, CAPTAINSLOG_PHRASES
                         if trigger_type == "captains_log" else TRANSCRIBE_PHRASE)

    if trigger_type == "transcribe":
        finish_transcribe(conn, mac, body)
    else:
        finish_captains_log(conn, mac, text, body)


def finish_transcribe(conn, mac, body):
    """Paste the dictated prompt into the latched terminal window.

    ENTER IS NOT PRESSED.  The prompt lands in the composer and sits there
    until you read it and say "computer proceed".  That separation is the
    entire safety argument for letting a voice pipeline type into a terminal:
    recognition WILL occasionally mishear, and the review step is what makes
    that a nuisance instead of an incident.  Do not add a submit here.
    """
    if not body:
        print(f"[computer] [{mac}] transcribe: nothing recognized")
        send_voice(conn, "No prompt recognized.", mac)
        return
    print(f"[computer] [{mac}] transcribe: {len(body.split())} words -> paste")
    vibekeys.paste_text(body)
    # ACK, not speech: the words are already on screen in the composer, which
    # is a better confirmation than any sentence we could synthesize.
    conn.sendall(b"c")


_MONTHS = ("January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December")


def replay_last_log():
    """Speak the most recent captain's log entry back, with its date and time.

    A stored line is `[2026-08-01 14:30:00] captain's log, the away team...`.
    Reading that aloud verbatim would say the timestamp as digits and
    punctuation, so the stamp is re-rendered as spoken English and the entry
    follows it.

    The stored entry KEEPS its trigger phrase (see finish_captains_log), and
    this announcement already opens with "Captain's log" — so a leading
    "captain's log" is stripped from the body before speaking, or every replay
    would say it twice.  The full system's shell version has that stutter;
    there is no reason to port a wart.
    """
    try:
        with open(CAPTAINSLOG_FILE, "r", encoding="utf-8") as f:
            lines = [ln.strip() for ln in f if ln.strip()]
    except OSError:
        lines = []
    if not lines:
        return "There are no log entries on file."

    m = re.match(r"^\[(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):\d{2}\]\s*(.*)$",
                 lines[-1])
    if not m:
        return lines[-1]   # unparseable stamp — speak the line as it stands
    year, mon, day, hour, minute, entry = m.groups()

    for phrase in CAPTAINSLOG_PHRASES:
        if entry.lower().startswith(phrase):
            entry = entry[len(phrase):].lstrip(" ,.")
            break

    hour, minute, day = int(hour), int(minute), int(day)
    if minute == 0:
        spoken_time = f"{hour} hundred hours"
    elif minute < 10:
        spoken_time = f"{hour} oh {minute}"
    else:
        spoken_time = f"{hour} {minute}"
    return (f"Captain's log, {_MONTHS[int(mon) - 1]} {day}, {year}, "
            f"{spoken_time}. {entry}")


def finish_captains_log(conn, mac, text, body):
    """Append one timestamped entry to CAPTAINSLOG_FILE.

    The trigger phrase is kept in the stored entry — a log that opens
    "captain's log, stardate..." reads the way it should, and the phrase is
    part of the dictation rather than a command that preceded it.  `body` is
    used only to decide whether anything was actually said after the trigger.

    The full system signals this with its own byte (b'l', which its relay
    answers with a spoken "Log recorded."); this SDK's listener.py knows only
    c/f/v/O, so the confirmation is an ordinary voice response.  One less
    moving part, and no listener change to run the feature.
    """
    if not body:
        print(f"[computer] [{mac}] captain's log: no content after trigger")
        send_voice(conn, "No log content recognized.", mac)
        return
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(CAPTAINSLOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{stamp}] {text}\n")
    except OSError as e:
        print(f"[computer] [{mac}] captain's log write failed: {e}")
        send_voice(conn, "Unable to record log.", mac)
        return
    # The full system suppresses log content on its console — it has a
    # teleprompter to render dictation on, so the console can stay discreet.
    # This server has no second display, so suppressing here would mean
    # dictating blind; the capture loop streams the words live instead
    # (Captain's call, 2026-08-01).  Worth knowing if you run this server
    # somewhere other people can see the terminal: your log is on that screen.
    print(f"[computer] [{mac}] captain's log recorded ({len(text.split())} words)")
    send_voice(conn, "Log recorded.", mac)


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
                # Dictation triggers next, BEFORE match_command: a trigger is
                # a prefix with open speech behind it, so waiting for the
                # utterance to finish would mean transcribing it with the
                # small model — the one thing the mode switch exists to
                # avoid.  Firing mid-utterance is what makes the replay in
                # handle_large_vocab_phase necessary, and sufficient.
                trig = detect_trigger(accumulated)
                if trig:
                    handle_large_vocab_phase(conn, mac, pcm, trig[0], trig[1])
                    return
                response = match_command(accumulated)
                if response is not None:
                    print(f"[computer] [{mac}] match on {accumulated!r} -> {response!r}")
                    respond(conn, response, mac)
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
            # Late trigger catch — a short dictation spoken fast enough that
            # the stream ended before the real-time loop saw the trigger.  The
            # replay still works; the capture loop just finds the socket at
            # EOF and finalizes immediately on the buffered audio alone.
            trig = detect_trigger(final)
            if trig:
                handle_large_vocab_phase(conn, mac, pcm, trig[0], trig[1])
                return
            response = match_command(final)
            if response is not None:
                respond(conn, response, mac)
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
    global large_model

    # Force UTF-8 on the console.  On Windows, Python picks the ANSI code page
    # (cp1252) whenever stdout is a pipe or a file rather than a terminal, and
    # cp1252 cannot encode the arrows and dashes in the banner below — so
    # `python computer.py MODEL > server.log` died on startup with
    # UnicodeEncodeError before it ever bound the socket.  Live transcription
    # makes this sharper still: it prints whatever the recognizer produced,
    # which is not ours to constrain.  errors="replace" means an unencodable
    # character degrades to "?" instead of killing a dictation in progress.
    #
    # line_buffering forces a flush per line.  Piping to a log file otherwise
    # switches stdout to block buffering, and a long-running server's output
    # then appears in 8 KB lurches — or is lost entirely if it is killed
    # before the buffer fills, which is exactly how a server usually ends.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace",
                               line_buffering=True)
    except (AttributeError, OSError):
        pass   # non-standard stdout (embedded, captured) — leave it alone

    if len(sys.argv) < 2:
        sys.exit("Usage: computer.py <small-model-dir> [large-model-dir]\n"
                 "  small: vosk-model-small-en-us-0.15  (required — command matching)\n"
                 "  large: vosk-model-en-us-0.22        (optional — enables dictation)")
    model_path = sys.argv[1]
    if not os.path.isdir(model_path):
        sys.exit(f"Model dir not found: {model_path}")
    large_path = sys.argv[2] if len(sys.argv) > 2 else None
    # Fail NOW if the path is wrong, not on the first dictation twenty minutes
    # from now.  A mistyped model path is a startup error.
    if large_path and not os.path.isdir(large_path):
        sys.exit(f"Large model dir not found: {large_path}")

    print(f"[computer] loading Vosk model {model_path}...")
    model = Model(model_path)   # takes ~1–3 s; model is reused for all connections
    if large_path:
        # Tens of seconds and gigabytes.  Announced because the server is
        # unresponsive while it happens and silence here looks like a hang.
        print(f"[computer] loading LARGE Vosk model {large_path} (slow, one time)...")
        large_model = Model(large_path)
        # Registered BEFORE the commands banner below, so it appears in it.
        # Playback needs no large model itself, but pairing it with the
        # recording half keeps the log commands appearing and disappearing
        # together.
        COMMANDS[REPLAY_LOG_PHRASE] = replay_last_log
    print(f"[computer] listening on 0.0.0.0:{PORT}")
    print(f"[computer] commands: {' | '.join(COMMANDS)}")
    print(f"[computer] tap badge → speak one of the above → voice response plays through badge")
    print(f"[computer] console: badges | hail <mac> [text]")
    if large_model is None:
        print(f"[computer] dictation: OFF (pass a large model dir as the 2nd argument)")
    else:
        print(f"[computer] dictation: \"{CAPTAINSLOG_PHRASES[0]} ...\" -> {CAPTAINSLOG_FILE}")
        print(f"[computer]   playback: \"{REPLAY_LOG_PHRASE}\"")
        if vibekeys is not None:
            # The one phrase whose availability really is conditional: it
            # pastes into the latched window, so it needs a latch.
            print(f"[computer]   \"{TRANSCRIBE_PHRASE} ...\" -> latched window "
                  f"(vibe control only)")
    if vibekeys is not None:
        # The ACTIVATION phrase leads this block even though it belongs to
        # COMMANDS and has therefore already been printed above.  This is the
        # line you read when you want the mode, and a listing of a mode's
        # vocabulary that omits the way IN to it sends you hunting through the
        # general command list for it.  Repeating one phrase is cheaper.
        print(f"[computer] vibe control available: {VIBE_ACTIVATE}")
        print(f"[computer]   adds: {' | '.join(VIBE_COMMANDS)} (see sdk/VIBE.md)")
        print(f"[computer]   the commands above stay live while it is latched")

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
