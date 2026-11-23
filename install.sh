#!/usr/bin/env bash
# AxionHost bot installer for Ubuntu/Debian VPS. Run:  bash install.sh
set -e
cd "$(dirname "$0")"
echo "==> Installing Python deps"
sudo apt-get update -y && sudo apt-get install -y python3 python3-venv python3-pip fonts-dejavu-core curl
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt

if ! command -v ollama >/dev/null 2>&1; then
  echo "==> Installing Ollama (self-hosted AI)"
  curl -fsSL https://ollama.com/install.sh | sh
fi
[ -f .env ] || bash setup_env.sh
MODEL=$(grep -E '^OLLAMA_MODEL=' .env 2>/dev/null | cut -d= -f2)
MODEL=${MODEL:-qwen2.5:3b}
echo "==> Pulling AI model $MODEL (one time, can take a few minutes)"
ollama pull "$MODEL"



echo "==> Creating systemd service"
sudo tee /etc/systemd/system/axionhost-bot.service >/dev/null <<UNIT
[Unit]
Description=AxionHost Discord Bot
After=network-online.target ollama.service
Wants=network-online.target

[Service]
WorkingDirectory=$(pwd)
ExecStart=$(pwd)/venv/bin/python bot.py
Restart=always
RestartSec=5
User=$(whoami)

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable axionhost-bot
echo
sudo systemctl start axionhost-bot && echo "DONE - bot started."
echo "Logs: journalctl -u axionhost-bot -f"
