#!/usr/bin/env bash
# Creates .env by asking questions (no nano needed).
#   Interactive:   bash setup_env.sh
#   One-liner:     bash setup_env.sh TOKEN GUILD_ID OWNER_ID [OLLAMA_URL]
cd "$(dirname "$0")"
if [ -n "$1" ]; then
  TOKEN="$1"; GUILD="$2"; OWNER="$3"; OLL="${4:-http://127.0.0.1:11434}"
else
  read -rp "Bot token: " TOKEN
  read -rp "Server (guild) ID: " GUILD
  read -rp "Your Discord user ID: " OWNER
  read -rp "Ollama URL [Enter = http://127.0.0.1:11434]: " OLL
  OLL="${OLL:-http://127.0.0.1:11434}"
fi
[ -n "$TOKEN" ] || { echo "Token is required"; exit 1; }
cat > .env <<ENV
DISCORD_TOKEN=$TOKEN
GUILD_ID=$GUILD
OWNER_IDS=$OWNER
BRAND_NAME=AxionHost
OLLAMA_URL=$OLL
OLLAMA_MODEL=qwen2.5:3b
OLLAMA_TIMEOUT=180
AI_LANG=hinglish
AI_CONCURRENCY=1
KB_PATH=kb.md
AUTO_EMOJIS=1
ENV
chmod 600 .env
echo ".env created."
