#!/usr/bin/env bash
#
# install-dependencies.sh -- install what the SDK needs to run. Nothing else.
#
# This is not a setup script, because the SDK has no setup step: the
# transceiver pairs badges itself, the server names them by voice, and
# everything configures itself on first use (README §0). What it cannot do
# is run without its dependencies, and those are all this installs:
#
#   --transceiver   (Linux: the host the badges pair to)
#       apt: bluez pipewire pipewire-pulse wireplumber libspa-0.2-bluetooth
#            pulseaudio-utils ffmpeg python3 python3-evdev
#   --server        (Linux or Windows: the host running computer.py)
#       apt (Linux): python3 python3-pip espeak-ng curl
#       pip:         vosk piper-tts
#       models:      Vosk small (commands, ~40 MB) and Vosk large (dictation:
#                    captain's log, 1.8 GB, ~5 GB of RAM in use) in ../.vosk/;
#                    Piper voice Lessac low (~63 MB) in ../.piper/
#
# Default: both roles on Linux (a transceiver host usually runs the server
# too), the server on Windows (Git Bash / MSYS2). It checks first, shows what
# is missing, and asks before installing anything; only what is missing is
# installed, so running it again is harmless. It does not pair badges, edit
# configuration or start anything.
#
# Usage:  ./install-dependencies.sh [--transceiver] [--server] [--check] [--yes]
#   --check   report only, install nothing
#   --yes     do not ask
#
# On Debian 12+ / Ubuntu 23.04+ Python is "externally managed" and a plain
# `pip install` is refused. The Python packages Debian ships (evdev) come
# from apt; vosk and piper-tts are not packaged, so they go in with
# `sudo pip install --break-system-packages` (into /usr/local, as on the
# SDK's own test host). The flag is added only where the system asks for it.

set -u

SDK_DIR="$(cd "$(dirname "$0")" && pwd)"
TOP_DIR="$(cd "$SDK_DIR/.." && pwd)"
VOSK_DIR="$TOP_DIR/.vosk"
PIPER_DIR="$TOP_DIR/.piper"

VOSK_SMALL="vosk-model-small-en-us-0.15"
VOSK_LARGE="vosk-model-en-us-0.22"
VOSK_URL="https://alphacephei.com/vosk/models"
PIPER_VOICE="en_US-lessac-low"
PIPER_URL="https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/low"

TRANSCEIVER_APT="bluez pipewire pipewire-pulse wireplumber libspa-0.2-bluetooth pulseaudio-utils ffmpeg python3 python3-evdev"
SERVER_APT="python3 python3-pip espeak-ng curl"
SERVER_PIP="vosk piper-tts"

want_transceiver=0; want_server=0; check_only=0; assume_yes=0
for arg in "$@"; do
    case "$arg" in
        --transceiver) want_transceiver=1 ;;
        --server)      want_server=1 ;;
        --check)       check_only=1 ;;
        --yes|-y)      assume_yes=1 ;;
        -h|--help)     sed -n '2,/^set -u/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "Unknown option: $arg (try --help)" >&2; exit 2 ;;
    esac
done

case "$(uname -s)" in
    Linux*)               os=linux ;;
    MINGW*|MSYS*|CYGWIN*) os=windows ;;
    *) echo "Unsupported OS: $(uname -s). Linux (Debian/Ubuntu) or Windows (Git Bash / MSYS2)." >&2; exit 2 ;;
esac

if [[ $want_transceiver -eq 0 && $want_server -eq 0 ]]; then
    want_server=1
    [[ $os == linux ]] && want_transceiver=1
fi
if [[ $os == windows && $want_transceiver -eq 1 ]]; then
    echo "The transceiver runs on Linux only (BlueZ, PipeWire, evdev)." >&2
    exit 2
fi
if [[ $os == linux ]] && ! command -v apt-get >/dev/null 2>&1; then
    echo "No apt-get: this script knows Debian/Ubuntu. On another distro, install the" >&2
    echo "packages listed at the top of this file with your package manager." >&2
    exit 2
