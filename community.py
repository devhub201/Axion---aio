"""Community features: setup, welcome/verify, logs, invite tracker, giveaways, live status monitor, utility commands."""
import asyncio
import random
import time
from typing import Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

import brand

WELCOME_DEFAULTS = {"channel_id": None, "role_id": None, "message": None}
VERIFY_DEFAULTS = {"role_id": None, "channel_id": None}
STATUS_DEFAULTS = {"channel_id": None, "message_id": None, "alert_role_id": None, "title": None}


# ---------------------------------------------------------------------------
# persistent views
# ---------------------------------------------------------------------------
class VerifyView(discord.ui.View):
    def __init__(self, bot, guild=None):
        super().__init__(timeout=None)
        self.bot = bot
        self.verify_btn.emoji = bot.emo.partial("shield", guild)

    @discord.ui.button(label="Verify", style=discord.ButtonStyle.success, custom_id="ax:verify")
    async def verify_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        g = interaction.guild
        cfg = await self.bot.db.cfg(g.id, "verify", VERIFY_DEFAULTS)
        role = g.get_role(cfg["role_id"]) if cfg["role_id"] else None
        if not role:
            return await interaction.response.send_message(embed=brand.embed(
                self.bot, "err", "Not configured", "Ask an admin to run `/setup verify` again.", guild=g), ephemeral=True)
        if role in interaction.user.roles:
            return await interaction.response.send_message(embed=brand.embed(
                self.bot, "info", "Already verified", "You already have access. Enjoy!", guild=g), ephemeral=True)
        try:
            await interaction.user.add_roles(role, reason="Verification button")
        except discord.HTTPException as exc:
            return await interaction.response.send_message(embed=brand.embed(
                self.bot, "err", "Could not verify you", f"{exc.text}\nTell staff: my role must be above the verified role.",
                guild=g), ephemeral=True)
        await interaction.response.send_message(embed=brand.embed(
            self.bot, "ok", "Verified!", f"{{check}} Welcome in — you now have {role.mention}.", guild=g), ephemeral=True)


class GiveawayView(discord.ui.View):
    def __init__(self, bot, guild=None):
        super().__init__(timeout=None)
        self.bot = bot
        self.enter_btn.emoji = bot.emo.partial("gift", guild)

    @discord.ui.button(label="Enter / Leave", style=discord.ButtonStyle.primary, custom_id="ax:gw:enter")
    async def enter_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        db = self.bot.db
        row = await db.fetchone("SELECT * FROM giveaways WHERE message_id=?", (interaction.message.id,))
        g = interaction.guild
        if not row or row["ended"]:
            return await interaction.response.send_message(embed=brand.embed(
                self.bot, "err", "Giveaway ended", "This giveaway is over.", guild=g), ephemeral=True)
        had = await db.fetchone("SELECT 1 x FROM giveaway_entries WHERE giveaway_id=? AND user_id=?",
                                (row["id"], interaction.user.id))
        if had:
            await db.execute("DELETE FROM giveaway_entries WHERE giveaway_id=? AND user_id=?", (row["id"], interaction.user.id))
            msg = "You left the giveaway."
        else:
            await db.execute("INSERT OR IGNORE INTO giveaway_entries(giveaway_id,user_id) VALUES(?,?)", (row["id"], interaction.user.id))
            msg = "{gift} You are in! Good luck!"
        total = (await db.fetchone("SELECT COUNT(*) c FROM giveaway_entries WHERE giveaway_id=?", (row["id"],)))["c"]
        await interaction.response.send_message(embed=brand.embed(
            self.bot, "ok", "Giveaway", f"{msg}\nEntries: **{total}**", guild=g), ephemeral=True)


