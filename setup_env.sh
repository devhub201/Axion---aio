#!/usr/bin/env bash
# Creates .env by asking questions (no nano needed).
#   Interactive:   bash setup_env.sh
#   One-liner:     bash setup_env.sh TOKEN GUILD_ID OWNER_ID [META_API_KEY]
cd "$(dirname "$0")"
if [ -n "$1" ]; then
  TOKEN="$1"; GUILD="$2"; OWNER="$3"; META="$4"; OLL="http://127.0.0.1:11434"
else
  read -rp "Bot token: " TOKEN
  read -rp "Server (guild) ID: " GUILD
  read -rp "Your Discord user ID: " OWNER
  read -rp "Meta AI API key (Enter to skip): " META
  OLL="http://127.0.0.1:11434"
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
META_API_KEY=$META
META_API_URL=https://api.llama.com/compat/v1
META_MODEL=Llama-3.3-70B-Instruct
ENV
chmod 600 .env
echo ".env created."
