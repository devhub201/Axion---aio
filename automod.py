"""Auto-moderation (spam, links, invites, caps, mass mentions, bad words) + moderation commands."""
import datetime
import re
import time
from collections import defaultdict, deque
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import brand

DEFAULTS = {
    "enabled": True,
    "spam": True, "spam_count": 6, "spam_secs": 6,
    "dupes": True,
    "invites": True,
    "links": False,
    "caps": True,
    "mentions": True, "mention_limit": 5,
    "emoji_spam": True, "emoji_limit": 12,
    "words": True,
    "bad_words": [],
    "allowed_domains": [],
    "whitelist_roles": [],
    "whitelist_channels": [],
    "strikes": [[3, 600], [5, 3600], [8, 86400]],     # [strikes in 24h, timeout seconds]
}

STARTER_WORDS = ["fuck", "shit", "bitch", "asshole", "madarchod", "behenchod", "bhenchod", "chutiya", "gandu", "randi",
                 "harami", "bhosdike"]

INVITE_RE = re.compile(r"(?:discord\.gg|discord(?:app)?\.com/invite|dsc\.gg)/[\w-]+", re.I)
LINK_RE = re.compile(r"https?://([^\s/<>]+)", re.I)
EMOJI_RE = re.compile(r"<a?:\w+:\d+>|[\U0001F300-\U0001FAFF☀-➿]")

FEATURES = {
    "spam": "Message spam", "dupes": "Repeated messages", "invites": "Discord invites", "links": "All links",
    "caps": "Excessive CAPS", "mentions": "Mass mentions", "emoji_spam": "Emoji spam", "words": "Bad words",
}


