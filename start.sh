#!/usr/bin/env bash
# Universal start script: works on bot-hosting panels (Pterodactyl etc.) and on a VPS.
# Startup command on a panel:  bash start.sh      (or just: python bot.py after pip install)
cd "$(dirname "$0")"
PY=$(command -v python3 || command -v python)
if ! $PY -c "import discord, aiohttp, PIL" >/dev/null 2>&1; then
  echo "[start] installing requirements..."
  $PY -m pip install --user --no-cache-dir -r requirements.txt || $PY -m pip install --no-cache-dir -r requirements.txt || exit 1
fi
exec $PY bot.py