fi

SUDO=""
if [[ $os == linux && $(id -u) -ne 0 ]]; then
    SUDO="sudo"
fi

PY=python3
if [[ $os == windows ]]; then
    PY=python
    command -v python >/dev/null 2>&1 || PY=python3
fi

next_steps() {
    echo
    echo "Next:"
    if [[ $want_server -eq 1 && $want_transceiver -eq 1 ]]; then
        echo "  1. Start the server:       ./computer.sh"
        echo "  2. Start the transceiver:  ./transceiver.sh     (in a second terminal; asks for sudo)"
        echo "  3. Power a badge on nearby. The transceiver pairs it by itself, and the"
        echo "     server asks you, on the badge, to name it. Tap to answer."
    elif [[ $want_server -eq 1 ]]; then
        echo "  1. Start the server:  ./computer.sh"
        echo "  2. On the transceiver host (Linux), point it at this machine:"
        echo "       SDK_SERVER_HOST=<this machine's address> ./transceiver.sh"
        [[ $os == windows ]] && echo "     Allow Python through the Windows firewall (TCP 1701) when asked."
        echo "  3. Power a badge on near the transceiver host. It is paired by itself, and"
        echo "     the server asks you, on the badge, to name it. Tap to answer."
    else
        echo "  1. Start the server (./computer.sh) on its machine."
        echo "  2. Start the transceiver:  SDK_SERVER_HOST=<server address> ./transceiver.sh"
        echo "     (just ./transceiver.sh if the server runs on this machine)"
        echo "  3. Power a badge on nearby. It is paired by itself, and the server asks"
        echo "     you, on the badge, to name it. Tap to answer."
    fi
    echo "  README §0 (Quick Start) has the details."
}

# --- check ------------------------------------------------------------------

missing_apt=(); missing_pip=(); missing_models=()

apt_has() { dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q "install ok installed"; }
pip_has() { "$PY" -c "import $1" >/dev/null 2>&1; }

say()  { printf '  %-34s %s\n' "$1" "$2"; }

echo "SDK dependencies on $(uname -n) ($os), roles:" \
     "$([[ $want_transceiver -eq 1 ]] && echo -n 'transceiver ')$([[ $want_server -eq 1 ]] && echo -n 'server')"

if [[ $os == linux ]]; then
    pkgs=""
    [[ $want_transceiver -eq 1 ]] && pkgs="$pkgs $TRANSCEIVER_APT"
    [[ $want_server -eq 1 ]] && pkgs="$pkgs $SERVER_APT"
    for p in $(echo $pkgs | tr ' ' '\n' | awk '!seen[$0]++'); do
        if apt_has "$p"; then say "apt  $p" "ok"; else say "apt  $p" "MISSING"; missing_apt+=("$p"); fi
    done
fi

if [[ $want_server -eq 1 ]]; then
    if ! command -v "$PY" >/dev/null 2>&1; then
        say "python" "MISSING"
        if [[ $os == windows ]]; then
            echo "Install Python 3 from python.org first (tick \"Add python.exe to PATH\"), then run this again." >&2
            exit 1
        fi
    fi
    for m in $SERVER_PIP; do
        mod="${m//-tts/}"          # piper-tts imports as piper
        if pip_has "$mod"; then say "pip  $m" "ok"; else say "pip  $m" "MISSING"; missing_pip+=("$m"); fi
    done
    for d in "$VOSK_SMALL" "$VOSK_LARGE"; do
        if [[ -d "$VOSK_DIR/$d" ]]; then say "model $d" "ok"; else say "model $d" "MISSING"; missing_models+=("vosk:$d"); fi
    done
    if [[ -f "$PIPER_DIR/$PIPER_VOICE.onnx" && -f "$PIPER_DIR/$PIPER_VOICE.onnx.json" ]]; then
        say "voice $PIPER_VOICE" "ok"
    else
        say "voice $PIPER_VOICE" "MISSING"; missing_models+=("piper:$PIPER_VOICE")
    fi