class AutoMod(commands.Cog, name="AutoMod"):
    automod = app_commands.Group(name="automod", description="Auto-moderation settings",
                                 default_permissions=discord.Permissions(manage_guild=True), guild_only=True)

    def __init__(self, bot):
        self.bot = bot
        self.history = defaultdict(lambda: deque(maxlen=30))     # (guild, user) -> (time, message)
        self.dupes = defaultdict(lambda: deque(maxlen=10))       # (guild, user) -> (time, text)
        self.cache = {}

    async def cfg(self, guild_id):
        return await self.bot.db.cfg(guild_id, "automod", DEFAULTS)

    # ------------------------------------------------------------ detection
    def exempt(self, message, cfg):
        m = message.author
        if not isinstance(m, discord.Member):
            return True
        p = m.guild_permissions
        if p.administrator or p.manage_messages or p.manage_guild:
            return True
        if message.channel.id in cfg["whitelist_channels"]:
            return True
        return any(r.id in cfg["whitelist_roles"] for r in m.roles)

    def detect(self, message, cfg):
        """Returns (reason, extra_messages_to_delete) or None."""
        text = message.content or ""
        key = (message.guild.id, message.author.id)
        now = time.monotonic()

        if cfg["spam"]:
            dq = self.history[key]
            dq.append((now, message))
            recent = [m for t, m in dq if now - t <= cfg["spam_secs"]]
            if len(recent) >= cfg["spam_count"]:
                dq.clear()
                return "spam", [m for m in recent if m.id != message.id]
        if cfg["dupes"] and len(text) >= 4:
            dq = self.dupes[key]
            norm = re.sub(r"\s+", " ", text.lower()).strip()
            dq.append((now, norm))
            if sum(1 for t, s in dq if s == norm and now - t <= 30) >= 4:
                dq.clear()
                return "repeated messages", []
        if cfg["invites"] and INVITE_RE.search(text):
            return "Discord invite link", []
        if cfg["links"]:
            allowed = [d.lower() for d in cfg["allowed_domains"]]
            for host in LINK_RE.findall(text):
                host = host.lower().split(":")[0]
                if not any(host == d or host.endswith("." + d) for d in allowed):
                    return "link not allowed", []
        if cfg["mentions"] and len(message.mentions) + len(message.role_mentions) >= cfg["mention_limit"]:
            return "mass mentions", []
        if cfg["emoji_spam"] and len(EMOJI_RE.findall(text)) >= cfg["emoji_limit"]:
            return "emoji spam", []
        if cfg["caps"]:
            letters = [c for c in text if c.isalpha()]
            if len(letters) >= 15 and sum(c.isupper() for c in letters) / len(letters) > 0.7:
                return "excessive caps", []
        if cfg["words"] and cfg["bad_words"]:
            low = text.lower()
            for w in cfg["bad_words"]:
                if re.search(rf"(?<!\w){re.escape(w.lower())}(?!\w)", low):
                    return "blocked word", []
        return None

    @commands.Cog.listener()
    async def on_message(self, message):
        await self.scan(message)

    @commands.Cog.listener()
    async def on_message_edit(self, before, after):
        if before.content != after.content:
            await self.scan(after)

    async def scan(self, message):
        if message.author.bot or not message.guild:
            return
        cfg = await self.cfg(message.guild.id)
        if not cfg["enabled"] or self.exempt(message, cfg):
            return
        hit = self.detect(message, cfg)
        if hit:
            await self.punish(message, hit[0], hit[1], cfg)

    # ------------------------------------------------------------ punishment
    async def punish(self, message, reason, extra, cfg):
        guild, member = message.guild, message.author
        for m in [message] + list(extra):
            try:
                await m.delete()
            except discord.HTTPException:
                pass
        await self.bot.db.execute(
            "INSERT INTO warnings(guild_id,user_id,mod_id,reason,created_at) VALUES(?,?,?,?,?)",
            (guild.id, member.id, self.bot.user.id, f"AutoMod: {reason}", time.time()))
        rows = await self.bot.db.fetchall(
            "SELECT COUNT(*) c FROM warnings WHERE guild_id=? AND user_id=? AND reason LIKE 'AutoMod:%' AND created_at>?",
            (guild.id, member.id, time.time() - 86400))
        strikes = rows[0]["c"]
        secs = 0
        for need, dur in sorted(cfg["strikes"]):
            if strikes >= need:
                secs = dur
        outcome = f"Strike **{strikes}**"
        failure = None
        if secs and not member.is_timed_out():
            try:
                await member.timeout(datetime.timedelta(seconds=secs), reason=f"AutoMod: {reason}")
                outcome += f" • timed out for **{brand.human_duration(secs)}**"
            except discord.HTTPException as exc:
                failure = f"Could not time out {member}: {exc.text} (my role must be above theirs and I need **Moderate Members**)."
        try:
            await message.channel.send(
                embed=brand.embed(self.bot, "warn", "AutoMod", f"{member.mention} — **{reason}** is not allowed here.\n{outcome}",
                                  guild=guild, ts=False),
                delete_after=9)
        except discord.HTTPException:
            pass
        log = brand.embed(self.bot, "warn", "AutoMod action", None, guild=guild, fields=[
            ("{members} User", f"{member.mention} (`{member.id}`)", True), ("Reason", reason, True),
            ("Channel", message.channel.mention, True), ("Result", outcome, False)])
        if message.content:
            log.add_field(name="Message", value=discord.utils.escape_markdown(message.content[:900]) or "—", inline=False)
        if failure:
            log.add_field(name="❌ Problem", value=failure, inline=False)
        await brand.send_log(self.bot, guild, log)

    # ------------------------------------------------------------ settings UI
    @automod.command(name="status", description="Show every auto-mod setting")
    async def status(self, interaction: discord.Interaction):
        cfg = await self.cfg(interaction.guild.id)
        lines = [f"{'{online}' if cfg[k] else '{offline}'} **{label}**" for k, label in FEATURES.items()]
        fields = [
            ("Master switch", "{online} ON" if cfg["enabled"] else "{offline} OFF", True),
            ("Spam rule", f"{cfg['spam_count']} msgs / {cfg['spam_secs']}s", True),
            ("Mention limit", str(cfg["mention_limit"]), True),
            ("Emoji limit", str(cfg["emoji_limit"]), True),
            ("Blocked words", str(len(cfg["bad_words"])), True),
            ("Allowed domains", ", ".join(cfg["allowed_domains"]) or "none", True),
            ("Strikes (24h)", " • ".join(f"{n}→{brand.human_duration(s)}" for n, s in cfg["strikes"]), False),
        ]
        await interaction.response.send_message(embed=brand.embed(
            self.bot, "premium", "AutoMod", "\n".join(lines), guild=interaction.guild, fields=fields), ephemeral=True)

    @automod.command(name="toggle", description="Turn a protection on or off")
    @app_commands.choices(feature=[app_commands.Choice(name="Everything (master switch)", value="enabled")] +
                          [app_commands.Choice(name=v, value=k) for k, v in FEATURES.items()])
    async def toggle(self, interaction: discord.Interaction, feature: app_commands.Choice[str], enabled: bool):
        await self.bot.db.cfg_set(interaction.guild.id, "automod", DEFAULTS, **{feature.value: enabled})
        await interaction.response.send_message(embed=brand.embed(
            self.bot, "ok", "AutoMod updated", f"**{feature.name}** is now {'{online} ON' if enabled else '{offline} OFF'}.",
            guild=interaction.guild), ephemeral=True)

    @automod.command(name="limit", description="Change a threshold")
    @app_commands.choices(setting=[
        app_commands.Choice(name="Spam: messages", value="spam_count"), app_commands.Choice(name="Spam: seconds", value="spam_secs"),
        app_commands.Choice(name="Mention limit", value="mention_limit"), app_commands.Choice(name="Emoji limit", value="emoji_limit")])
    async def limit(self, interaction: discord.Interaction, setting: app_commands.Choice[str],
                    value: app_commands.Range[int, 2, 60]):
        await self.bot.db.cfg_set(interaction.guild.id, "automod", DEFAULTS, **{setting.value: value})
        await interaction.response.send_message(embed=brand.embed(
            self.bot, "ok", "Limit updated", f"**{setting.name}** = `{value}`", guild=interaction.guild), ephemeral=True)

    @automod.command(name="word", description="Manage blocked words")
    @app_commands.choices(action=[app_commands.Choice(name=a, value=a) for a in ("add", "remove", "list", "load-starter-list")])
    async def word(self, interaction: discord.Interaction, action: app_commands.Choice[str], word: Optional[str] = None):
        g = interaction.guild
        cfg = await self.cfg(g.id)
        words = cfg["bad_words"]
        if action.value == "list":
            body = ", ".join(f"||{w}||" for w in words) or "No blocked words yet."
            return await interaction.response.send_message(embed=brand.embed(
                self.bot, "info", "Blocked words", body[:3900], guild=g), ephemeral=True)
        if action.value == "load-starter-list":
            words = sorted(set(words) | set(STARTER_WORDS))
        else:
            if not word:
                return await interaction.response.send_message(embed=brand.embed(
                    self.bot, "err", "Missing word", "Give the `word` to add/remove.", guild=g), ephemeral=True)
            w = word.lower().strip()
            words = sorted(set(words) | {w}) if action.value == "add" else [x for x in words if x != w]
        await self.bot.db.cfg_set(g.id, "automod", DEFAULTS, bad_words=words)
        await interaction.response.send_message(embed=brand.embed(
            self.bot, "ok", "Word list updated", f"Now blocking **{len(words)}** word(s).", guild=g), ephemeral=True)

    @automod.command(name="whitelist", description="Exempt a role or channel from auto-mod")
    async def whitelist(self, interaction: discord.Interaction, enabled: bool,
                        role: Optional[discord.Role] = None, channel: Optional[discord.TextChannel] = None):
        if not role and not channel:
            return await interaction.response.send_message(embed=brand.embed(
                self.bot, "err", "Nothing chosen", "Pick a role or a channel.", guild=interaction.guild), ephemeral=True)
        cfg = await self.cfg(interaction.guild.id)
        roles, chans = set(cfg["whitelist_roles"]), set(cfg["whitelist_channels"])
        if role:
            (roles.add if enabled else roles.discard)(role.id)
        if channel:
            (chans.add if enabled else chans.discard)(channel.id)
        await self.bot.db.cfg_set(interaction.guild.id, "automod", DEFAULTS,
                                  whitelist_roles=list(roles), whitelist_channels=list(chans))
        await interaction.response.send_message(embed=brand.embed(
            self.bot, "ok", "Whitelist updated", f"Roles: {len(roles)} • Channels: {len(chans)}", guild=interaction.guild),
            ephemeral=True)

    @automod.command(name="domain", description="Allow a domain when 'All links' blocking is on")
    async def domain(self, interaction: discord.Interaction, domain: str, allow: bool = True):
        d = domain.lower().replace("https://", "").replace("http://", "").strip("/ ")
        cfg = await self.cfg(interaction.guild.id)
        doms = set(cfg["allowed_domains"])
        (doms.add if allow else doms.discard)(d)
        await self.bot.db.cfg_set(interaction.guild.id, "automod", DEFAULTS, allowed_domains=sorted(doms))
        await interaction.response.send_message(embed=brand.embed(
            self.bot, "ok", "Domains updated", ", ".join(sorted(doms)) or "none", guild=interaction.guild), ephemeral=True)


