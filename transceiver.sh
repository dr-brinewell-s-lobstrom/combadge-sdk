#!/usr/bin/env bash
# Transceiver launcher (Linux, runs transceiver.py as root via sudo).
#
# The server defaults to this machine. For a server elsewhere:
#     SDK_SERVER_HOST=192.168.x.x ./transceiver.sh
# The variable is expanded HERE, in your shell, and handed to sudo on its
# command line: sudo resets the environment, so an exported SDK_SERVER_HOST
# would otherwise be stripped and the listeners would wait on localhost.
# SDK_SERVER_PORT works the same way (default 1701).
cd "$(dirname "$0")" || exit 1
exec sudo SDK_SERVER_HOST="${SDK_SERVER_HOST:-localhost}" \
          SDK_SERVER_PORT="${SDK_SERVER_PORT:-1701}" \
          python3 transceiver.py