fi

# A host with no desktop session needs WirePlumber told not to wait for one,
# or no badge ever gets an audio profile (README §3). Configuration, not a
# dependency: reported, not done.
if [[ $os == linux && $want_transceiver -eq 1 ]]; then
    home="$HOME"
    [[ -n "${SUDO_USER:-}" ]] && home="$(getent passwd "$SUDO_USER" | cut -d: -f6)"
    if [[ ! -f "$home/.config/wireplumber/wireplumber.conf.d/51-bluez-no-seat.conf" ]]; then
        echo
        echo "  note: if this host runs with no desktop session (headless, SSH only),"
        echo "        apply the WirePlumber seat fix in README §3 - it is configuration,"
        echo "        not a dependency, so this script leaves it to you."
    fi
fi

total=$(( ${#missing_apt[@]} + ${#missing_pip[@]} + ${#missing_models[@]} ))
echo
if [[ $total -eq 0 ]]; then
    echo "Everything is installed."
    [[ $check_only -eq 0 ]] && next_steps
    exit 0
fi
echo "Missing: $total item(s)."
[[ ${#missing_models[@]} -gt 0 ]] && echo "The models are a download of up to ~2 GB (the large Vosk model is 1.8 GB)."
if [[ $check_only -eq 1 ]]; then
    exit 1
fi
if [[ $assume_yes -eq 0 ]]; then
    read -r -p "Install them now? [y/N] " ans
    [[ "$ans" == [yY]* ]] || { echo "Nothing installed."; exit 1; }
fi

# --- install ----------------------------------------------------------------

fail=0

if [[ ${#missing_apt[@]} -gt 0 ]]; then
    echo "== apt: ${missing_apt[*]}"
    $SUDO apt-get update && $SUDO apt-get install -y "${missing_apt[@]}" || fail=1
fi

if [[ ${#missing_pip[@]} -gt 0 ]]; then
    echo "== pip: ${missing_pip[*]}"
    if [[ $os == linux ]]; then
        extra=""
        ls /usr/lib/python3*/EXTERNALLY-MANAGED >/dev/null 2>&1 && extra="--break-system-packages"
        $SUDO "$PY" -m pip install $extra "${missing_pip[@]}" || fail=1
    else
        "$PY" -m pip install "${missing_pip[@]}" || fail=1
    fi
fi

fetch() {   # url dest -- to a .part file first, so an interrupted download is never mistaken for a model
    curl -fL --progress-bar -o "$2.part" "$1" && mv "$2.part" "$2"
}

for item in "${missing_models[@]}"; do
    kind="${item%%:*}"; name="${item#*:}"
    if [[ $kind == vosk ]]; then
        echo "== Vosk model $name"
        mkdir -p "$VOSK_DIR"
        zip="$VOSK_DIR/$name.zip"
        if fetch "$VOSK_URL/$name.zip" "$zip" \
           && (cd "$VOSK_DIR" && "$PY" -m zipfile -e "$name.zip" .) && [[ -d "$VOSK_DIR/$name" ]]; then
            rm -f "$zip"
        else
            echo "   failed: $name" >&2; fail=1
        fi
    else
        echo "== Piper voice $name"
        mkdir -p "$PIPER_DIR"
        fetch "$PIPER_URL/$name.onnx"      "$PIPER_DIR/$name.onnx" \
            && fetch "$PIPER_URL/$name.onnx.json" "$PIPER_DIR/$name.onnx.json" \
            || { echo "   failed: $name" >&2; fail=1; }
    fi
done

echo
if [[ $fail -ne 0 ]]; then
    echo "Some items failed (see above). Run again to retry just those."
    exit 1
fi
echo "Done."
next_steps
