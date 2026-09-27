#!/usr/bin/env bash
# Server launcher: computer.py with both Vosk models.
#
# The large model (~1.8 GB) is REQUIRED: captain's log is a core feature and
# depends on it (Captain, 2026-09-27; README §11). It costs a slower start and
# ~5 GB of RAM, which is accepted rather than making dictation optional.
# ./install-dependencies.sh --server fetches both models into ../.vosk/.
#
# Paths are relative to this script, not to wherever it is run from.
cd "$(dirname "$0")" || exit 1
SMALL=../.vosk/vosk-model-small-en-us-0.15
LARGE=../.vosk/vosk-model-en-us-0.22

for m in "$SMALL" "$LARGE"; do
    if [ ! -d "$m" ]; then
        echo "computer.sh: Vosk model not found: $m" >&2
        echo "             run ./install-dependencies.sh --server" >&2
        exit 1
    fi
done

# Debian and Ubuntu ship python3 but no `python`; Windows has `python`.
PY=python
command -v python >/dev/null 2>&1 || PY=python3
exec "$PY" computer.py "$SMALL" "$LARGE"