# ---------------------------------------------------------------------------
class Community(commands.Cog, name="Community"):
    setup_group = app_commands.Group(name="setup", description="Server setup",
                                     default_permissions=discord.Permissions(manage_guild=True), guild_only=True)
    giveaway = app_commands.Group(name="giveaway", description="Giveaways",
                                  default_permissions=discord.Permissions(manage_guild=True), guild_only=True)
    status = app_commands.Group(name="status", description="Live service status board",
                                default_permissions=discord.Permissions(manage_guild=True), guild_only=True)

    def __init__(self, bot):
        self.bot = bot
        self.invites = {}
        self._invite_warned = set()

    async def cog_load(self):
        self.bot.add_view(VerifyView(self.bot))
        self.bot.add_view(GiveawayView(self.bot))
        self.giveaway_loop.start()
        self.status_loop.start()

    async def cog_unload(self):
        self.giveaway_loop.cancel()
        self.status_loop.cancel()

    # ------------------------------------------------------------------ setup
    @setup_group.command(name="logs", description="Set the channel for logs (moderation, joins, deletes, AutoMod, anti-nuke)")
    async def set_logs(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if not channel.permissions_for(interaction.guild.me).send_messages:
            return await interaction.response.send_message(embed=brand.embed(
                self.bot, "err", "No access", f"I cannot send messages in {channel.mention}.", guild=interaction.guild), ephemeral=True)
        await self.bot.db.set(interaction.guild.id, "log_channel", channel.id)
        await interaction.response.send_message(embed=brand.embed(
            self.bot, "ok", "Logs enabled", f"{{stats}} Logs will go to {channel.mention}.", guild=interaction.guild), ephemeral=True)

    @setup_group.command(name="welcome", description="Welcome new members with a premium embed")
    @app_commands.describe(channel="Where to welcome people", autorole="Role given automatically on join",
                           message="Custom text. Placeholders: {user} {server} {count}")
    async def set_welcome(self, interaction: discord.Interaction, channel: discord.TextChannel,
                          autorole: Optional[discord.Role] = None, message: Optional[str] = None):
        g = interaction.guild
        if autorole and autorole >= g.me.top_role:
            return await interaction.response.send_message(embed=brand.embed(
                self.bot, "err", "Role too high", "That role is above mine — move my role higher first.", guild=g), ephemeral=True)
        await self.bot.db.cfg_set(g.id, "welcome", WELCOME_DEFAULTS, channel_id=channel.id,
                                  role_id=autorole.id if autorole else None, message=message)
        await interaction.response.send_message(embed=brand.embed(
            self.bot, "ok", "Welcome configured", f"Channel: {channel.mention}\nAuto role: {autorole.mention if autorole else 'none'}",
            guild=g), ephemeral=True)

    @setup_group.command(name="verify", description="Post a verification button that gives a role")
    async def set_verify(self, interaction: discord.Interaction, channel: discord.TextChannel, role: discord.Role):
        g = interaction.guild
        if role >= g.me.top_role:
            return await interaction.response.send_message(embed=brand.embed(
                self.bot, "err", "Role too high", "That role is above mine — move my role higher first.", guild=g), ephemeral=True)
        await self.bot.db.cfg_set(g.id, "verify", VERIFY_DEFAULTS, role_id=role.id, channel_id=channel.id)
        em = brand.embed(self.bot, "premium", "Verify to enter",
                         f"{{shield}} Press the button below to unlock the server.\n{brand.LINE}\n"
                         "{check} Takes one second\n{lock} Keeps bots and raiders out", guild=g)
        try:
            await channel.send(embed=em, view=VerifyView(self.bot, g))
        except discord.HTTPException as exc:
            return await interaction.response.send_message(embed=brand.embed(
                self.bot, "err", "Could not post", exc.text, guild=g), ephemeral=True)
        await interaction.response.send_message(embed=brand.embed(
            self.bot, "ok", "Verification live", f"Posted in {channel.mention}. Members get {role.mention}.", guild=g), ephemeral=True)

    @setup_group.command(name="show", description="Show the current setup")
    async def show(self, interaction: discord.Interaction):
        g = interaction.guild
        log = await self.bot.db.get(g.id, "log_channel")
        w = await self.bot.db.cfg(g.id, "welcome", WELCOME_DEFAULTS)
        v = await self.bot.db.cfg(g.id, "verify", VERIFY_DEFAULTS)
        t = await self.bot.db.cfg(g.id, "tickets", brand.TICKET_DEFAULTS)
        a = await self.bot.db.cfg(g.id, "antinuke", {"enabled": False})
        m = await self.bot.db.cfg(g.id, "automod", {"enabled": True})
        c = lambda i: f"<#{i}>" if i else "—"
        r = lambda i: f"<@&{i}>" if i else "—"
        await interaction.response.send_message(embed=brand.embed(
            self.bot, "premium", "Server setup", None, guild=g, fields=[
                ("{stats} Logs", c(log), True), ("{star} Welcome", c(w["channel_id"]), True), ("Auto role", r(w["role_id"]), True),
                ("{shield} Verify role", r(v["role_id"]), True), ("{ticket} Ticket category", c(t["category_id"]), True),
                ("{crown} Staff role", r(t["staff_role_id"]), True),
                ("{robot} Ticket AI", "on" if t["ai"] else "off", True),
                ("AutoMod", "on" if m["enabled"] else "off", True), ("Anti-nuke", "on" if a["enabled"] else "off", True),
                ("Emoji pack", "installed" if self.bot.emo.has(g) else "run /emoji install", True)]), ephemeral=True)

    # ------------------------------------------------------------------ welcome + invites
    async def _read_invites(self, guild):
        try:
            invs = await guild.invites()
        except discord.HTTPException:
            if guild.id not in self._invite_warned:
                self._invite_warned.add(guild.id)
                print(f"[invites] {guild.name}: need 'Manage Server' to track invites")
            return None
        return {i.code: (i.uses or 0, i.inviter.id if i.inviter else None) for i in invs}

    @commands.Cog.listener()
    async def on_ready(self):
        for g in self.bot.guilds:
            data = await self._read_invites(g)
            if data is not None:
                self.invites[g.id] = data

    @commands.Cog.listener()
    async def on_invite_create(self, invite):
        data = await self._read_invites(invite.guild)
        if data is not None:
            self.invites[invite.guild.id] = data

    @commands.Cog.listener()
    async def on_invite_delete(self, invite):
        self.invites.get(invite.guild.id, {}).pop(invite.code, None)

    @commands.Cog.listener()
    async def on_member_join(self, member):
        g = member.guild
        inviter_id, code = None, None
        before = self.invites.get(g.id)
        after = await self._read_invites(g)
        if after is not None:
            if before:
                for c, (uses, inv) in after.items():
                    if uses > before.get(c, (0, None))[0]:
                        inviter_id, code = inv, c
                        break
                if code is None:
                    for c, (uses, inv) in before.items():          # single-use invite that vanished
                        if c not in after:
                            inviter_id, code = inv, c
                            break
            self.invites[g.id] = after
        if inviter_id and not member.bot:
            await self.bot.db.execute(
                "INSERT OR REPLACE INTO invites(guild_id,member_id,inviter_id,code,joined_at) VALUES(?,?,?,?,?)",
                (g.id, member.id, inviter_id, code, time.time()))

        cfg = await self.bot.db.cfg(g.id, "welcome", WELCOME_DEFAULTS)
        if cfg["role_id"] and not member.bot:
            role = g.get_role(cfg["role_id"])
            if role:
                try:
                    await member.add_roles(role, reason="Auto role")
                except discord.HTTPException as exc:
                    print(f"[welcome] auto role failed in {g.name}: {exc}")
        if cfg["channel_id"]:
            ch = g.get_channel(cfg["channel_id"])
            if ch:
                text = (cfg["message"] or "Welcome {user} to **{server}**! You are member **#{count}**.").format_map(
                    {"user": member.mention, "server": g.name, "count": g.member_count})
                invited = f"\n{{members}} Invited by <@{inviter_id}>" if inviter_id else ""
                em = brand.embed(self.bot, "premium", f"Welcome to {g.name}!",
                                 f"{{rocket}} {text}{invited}\n{brand.LINE}\n{{ticket}} Need help? Open a ticket anytime.\n"
                                 "{shield} Read the rules and enjoy your stay!", guild=g, thumbnail=member.display_avatar.url)
                try:
                    await ch.send(content=member.mention, embed=em, allowed_mentions=discord.AllowedMentions(users=[member]))
                except discord.HTTPException as exc:
                    print(f"[welcome] cannot post in {g.name}: {exc}")
        em = brand.embed(self.bot, "ok", "Member joined", f"{member.mention} (`{member.id}`)", guild=g, fields=[
            ("Account created", f"<t:{int(member.created_at.timestamp())}:R>", True),
            ("Invited by", f"<@{inviter_id}>" if inviter_id else "unknown", True)])
        await brand.send_log(self.bot, g, em)

    @commands.Cog.listener()
    async def on_member_remove(self, member):
        await brand.send_log(self.bot, member.guild, brand.embed(
            self.bot, "warn", "Member left", f"{member.mention} (`{member.id}`)", guild=member.guild,
            thumbnail=member.display_avatar.url))

    @app_commands.command(name="invites", description="See how many people someone invited")
    @app_commands.guild_only()
    async def invites_cmd(self, interaction: discord.Interaction, member: Optional[discord.Member] = None):
        member = member or interaction.user
        g = interaction.guild
        rows = await self.bot.db.fetchall("SELECT member_id FROM invites WHERE guild_id=? AND inviter_id=?", (g.id, member.id))
        here = sum(1 for r in rows if g.get_member(r["member_id"]))
        await interaction.response.send_message(embed=brand.embed(
            self.bot, "info", f"Invites • {member.display_name}", None, guild=g, thumbnail=member.display_avatar.url,
            fields=[("{members} Total invited", str(len(rows)), True), ("{online} Still here", str(here), True),
                    ("{offline} Left", str(len(rows) - here), True)]))

    @app_commands.command(name="invite-leaderboard", description="Top inviters")
    @app_commands.guild_only()
    async def invite_board(self, interaction: discord.Interaction):
        rows = await self.bot.db.fetchall(
            "SELECT inviter_id, COUNT(*) c FROM invites WHERE guild_id=? GROUP BY inviter_id ORDER BY c DESC LIMIT 10",
            (interaction.guild.id,))
        medals = ["🥇", "🥈", "🥉"]
        body = "\n".join(f"{medals[i] if i < 3 else f'`{i + 1}.`'} <@{r['inviter_id']}> — **{r['c']}**" for i, r in enumerate(rows)) \
            or "No tracked invites yet."
        await interaction.response.send_message(embed=brand.embed(
            self.bot, "premium", "Invite leaderboard", "{crown} " + body, guild=interaction.guild))

    # ------------------------------------------------------------------ logs
    @commands.Cog.listener()
    async def on_message_delete(self, message):
        if not message.guild or message.author.bot:
            return
        body = message.content[:1000] if message.content else "(no text)"
        if message.attachments:
            body += "\n" + "\n".join(a.filename for a in message.attachments)
        await brand.send_log(self.bot, message.guild, brand.embed(
            self.bot, "warn", "Message deleted", None, guild=message.guild, fields=[
                ("Author", f"{message.author.mention} (`{message.author.id}`)", True), ("Channel", message.channel.mention, True),
                ("Content", discord.utils.escape_markdown(body) or "—", False)]))

    @commands.Cog.listener()
    async def on_message_edit(self, before, after):
        if not after.guild or after.author.bot or before.content == after.content:
            return
        await brand.send_log(self.bot, after.guild, brand.embed(
            self.bot, "info", "Message edited", f"[Jump to message]({after.jump_url})", guild=after.guild, fields=[
                ("Author", after.author.mention, True), ("Channel", after.channel.mention, True),
                ("Before", discord.utils.escape_markdown(before.content[:900]) or "—", False),
                ("After", discord.utils.escape_markdown(after.content[:900]) or "—", False)]))

    @commands.Cog.listener()
    async def on_member_ban(self, guild, user):
        await brand.send_log(self.bot, guild, brand.embed(self.bot, "err", "Member banned", f"{user} (`{user.id}`)", guild=guild))

    @commands.Cog.listener()
    async def on_member_unban(self, guild, user):
        await brand.send_log(self.bot, guild, brand.embed(self.bot, "ok", "Member unbanned", f"{user} (`{user.id}`)", guild=guild))

    @commands.Cog.listener()
    async def on_member_update(self, before, after):
        if before.roles != after.roles:
            added = [r.mention for r in after.roles if r not in before.roles]
            removed = [r.mention for r in before.roles if r not in after.roles]
            fields = [("User", after.mention, True)]
            if added:
                fields.append(("Roles added", " ".join(added), True))
            if removed:
                fields.append(("Roles removed", " ".join(removed), True))
            await brand.send_log(self.bot, after.guild, brand.embed(self.bot, "info", "Roles changed", None, guild=after.guild, fields=fields))
        elif before.nick != after.nick:
            await brand.send_log(self.bot, after.guild, brand.embed(
                self.bot, "info", "Nickname changed", None, guild=after.guild, fields=[
                    ("User", after.mention, True), ("Before", before.nick or "—", True), ("After", after.nick or "—", True)]))

    @commands.Cog.listener()
    async def on_guild_channel_create(self, ch):
        await brand.send_log(self.bot, ch.guild, brand.embed(self.bot, "ok", "Channel created", f"{ch.mention} (`{ch.name}`)", guild=ch.guild))

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, ch):
        tcfg = await self.bot.db.cfg(ch.guild.id, "tickets", brand.TICKET_DEFAULTS)
        if tcfg["category_id"] and getattr(ch, "category_id", None) == tcfg["category_id"]:
            return                                                # ticket channels are logged by the ticket system
        await brand.send_log(self.bot, ch.guild, brand.embed(self.bot, "err", "Channel deleted", f"`#{ch.name}`", guild=ch.guild))

    @commands.Cog.listener()
    async def on_guild_role_create(self, role):
        await brand.send_log(self.bot, role.guild, brand.embed(self.bot, "ok", "Role created", f"{role.mention} (`{role.name}`)", guild=role.guild))

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role):
        await brand.send_log(self.bot, role.guild, brand.embed(self.bot, "err", "Role deleted", f"`{role.name}`", guild=role.guild))

    # ------------------------------------------------------------------ giveaways
    @giveaway.command(name="start", description="Start a giveaway")
    @app_commands.describe(prize="What you are giving away", duration="e.g. 30m, 2h, 1d", winners="How many winners",
                           channel="Where to post (default: here)")
    async def gw_start(self, interaction: discord.Interaction, prize: str, duration: str,
                       winners: app_commands.Range[int, 1, 20] = 1, channel: Optional[discord.TextChannel] = None):
        g = interaction.guild
        secs = brand.parse_duration(duration)
        if not secs or secs > 30 * 86400:
            return await interaction.response.send_message(embed=brand.embed(
                self.bot, "err", "Bad duration", "Use something like `30m`, `2h`, `1d` (max 30 days).", guild=g), ephemeral=True)
        ch = channel or interaction.channel
        ends = time.time() + secs
        em = self._gw_embed(g, prize, winners, ends, interaction.user.id)
        try:
            msg = await ch.send(embed=em, view=GiveawayView(self.bot, g))
        except discord.HTTPException as exc:
            return await interaction.response.send_message(embed=brand.embed(
                self.bot, "err", "Could not post", exc.text, guild=g), ephemeral=True)
        gid = await self.bot.db.execute(
            "INSERT INTO giveaways(guild_id,channel_id,message_id,prize,winners,ends_at,host_id) VALUES(?,?,?,?,?,?,?)",
            (g.id, ch.id, msg.id, prize, winners, ends, interaction.user.id))
        await interaction.response.send_message(embed=brand.embed(
            self.bot, "ok", "Giveaway started", f"ID `{gid}` • ends <t:{int(ends)}:R> • {msg.jump_url}", guild=g), ephemeral=True)

    def _gw_embed(self, guild, prize, winners, ends, host_id, entries=None, winner_ids=None, ended=False):
        if ended:
            who = " ".join(f"<@{w}>" for w in winner_ids) if winner_ids else "No valid entries"
            em = brand.embed(self.bot, "premium", f"GIVEAWAY ENDED • {prize}", None, guild=guild, fields=[
                ("{crown} Winners", who, False), ("{members} Entries", str(entries or 0), True),
                ("Hosted by", f"<@{host_id}>", True)])
        else:
            em = brand.embed(self.bot, "premium", f"GIVEAWAY • {prize}",
                             f"{{gift}} Press the button to enter!\n{brand.LINE}", guild=guild, fields=[
                                 ("{bell} Ends", f"<t:{int(ends)}:R>", True), ("{crown} Winners", str(winners), True),
                                 ("Hosted by", f"<@{host_id}>", True)])
        em.color = 0xEC4899
        return em

    async def _pick_winners(self, row):
        g = self.bot.get_guild(row["guild_id"])
        ids = [r["user_id"] for r in await self.bot.db.fetchall(
            "SELECT user_id FROM giveaway_entries WHERE giveaway_id=?", (row["id"],))]
        valid = [i for i in ids if g and g.get_member(i) and not g.get_member(i).bot]
        return ids, random.sample(valid, min(row["winners"], len(valid)))

    async def _finish(self, row, reroll=False):
        g = self.bot.get_guild(row["guild_id"])
        ch = g.get_channel(row["channel_id"]) if g else None
        ids, winners = await self._pick_winners(row)
        await self.bot.db.execute("UPDATE giveaways SET ended=1 WHERE id=?", (row["id"],))
        if not ch:
            return winners
        try:
            msg = await ch.fetch_message(row["message_id"])
            await msg.edit(embed=self._gw_embed(g, row["prize"], row["winners"], row["ends_at"], row["host_id"],
                                                entries=len(ids), winner_ids=winners, ended=True), view=None)
            if winners:
                text = f"{self.bot.emo.get('gift', g)} {'New winner(s)' if reroll else 'Congratulations'} " \
                       f"{' '.join(f'<@{w}>' for w in winners)}! You won **{row['prize']}**!"
            else:
                text = f"{self.bot.emo.get('cross', g)} No valid entries for **{row['prize']}**."
            await msg.reply(text, allowed_mentions=discord.AllowedMentions(users=True))
        except discord.HTTPException as exc:
            print(f"[giveaway] could not finish giveaway {row['id']}: {exc}")
        return winners

    @tasks.loop(seconds=15)
    async def giveaway_loop(self):
        rows = await self.bot.db.fetchall("SELECT * FROM giveaways WHERE ended=0 AND ends_at<=?", (time.time(),))
        for row in rows:
            try:
                await self._finish(row)
            except Exception as exc:
                print(f"[giveaway] error ending {row['id']}: {exc!r}")

    @giveaway_loop.before_loop
    async def _gw_ready(self):
        await self.bot.wait_until_ready()

    @giveaway.command(name="end", description="End a giveaway now")
    async def gw_end(self, interaction: discord.Interaction, giveaway_id: int):
        row = await self.bot.db.fetchone("SELECT * FROM giveaways WHERE id=? AND guild_id=?", (giveaway_id, interaction.guild.id))
        if not row or row["ended"]:
            return await interaction.response.send_message(embed=brand.embed(
                self.bot, "err", "Not found", "No running giveaway with that ID.", guild=interaction.guild), ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        await self._finish(row)
        await interaction.followup.send(embed=brand.embed(self.bot, "ok", "Ended", f"Giveaway `{giveaway_id}` finished.",
                                                          guild=interaction.guild), ephemeral=True)

    @giveaway.command(name="reroll", description="Pick new winners for an ended giveaway")
    async def gw_reroll(self, interaction: discord.Interaction, giveaway_id: int):
        row = await self.bot.db.fetchone("SELECT * FROM giveaways WHERE id=? AND guild_id=?", (giveaway_id, interaction.guild.id))
        if not row or not row["ended"]:
            return await interaction.response.send_message(embed=brand.embed(
                self.bot, "err", "Not found", "Only ended giveaways can be rerolled.", guild=interaction.guild), ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        await self._finish(row, reroll=True)
        await interaction.followup.send(embed=brand.embed(self.bot, "ok", "Rerolled", "New winner(s) announced.",
                                                          guild=interaction.guild), ephemeral=True)

    @giveaway.command(name="list", description="Running giveaways")
    async def gw_list(self, interaction: discord.Interaction):
        rows = await self.bot.db.fetchall("SELECT * FROM giveaways WHERE guild_id=? AND ended=0 ORDER BY ends_at",
                                          (interaction.guild.id,))
        body = "\n".join(f"`{r['id']}` • **{r['prize']}** • ends <t:{int(r['ends_at'])}:R> • <#{r['channel_id']}>" for r in rows) \
            or "No running giveaways."
        await interaction.response.send_message(embed=brand.embed(self.bot, "info", "Giveaways", body, guild=interaction.guild),
                                                ephemeral=True)

    # ------------------------------------------------------------------ status monitor
    @staticmethod
    async def _check(svc):
        """Returns (ok, latency_ms, error)."""
        t0 = time.monotonic()
        try:
            if svc["kind"] == "tcp":
                host, _, port = svc["target"].rpartition(":")
                _, writer = await asyncio.wait_for(asyncio.open_connection(host, int(port)), timeout=5)
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass
            else:
                url = svc["target"] if svc["target"].startswith("http") else "https://" + svc["target"]
                async with aiohttp.ClientSession() as s:
                    async with s.get(url, timeout=aiohttp.ClientTimeout(total=8), allow_redirects=True) as r:
                        if r.status >= 500:
                            return False, None, f"HTTP {r.status}"
            return True, int((time.monotonic() - t0) * 1000), None
        except asyncio.TimeoutError:
            return False, None, "timeout"
        except (OSError, ValueError, aiohttp.ClientError) as exc:
            return False, None, str(exc)[:80] or exc.__class__.__name__

    async def refresh_status(self, guild, cfg=None):
        cfg = cfg or await self.bot.db.cfg(guild.id, "status", STATUS_DEFAULTS)
        ch = guild.get_channel(cfg["channel_id"]) if cfg["channel_id"] else None
        if not ch:
            return
        services = await self.bot.db.fetchall("SELECT * FROM services WHERE guild_id=? ORDER BY id", (guild.id,))
        results = await asyncio.gather(*[self._check(s) for s in services]) if services else []
        lines, down, up, maint, flips = [], 0, 0, 0, []
        emo = self.bot.emo
        for svc, (ok, ms, err) in zip(services, results):
            if svc["maint"]:
                maint += 1
                lines.append(f"{emo.get('maintenance', guild)} **{svc['name']}** — maintenance")
                continue
            if ok:
                up += 1
                lines.append(f"{emo.get('online', guild)} **{svc['name']}** — `{ms}ms`")
            else:
                down += 1
                lines.append(f"{emo.get('offline', guild)} **{svc['name']}** — offline `{err}`")
            if bool(svc["last_ok"]) != ok:
                flips.append((svc["name"], ok))
                await self.bot.db.execute("UPDATE services SET last_ok=? WHERE id=?", (1 if ok else 0, svc["id"]))
        if not services:
            headline, color = "No services added yet. Use `/status add`.", 0x3B82F6
        elif down == 0:
            headline, color = f"{emo.get('check', guild)} **All systems operational**", 0x22C55E
        elif up == 0 and maint == 0:
            headline, color = f"{emo.get('cross', guild)} **Major outage**", 0xEF4444
        else:
            headline, color = f"{emo.get('warn', guild)} **Partial outage**", 0xF59E0B
        em = discord.Embed(title=f"{emo.get('globe', guild)} {cfg['title'] or brand.NAME + ' Network Status'}",
                           description=f"{headline}\n{brand.LINE}\n" + ("\n".join(lines) or "—"), color=color)
        em.add_field(name=f"{emo.get('bolt', guild)} Updated", value=f"<t:{int(time.time())}:R> • refreshes every minute", inline=False)
        em.set_footer(text=f"{brand.NAME} • live status")
        em.timestamp = discord.utils.utcnow()
        msg = None
        if cfg["message_id"]:
            try:
                msg = await ch.fetch_message(cfg["message_id"])
            except discord.HTTPException:
                msg = None
        try:
            if msg:
                await msg.edit(embed=em)
            else:
                new = await ch.send(embed=em)
                await self.bot.db.cfg_set(guild.id, "status", STATUS_DEFAULTS, message_id=new.id)
        except discord.HTTPException as exc:
            print(f"[status] {guild.name}: cannot update board: {exc}")
        for name, ok in flips:
            role = guild.get_role(cfg["alert_role_id"]) if cfg["alert_role_id"] else None
            text = (f"{emo.get('online', guild)} **{name}** is back online!" if ok
                    else f"{emo.get('offline', guild)} **{name}** went offline. Our team is on it.")
            try:
                await ch.send((role.mention + " " if role else "") + text, allowed_mentions=discord.AllowedMentions(roles=True))
            except discord.HTTPException as exc:
                print(f"[status] cannot send alert: {exc}")

    @tasks.loop(seconds=60)
    async def status_loop(self):
        for guild in self.bot.guilds:
            try:
                await self.refresh_status(guild)
            except Exception as exc:
                print(f"[status] error in {guild.name}: {exc!r}")

    @status_loop.before_loop
    async def _st_ready(self):
        await self.bot.wait_until_ready()

    @status.command(name="setup", description="Create the live status board in a channel")
    async def st_setup(self, interaction: discord.Interaction, channel: discord.TextChannel,
                       alert_role: Optional[discord.Role] = None, title: Optional[str] = None):
        g = interaction.guild
        await self.bot.db.cfg_set(g.id, "status", STATUS_DEFAULTS, channel_id=channel.id, message_id=None,
                                  alert_role_id=alert_role.id if alert_role else None, title=title)
        await interaction.response.defer(ephemeral=True)
        await self.refresh_status(g)
        await interaction.followup.send(embed=brand.embed(
            self.bot, "ok", "Status board created", f"Live board in {channel.mention}. Add services with `/status add`.",
            guild=g), ephemeral=True)

    @status.command(name="add", description="Monitor a node, panel or website")
    @app_commands.describe(name="Display name, e.g. Node-1 or Panel", kind="tcp = host:port, http = a URL",
                           target="host:port (tcp) or URL (http)")
    @app_commands.choices(kind=[app_commands.Choice(name="TCP port (host:port)", value="tcp"),
                                app_commands.Choice(name="Website / HTTP", value="http")])
    async def st_add(self, interaction: discord.Interaction, name: str, kind: app_commands.Choice[str], target: str):
        g = interaction.guild
        if kind.value == "tcp" and (":" not in target or not target.rpartition(":")[2].isdigit()):
            return await interaction.response.send_message(embed=brand.embed(
                self.bot, "err", "Bad target", "TCP targets look like `1.2.3.4:8080`.", guild=g), ephemeral=True)
        try:
            await self.bot.db.execute("INSERT INTO services(guild_id,name,kind,target) VALUES(?,?,?,?)",
                                      (g.id, name[:40], kind.value, target.strip()))
        except Exception:
            return await interaction.response.send_message(embed=brand.embed(
                self.bot, "err", "Already exists", f"A service named **{name}** exists. Remove it first.", guild=g), ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        await self.refresh_status(g)
        await interaction.followup.send(embed=brand.embed(self.bot, "ok", "Service added", f"**{name}** is now monitored.",
                                                          guild=g), ephemeral=True)

    @status.command(name="remove", description="Stop monitoring a service")
    async def st_remove(self, interaction: discord.Interaction, name: str):
        n = await self.bot.db.rowcount("DELETE FROM services WHERE guild_id=? AND name=?", (interaction.guild.id, name))
        await interaction.response.send_message(embed=brand.embed(
            self.bot, "ok" if n else "err", "Removed" if n else "Not found", name, guild=interaction.guild), ephemeral=True)
        if n:
            await self.refresh_status(interaction.guild)

    @status.command(name="maintenance", description="Mark a service as under maintenance (no offline alerts)")
    async def st_maint(self, interaction: discord.Interaction, name: str, enabled: bool):
        n = await self.bot.db.rowcount("UPDATE services SET maint=? WHERE guild_id=? AND name=?",
                                       (1 if enabled else 0, interaction.guild.id, name))
        await interaction.response.defer(ephemeral=True)
        if n:
            await self.refresh_status(interaction.guild)
        await interaction.followup.send(embed=brand.embed(
            self.bot, "ok" if n else "err", "Updated" if n else "Not found",
            f"**{name}** maintenance: {'on' if enabled else 'off'}" if n else "No service with that name.",
            guild=interaction.guild), ephemeral=True)

    @status.command(name="list", description="List monitored services")
    async def st_list(self, interaction: discord.Interaction):
        rows = await self.bot.db.fetchall("SELECT * FROM services WHERE guild_id=? ORDER BY id", (interaction.guild.id,))
        body = "\n".join(f"• **{r['name']}** — `{r['kind']}` `{r['target']}`" for r in rows) or "Nothing monitored yet."
        await interaction.response.send_message(embed=brand.embed(self.bot, "info", "Services", body, guild=interaction.guild),
                                                ephemeral=True)

    # ------------------------------------------------------------------ utility
    @app_commands.command(name="ping", description="Bot latency")
    async def ping(self, interaction: discord.Interaction):
        await interaction.response.send_message(embed=brand.embed(
            self.bot, "ok", "Pong!", f"{{wifi}} Gateway latency: **{round(self.bot.latency * 1000)}ms**", guild=interaction.guild))

    @app_commands.command(name="serverinfo", description="Server information")
    @app_commands.guild_only()
    async def serverinfo(self, interaction: discord.Interaction):
        g = interaction.guild
        bots = sum(1 for m in g.members if m.bot)
        await interaction.response.send_message(embed=brand.embed(
            self.bot, "premium", g.name, None, guild=g, thumbnail=g.icon.url if g.icon else None, fields=[
                ("{crown} Owner", f"<@{g.owner_id}>", True), ("{members} Members", f"{g.member_count} ({bots} bots)", True),
                ("{ticket} Channels", str(len(g.channels)), True), ("Roles", str(len(g.roles)), True),
                ("{diamond} Boosts", f"{g.premium_subscription_count} (tier {g.premium_tier})", True),
                ("Created", f"<t:{int(g.created_at.timestamp())}:R>", True)]))

    @app_commands.command(name="userinfo", description="Information about a member")
    @app_commands.guild_only()
    async def userinfo(self, interaction: discord.Interaction, member: Optional[discord.Member] = None):
        m = member or interaction.user
        roles = " ".join(r.mention for r in m.roles[1:][::-1][:12]) or "none"
        await interaction.response.send_message(embed=brand.embed(
            self.bot, "info", str(m), None, guild=interaction.guild, thumbnail=m.display_avatar.url, fields=[
                ("ID", f"`{m.id}`", True), ("Joined", f"<t:{int(m.joined_at.timestamp())}:R>" if m.joined_at else "?", True),
                ("Created", f"<t:{int(m.created_at.timestamp())}:R>", True), ("Roles", roles, False)]))

    @app_commands.command(name="help", description="What can this bot do?")
    async def help(self, interaction: discord.Interaction):
        g = interaction.guild
        await interaction.response.send_message(embed=brand.embed(
            self.bot, "premium", f"{brand.NAME} Bot", f"{{rocket}} All-in-one hosting community bot.\n{brand.LINE}", guild=g, fields=[
                ("{ticket} Tickets + AI", "`/ticket setup` • `/ticket panel` • `/ticket add` • `/ticket ai`", False),
                ("{megaphone} AI announcements", "`/ai announce` • `/ai ask` • `/ai status`", False),
                ("{shield} Protection", "`/automod status` • `/antinuke enable` • `/antinuke lockdown`", False),
                ("{crown} Moderation", "`/warn` `/timeout` `/kick` `/ban` `/purge` `/lock` `/slowmode`", False),
                ("{globe} Status board", "`/status setup` • `/status add` • `/status maintenance`", False),
                ("{gift} Community", "`/giveaway start` • `/invites` • `/invite-leaderboard` • `/setup welcome|verify|logs`", False),
                ("{star} Emojis", "`/emoji install` • `/emoji make` • `/emoji list`", False),
                ("{rocket} Hosting", "`/plans-list` • `/payments` • `/tos` • `/vouch` • `/vouches` • `/customer`", False),
                ("{stats} Levels", "`/rank` • `/leaderboard` • `/level role|settings`", False),
                ("{bolt} BOLTs economy", "`/daily` `/work` `/balance` `/pay` `/coinflip` `/slots` `/richest` `/shop list|buy`", False),
                ("{members} Engage", "`/poll create` • `/suggestion new` • `/remind` • `/afk` • `/avatar` • `/8ball` • `/roll`", False),
                ("{ticket} Staff tools", "`/plans add|post` • `/embed` • `/rolepanel create` • `/sticky set` • `/responder add` • `/starboard setup` • `/tempvc setup`", False)]))


async def setup(bot):
    await bot.add_cog(Community(bot))