# ---------------------------------------------------------------------------
class Moderation(commands.Cog, name="Moderation"):
    def __init__(self, bot):
        self.bot = bot

    def _can_act(self, interaction, target):
        """Returns an error string or None."""
        guild = interaction.guild
        if target.id == interaction.user.id:
            return "You cannot do that to yourself."
        if target.id == guild.owner_id:
            return "You cannot do that to the server owner."
        if target.id == self.bot.user.id:
            return "Nice try."
        if interaction.user.id != guild.owner_id and isinstance(target, discord.Member) \
                and target.top_role >= interaction.user.top_role:
            return "That member's top role is equal to or higher than yours."
        if isinstance(target, discord.Member) and target.top_role >= guild.me.top_role:
            return "That member's top role is higher than mine. Move my role up."
        return None

    async def _deny(self, interaction, text):
        await interaction.response.send_message(embed=brand.embed(self.bot, "err", "Cannot do that", text,
                                                                  guild=interaction.guild), ephemeral=True)

    async def _done(self, interaction, kind, title, member, reason, extra=None, dm=True):
        g = interaction.guild
        fields = [("{members} User", f"{member.mention} (`{member.id}`)", True),
                  ("{crown} Moderator", interaction.user.mention, True), ("Reason", reason[:900], False)]
        if extra:
            fields.append(extra)
        em = brand.embed(self.bot, kind, title, None, guild=g, fields=fields)
        await interaction.followup.send(embed=em) if interaction.response.is_done() else \
            await interaction.response.send_message(embed=em)
        await brand.send_log(self.bot, g, em)

    async def _dm(self, member, kind, title, guild, text):
        try:
            await member.send(embed=brand.embed(self.bot, kind, title, text, guild=guild))
        except discord.HTTPException:
            pass

    @app_commands.command(name="warn", description="Warn a member")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.guild_only()
    async def warn(self, interaction: discord.Interaction, member: discord.Member, reason: str):
        if (err := self._can_act(interaction, member)):
            return await self._deny(interaction, err)
        await self.bot.db.execute("INSERT INTO warnings(guild_id,user_id,mod_id,reason,created_at) VALUES(?,?,?,?,?)",
                                  (interaction.guild.id, member.id, interaction.user.id, reason, time.time()))
        total = (await self.bot.db.fetchone("SELECT COUNT(*) c FROM warnings WHERE guild_id=? AND user_id=?",
                                            (interaction.guild.id, member.id)))["c"]
        await self._dm(member, "warn", "You were warned", interaction.guild,
                       f"Server: **{interaction.guild.name}**\nReason: {reason}")
        await self._done(interaction, "warn", "Member warned", member, reason, ("Total warnings", str(total), True))

    @app_commands.command(name="warnings", description="List a member's warnings")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.guild_only()
    async def warnings(self, interaction: discord.Interaction, member: discord.Member):
        rows = await self.bot.db.fetchall(
            "SELECT id, mod_id, reason, created_at FROM warnings WHERE guild_id=? AND user_id=? ORDER BY id DESC LIMIT 15",
            (interaction.guild.id, member.id))
        body = "\n".join(f"`#{r['id']}` <t:{int(r['created_at'])}:d> — {r['reason'][:90]}" for r in rows) or "Clean record."
        await interaction.response.send_message(embed=brand.embed(
            self.bot, "info", f"Warnings • {member.display_name}", body, guild=interaction.guild,
            thumbnail=member.display_avatar.url), ephemeral=True)

    @app_commands.command(name="clearwarns", description="Delete all warnings of a member")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.guild_only()
    async def clearwarns(self, interaction: discord.Interaction, member: discord.Member):
        n = await self.bot.db.rowcount("DELETE FROM warnings WHERE guild_id=? AND user_id=?", (interaction.guild.id, member.id))
        await interaction.response.send_message(embed=brand.embed(
            self.bot, "ok", "Warnings cleared", f"Removed **{n}** warning(s) from {member.mention}.", guild=interaction.guild))

    @app_commands.command(name="timeout", description="Timeout a member")
    @app_commands.describe(duration="e.g. 10m, 2h, 1d (max 28d)")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.guild_only()
    async def timeout(self, interaction: discord.Interaction, member: discord.Member, duration: str,
                      reason: str = "No reason given"):
        if (err := self._can_act(interaction, member)):
            return await self._deny(interaction, err)
        secs = brand.parse_duration(duration)
        if not secs or secs > 28 * 86400:
            return await self._deny(interaction, "Use a duration like `10m`, `2h` or `1d` (max 28 days).")
        try:
            await member.timeout(datetime.timedelta(seconds=secs), reason=f"{interaction.user}: {reason}")
        except discord.HTTPException as exc:
            return await self._deny(interaction, f"Discord refused: {exc.text}")
        await self._dm(member, "warn", "You were timed out", interaction.guild,
                       f"Server: **{interaction.guild.name}**\nFor: **{brand.human_duration(secs)}**\nReason: {reason}")
        await self._done(interaction, "warn", "Member timed out", member, reason, ("Duration", brand.human_duration(secs), True))

    @app_commands.command(name="untimeout", description="Remove a timeout")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.guild_only()
    async def untimeout(self, interaction: discord.Interaction, member: discord.Member):
        try:
            await member.timeout(None, reason=f"Untimeout by {interaction.user}")
        except discord.HTTPException as exc:
            return await self._deny(interaction, f"Discord refused: {exc.text}")
        await self._done(interaction, "ok", "Timeout removed", member, "Timeout lifted")

    @app_commands.command(name="kick", description="Kick a member")
    @app_commands.default_permissions(kick_members=True)
    @app_commands.guild_only()
    async def kick(self, interaction: discord.Interaction, member: discord.Member, reason: str = "No reason given"):
        if (err := self._can_act(interaction, member)):
            return await self._deny(interaction, err)
        await self._dm(member, "warn", "You were kicked", interaction.guild,
                       f"Server: **{interaction.guild.name}**\nReason: {reason}")
        try:
            await member.kick(reason=f"{interaction.user}: {reason}")
        except discord.HTTPException as exc:
            return await self._deny(interaction, f"Discord refused: {exc.text}")
        await self._done(interaction, "warn", "Member kicked", member, reason)

    @app_commands.command(name="ban", description="Ban a member")
    @app_commands.default_permissions(ban_members=True)
    @app_commands.guild_only()
    async def ban(self, interaction: discord.Interaction, member: discord.Member, reason: str = "No reason given",
                  delete_days: app_commands.Range[int, 0, 7] = 0):
        if (err := self._can_act(interaction, member)):
            return await self._deny(interaction, err)
        await self._dm(member, "err", "You were banned", interaction.guild,
                       f"Server: **{interaction.guild.name}**\nReason: {reason}")
        try:
            await interaction.guild.ban(member, reason=f"{interaction.user}: {reason}", delete_message_seconds=delete_days * 86400)
        except discord.HTTPException as exc:
            return await self._deny(interaction, f"Discord refused: {exc.text}")
        await self._done(interaction, "err", "Member banned", member, reason)

    @app_commands.command(name="unban", description="Unban a user by ID")
    @app_commands.default_permissions(ban_members=True)
    @app_commands.guild_only()
    async def unban(self, interaction: discord.Interaction, user_id: str, reason: str = "No reason given"):
        if not user_id.isdigit():
            return await self._deny(interaction, "Give the numeric user ID.")
        try:
            user = await self.bot.fetch_user(int(user_id))
            await interaction.guild.unban(user, reason=f"{interaction.user}: {reason}")
        except discord.NotFound:
            return await self._deny(interaction, "That user is not banned (or the ID is wrong).")
        except discord.HTTPException as exc:
            return await self._deny(interaction, f"Discord refused: {exc.text}")
        await self._done(interaction, "ok", "User unbanned", user, reason)

    @app_commands.command(name="purge", description="Delete recent messages")
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.guild_only()
    async def purge(self, interaction: discord.Interaction, amount: app_commands.Range[int, 1, 200],
                    member: Optional[discord.Member] = None):
        await interaction.response.defer(ephemeral=True)
        check = (lambda m: m.author.id == member.id) if member else None
        try:
            deleted = await interaction.channel.purge(limit=amount, check=check, reason=f"Purge by {interaction.user}")
        except discord.HTTPException as exc:
            return await interaction.followup.send(embed=brand.embed(
                self.bot, "err", "Purge failed", exc.text, guild=interaction.guild), ephemeral=True)
        await interaction.followup.send(embed=brand.embed(
            self.bot, "ok", "Purged", f"Deleted **{len(deleted)}** message(s). (Discord skips messages older than 14 days.)",
            guild=interaction.guild), ephemeral=True)

    @app_commands.command(name="slowmode", description="Set slowmode in this channel (0 = off)")
    @app_commands.default_permissions(manage_channels=True)
    @app_commands.guild_only()
    async def slowmode(self, interaction: discord.Interaction, seconds: app_commands.Range[int, 0, 21600]):
        await interaction.channel.edit(slowmode_delay=seconds)
        await interaction.response.send_message(embed=brand.embed(
            self.bot, "ok", "Slowmode", f"Slowmode is now **{seconds}s**.", guild=interaction.guild))

    @app_commands.command(name="lock", description="Lock a channel for @everyone")
    @app_commands.default_permissions(manage_channels=True)
    @app_commands.guild_only()
    async def lock(self, interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
        ch = channel or interaction.channel
        await ch.set_permissions(interaction.guild.default_role, send_messages=False, reason=f"Lock by {interaction.user}")
        await interaction.response.send_message(embed=brand.embed(
            self.bot, "warn", "Channel locked", f"{{lock}} {ch.mention} is locked.", guild=interaction.guild))

    @app_commands.command(name="unlock", description="Unlock a channel")
    @app_commands.default_permissions(manage_channels=True)
    @app_commands.guild_only()
    async def unlock(self, interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
        ch = channel or interaction.channel
        await ch.set_permissions(interaction.guild.default_role, send_messages=None, reason=f"Unlock by {interaction.user}")
        await interaction.response.send_message(embed=brand.embed(
            self.bot, "ok", "Channel unlocked", f"{ch.mention} is open again.", guild=interaction.guild))


async def setup(bot):
    await bot.add_cog(AutoMod(bot))
    await bot.add_cog(Moderation(bot))
