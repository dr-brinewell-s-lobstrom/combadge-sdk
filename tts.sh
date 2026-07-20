#!/usr/bin/bash
#
# tts.sh — write a WAV file from text using the SDK's own TTS engine.
#
# This mirrors sdk/computer.py::synth_wav() so generated assets sound identical
# to the voice the SDK speaks at runtime:
#     Windows / MSYS2  →  SAPI via PowerShell (System.Speech, female voice,
#                         Rate 2) — ships with .NET, no install needed.
#     Linux / macOS    →  espeak-ng -v en-us -s 165
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
#   ./tts.sh "Badge to comms relay online."  assets/badge-to-comms-relay-online.wav
#   ./tts.sh "Command executed."             assets/commandexecuted.wav
#   ./tts.sh "Command failure."              assets/commandfailure.wav

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

if [[ "$OSTYPE" == "linux-gnu"* || "$OSTYPE" == "darwin"* ]]; then
    # Linux / macOS: espeak-ng, same flags the SDK uses.
    espeak-ng -v en-us -s 165 -w "$OUT_PATH" "$PHRASE"
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
