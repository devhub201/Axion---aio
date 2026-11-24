# AxionHost Bot v1.0

All-in-one hosting community bot: tickets + self-hosted AI, AI announcements/embeds, animated custom emojis (drawn by the bot),
AutoMod, Anti-Nuke + Anti-Raid, moderation, status board, giveaways, welcome/verify, logs, invite tracker.
(Deploy/VPS-manager features are intentionally NOT included yet.)

## Files (put all in one folder)
bot.py, brand.py, db.py, icons.py, emojis.py, ai.py, tickets.py, automod.py, antinuke.py, community.py,
requirements.txt, install.sh, start.sh, ollama_remote.sh, Dockerfile, docker-compose.yml, .env.example, kb.md

## 1. Discord Developer Portal
Bot page -> enable **Server Members Intent** and **Message Content Intent**.
Invite with scopes `bot` + `applications.commands`. Give it **Administrator** (simplest) or: Manage Channels/Roles/Expressions/Webhooks/Server,
Ban/Kick/Moderate Members, View Audit Log, Manage Messages, Embed Links, Attach Files.
**Drag the bot's role to the TOP of the role list** (needed for anti-nuke and moderation).

## 2. Run it — pick ONE mode

### Mode A: on your VPS (bot + AI together)
    git clone https://github.com/devhub201/Axion---aio && cd Axion---aio
    bash install.sh        # asks token / server ID / your ID, then installs everything and starts the bot
    journalctl -u axionhost-bot -f
No nano needed. To re-create .env later in one line: `bash setup_env.sh TOKEN GUILD_ID YOUR_ID`.
Docker alternative: `cp .env.example .env`, edit, `docker compose up -d --build`, then `docker compose exec ollama ollama pull qwen2.5:3b`.

### Mode B: on a bot-hosting panel (Pterodactyl etc.) — AI stays on your VPS
Bot hosts are too small/limited for Ollama, so the bot runs there and talks to the Ollama on your VPS.
1. **VPS:** install Ollama (`curl -fsSL https://ollama.com/install.sh | sh && ollama pull qwen2.5:3b`), then `bash ollama_remote.sh <BOT_HOST_IP>` (opens port 11434 only for the bot host).
2. **Bot host:** upload all files (or pull from GitHub). Python 3.10+. Startup command: `bash start.sh` (installs requirements automatically, then runs bot.py).
3. **Bot host variables** (panel Startup/Variables tab; no file editing): `DISCORD_TOKEN`, `GUILD_ID`, `OWNER_IDS`, `OLLAMA_URL=http://YOUR_VPS_IP:11434`.
4. Start the server, then run `/ai status` in Discord to confirm the AI is reachable.
No VPS AI yet? The bot still works; ticket AI shows a clear "AI unavailable" notice and staff take over, and announcements fall back to templates.

Model size: 2-4 GB RAM -> qwen2.5:1.5b, 4-8 GB -> qwen2.5:3b (default), 8 GB+ -> qwen2.5:7b (set OLLAMA_MODEL, then `ollama pull <model>`).

## 3. First-time setup in Discord
1. `/emoji install` (or automatic on first start) - 28 animated emojis appear. `/emoji list` shows them.
2. `/setup logs #logs`
3. `/ticket setup` (category, log channel, staff role) then `/ticket panel` - the AI writes the panel title + description, you preview/edit/post.
4. `/ai status` - confirms the AI on your VPS works.
5. `/antinuke enable`, `/automod status`, `/setup welcome`, `/setup verify`, `/status setup` + `/status add`.

## Highlights
- **Tickets**: dropdown categories, private channels, AI answers first (Hinglish/English, uses kb.md), "Talk to Staff" pauses AI, claim, add/remove, HTML transcripts to log channel + DM.
- **AI announcements**: `/ai announce brief:"..." kind:offer` -> styled embed with custom emojis -> Post / Regenerate / Edit / Cancel.
- **Emojis**: `/emoji make name shape color text` draws a NEW animated emoji; use `{rocket}` style placeholders in any AI text.
- **Anti-nuke** (needs audit log): mass channel/role delete+create, mass ban/kick, webhook spam, unauthorized bot, admin-role grants, server rename -> auto-undo + punish. Owner-only config. `/antinuke lockdown`.
- **AutoMod**: spam, repeats, invites, links, caps, mass mentions, emoji spam, bad words, escalating timeouts.

## v1.1 — more features
- **Hosting tools**: `/plans add` + `/plans post` (plans embed with an **Order now** button that opens a Sales ticket), `/host payments|tos|vouch-channel|customer-role`, `/customer`, `/vouch` (1-5 stars) + `/vouches` rating. Plans you add are fed to the ticket AI automatically, so it quotes real prices.
- **Levels**: XP per message, rank card image (`/rank`), `/leaderboard`, level-role rewards (`/level role add`), double-XP via `/level settings multiplier`.
- **BOLTs economy**: `/daily` (streak), `/work`, `/pay`, `/coinflip`, `/slots`, `/richest`, role shop (`/shop add|buy`).
- **Engage**: polls with live bars, suggestions with vote/approve/deny, reminders, AFK, 8ball, dice, avatar.
- **Staff tools**: `/embed` builder, reaction-role button panels, sticky messages, auto-responders (FAQ), starboard, join-to-create voice channels, `/say`.

## Edit the AI's knowledge
`nano kb.md` - changes apply instantly. The AI never invents prices; it only uses this file.

## Troubleshooting
- Commands missing: set GUILD_ID in .env.
- `/ai status` fails: `systemctl status ollama`, `ollama list`.
- Bot host: data/ (SQLite) must be writable and persistent.
- Emojis fail: bot needs Manage Expressions; free animated slots needed (28).
