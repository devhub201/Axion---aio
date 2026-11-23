#!/usr/bin/env bash
# Run on your VPS to let a BOT-HOSTING panel use this VPS's Ollama.
# Usage: bash ollama_remote.sh <BOT_HOST_IP>     (get the IP from your host's panel / ask support)
set -e
IP="$1"; [ -n "$IP" ] || { echo "Usage: bash ollama_remote.sh <bot-host-ip>"; exit 1; }
sudo mkdir -p /etc/systemd/system/ollama.service.d
printf '[Service]\nEnvironment="OLLAMA_HOST=0.0.0.0:11434"\n' | sudo tee /etc/systemd/system/ollama.service.d/remote.conf >/dev/null
sudo systemctl daemon-reload && sudo systemctl restart ollama
if command -v ufw >/dev/null; then
  sudo ufw allow from "$IP" to any port 11434 proto tcp
  sudo ufw deny 11434/tcp || true
  echo "Firewall: only $IP can reach port 11434"
else
  echo "WARNING: no ufw found. Restrict port 11434 to $IP in your firewall/provider panel, otherwise anyone can use your AI."
fi
echo "In the bot host set:  OLLAMA_URL=http://$(curl -s ifconfig.me):11434"
