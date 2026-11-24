#!/usr/bin/env bash
# Adds/updates the Groq key in .env without touching anything else (no nano needed).
#   bash set_groq.sh YOUR_GROQ_KEY
cd "$(dirname "$0")"
KEY="$1"
[ -n "$KEY" ] || { echo "Usage: bash set_groq.sh YOUR_GROQ_KEY"; exit 1; }
[ -f .env ] || { echo ".env not found here. Run setup_env.sh first."; exit 1; }
grep -v '^GROQ_' .env > .env.tmp
printf 'GROQ_API_KEY=%s\nGROQ_MODEL=llama-3.3-70b-versatile\n' "$KEY" >> .env.tmp
mv .env.tmp .env && chmod 600 .env
echo "Groq key saved (${#KEY} characters). Now fully stop and start the bot."
