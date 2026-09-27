#!/usr/bin/bash
#
# tts.sh — write a WAV file from text using the SDK's own TTS engine.
#
# This mirrors sdk/computer.py::synth_wav() so generated assets sound identical
# to the voice the SDK speaks at runtime, first available:
#     Piper            →  Lessac low (../.piper/ or ~/.piper/, or
#                         $SDK_PIPER_MODEL), when piper-tts is installed
#     Windows / MSYS2  →  SAPI via PowerShell (System.Speech, female voice,
#                         Rate 2) — ships with .NET, no install needed.
#     Linux / macOS    →  espeak-ng -v en-us+f3 -s 165 (female variant)
#
# Unlike speak/speak.sh, this does NOT play the audio — it only writes the WAV,
# so you can regenerate the sdk/assets/*.wav phrases with a neutral TTS voice.
#
# Usage:
#   ./tts.sh "phrase"                 # -> ./out.wav (beside this script)
#   ./tts.sh "phrase" name.wav        # -> ./name.wav (beside this script)
#   ./tts.sh "phrase" assets/foo.wav  # -> path with a slash is used as given
#
# Examples (regenerating the shipped asset phrases):
#   ./tts.sh "Access granted."               assets/access_granted.wav
#   ./tts.sh "Main computer online."         assets/maincomputeronline.wav
#   ./tts.sh "Main computer offline."        assets/maincomputeroffline.wav
#   ./tts.sh "Badge to comms relay online."  assets/badge-to-comms-relay-online.wav
#   ./tts.sh "Command executed."             assets/commandexecuted.wav
#   ./tts.sh "Command failure."              assets/commandfailure.wav
#   ./tts.sh "Channel open."                 assets/channelopen.wav
#   ./tts.sh "Channel closed."               assets/channelclosed.wav
#   ./tts.sh "Cancelled."                    assets/cancelled.wav
#   ./tts.sh "Listening."                    assets/listening.wav
#
# ⚠ EVERY ASSET IN THIS LIST IS SPEECH, listening.wav included -- it says the
# word "Listening", it is not a tone. TOS ships a file of the same name that
# IS a tone, and on 2026-09-13 that difference was ported across by filename:
# the SDK's end-of-cycle cue replayed listening.wav and told the Captain the
# badge was listening when it was not. listening.wav was the one asset missing
# from this list, which is how its meaning went unrecorded. Keep the list
# complete.
#
# ⚠ AND listening.wav AS SHIPPED IS TAIL-TRIMMED: 1189 ms -> 671 ms on
# 2026-09-06, to cut dead air before capture begins. Regenerating it here
# gives the untrimmed file back. The HEAD is what PRIME_MS_LISTENING = 160 is
# sized against, so trim only the tail if you re-cut it.

set -euo pipefail

PHRASE="${1:-Computer voice is operational.}"
OUT="${2:-out.wav}"

# Resolve script directory.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# If OUT has no directory component, write it beside this script (sdk/).
case "$OUT" in
    */*) OUT_PATH="$OUT" ;;
    *)   OUT_PATH="$SCRIPT_DIR/$OUT" ;;
esac

# Piper first, as computer.py: the named voice, else Lessac low in either place.
PIPER_MODEL="${SDK_PIPER_MODEL:-}"
if [[ -z "$PIPER_MODEL" ]]; then
    for d in "$SCRIPT_DIR/../.piper" "$HOME/.piper"; do
        if [[ -f "$d/en_US-lessac-low.onnx" ]]; then
            PIPER_MODEL="$d/en_US-lessac-low.onnx"
            break
        fi
    done
fi
PY=python
command -v python >/dev/null 2>&1 || PY=python3

if [[ -n "$PIPER_MODEL" ]] && "$PY" -c "import piper" 2>/dev/null; then
    MODEL_ARG="$PIPER_MODEL"
    OUT_ARG="$OUT_PATH"
    if command -v cygpath >/dev/null 2>&1; then
        MODEL_ARG="$(cygpath -m "$PIPER_MODEL")"
        OUT_ARG="$(cygpath -m "$OUT_PATH")"
    fi
    printf '%s' "$PHRASE" | "$PY" -m piper -m "$MODEL_ARG" -f "$OUT_ARG"
elif [[ "$OSTYPE" == "linux-gnu"* || "$OSTYPE" == "darwin"* ]]; then
    # Linux / macOS: espeak-ng, same flags the SDK uses.
    espeak-ng -v en-us+f3 -s 165 -w "$OUT_PATH" "$PHRASE"
else
    # Windows / MSYS2: SAPI via PowerShell needs a native Windows path.
    WIN_PATH="$(cygpath -w "$OUT_PATH")"
    # Escape single quotes for the PowerShell single-quoted string literal.
    SAFE="${PHRASE//\'/\'\'}"
    powershell -NoProfile -Command "
        Add-Type -AssemblyName System.Speech;
        \$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;
        \$s.SelectVoiceByHints('Female');
        \$s.Rate = 2;
        \$s.SetOutputToWaveFile('$WIN_PATH');
        \$s.Speak('$SAFE');
        \$s.Dispose()"
fi

if [[ ! -f "$OUT_PATH" ]]; then
    echo "ERROR: TTS reported success but $OUT_PATH was not created" >&2
    exit 1
fi

echo "Wrote $OUT_PATH ($(wc -c < "$OUT_PATH") bytes)"
