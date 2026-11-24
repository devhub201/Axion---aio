"""Birthdays (announcement + birthday role) and the counting game."""
import asyncio
import calendar
import datetime
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import brand

BD_DEFAULTS = {"channel_id": None, "role_id": None, "tz": 330, "message": None}
COUNT_DEFAULTS = {"channel_id": None, "count": 0, "last_user": None, "high": 0, "ruins": 0, "total": 0}
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]


def is_leap(y):
    return calendar.isleap(y)


class Birthdays(commands.Cog, name="Birthdays"):
    birthday = app_commands.Group(name="birthday", description="Birthdays", guild_only=True)

    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        self.birthday_loop.start()

    async def cog_unload(self):
        self.birthday_loop.cancel()

    async def local_today(self, guild_id):
        cfg = await self.bot.db.cfg(guild_id, "birthday", BD_DEFAULTS)
        now = discord.utils.utcnow() + datetime.timedelta(minutes=int(cfg["tz"]))
        return cfg, now.date()

    # ------------------------------------------------------------ commands
    @birthday.command(name="set", description="Set your birthday (day and month only)")
    @app_commands.choices(month=[app_commands.Choice(name=m, value=i + 1) for i, m in enumerate(MONTHS)])
    async def set_(self, interaction: discord.Interaction, day: app_commands.Range[int, 1, 31], month: app_commands.Choice[int]):
        g = interaction.guild
        if day > calendar.monthrange(2024, month.value)[1]:
            return await interaction.response.send_message(embed=brand.embed(
                self.bot, "err", "Invalid date", f"{month.name} does not have {day} days.", guild=g), ephemeral=True)
        await self.bot.db.execute("INSERT OR REPLACE INTO birthdays(guild_id,user_id,month,day) VALUES(?,?,?,?)",
                                  (g.id, interaction.user.id, month.value, day))
        await interaction.response.send_message(embed=brand.embed(
            self.bot, "ok", "Birthday saved", f"{{gift}} We will celebrate on **{day} {month.name}**!", guild=g), ephemeral=True)

    @birthday.command(name="remove", description="Remove your birthday")
    async def remove(self, interaction: discord.Interaction):
        n = await self.bot.db.rowcount("DELETE FROM birthdays WHERE guild_id=? AND user_id=?", (interaction.guild.id, interaction.user.id))
        await interaction.response.send_message(embed=brand.embed(
            self.bot, "ok" if n else "info", "Removed" if n else "Nothing to remove", "", guild=interaction.guild), ephemeral=True)

    @birthday.command(name="show", description="See someone's birthday")
    async def show(self, interaction: discord.Interaction, member: Optional[discord.Member] = None):
        m = member or interaction.user
        row = await self.bot.db.fetchone("SELECT month, day FROM birthdays WHERE guild_id=? AND user_id=?", (interaction.guild.id, m.id))
        text = f"{m.mention}'s birthday is **{row['day']} {MONTHS[row['month'] - 1]}** {{gift}}" if row else f"{m.mention} has not set a birthday."
        await interaction.response.send_message(embed=brand.embed(self.bot, "info", "Birthday", text, guild=interaction.guild),
                                                allowed_mentions=discord.AllowedMentions.none())

    @birthday.command(name="upcoming", description="Next birthdays in the server")
    async def upcoming(self, interaction: discord.Interaction):
        g = interaction.guild
        _, today = await self.local_today(g.id)
        rows = await self.bot.db.fetchall("SELECT user_id, month, day FROM birthdays WHERE guild_id=?", (g.id,))
        items = []
        for r in rows:
            if not g.get_member(r["user_id"]):
                continue
            day = min(r["day"], calendar.monthrange(today.year, r["month"])[1]) if r["month"] == 2 and r["day"] == 29 and not is_leap(today.year) else r["day"]
            nxt = datetime.date(today.year, r["month"], day)
            if nxt < today:
                y = today.year + 1
                d2 = 28 if (r["month"] == 2 and r["day"] == 29 and not is_leap(y)) else r["day"]
                nxt = datetime.date(y, r["month"], d2)
            items.append(((nxt - today).days, r["user_id"], nxt))
        items.sort()
        body = "\n".join(f"{'🎂 **TODAY**' if d == 0 else f'in {d}d'} • <@{u}> — {n.day} {MONTHS[n.month - 1]}" for d, u, n in items[:10]) \
            or "Nobody has set a birthday yet. Use `/birthday set`!"
        await interaction.response.send_message(embed=brand.embed(self.bot, "premium", "Upcoming birthdays", "{gift} " + body, guild=g),
                                                allowed_mentions=discord.AllowedMentions.none())

    @birthday.command(name="setup", description="Admin: where birthdays are announced")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(channel="Announcement channel", role="Optional role given for the birthday day",
                           timezone_hours="UTC offset in hours, e.g. 5.5 for India (default)", message="Custom text. Placeholders: {user} {server}")
    async def setup_(self, interaction: discord.Interaction, channel: discord.TextChannel, role: Optional[discord.Role] = None,
                     timezone_hours: app_commands.Range[float, -12.0, 14.0] = 5.5, message: Optional[str] = None):
        g = interaction.guild
        if role and (role >= g.me.top_role or role.managed or role.is_default()):
            return await interaction.response.send_message(embed=brand.embed(
                self.bot, "err", "Role problem", "Pick a role below my highest role.", guild=g), ephemeral=True)
        await self.bot.db.cfg_set(g.id, "birthday", BD_DEFAULTS, channel_id=channel.id, role_id=role.id if role else None,
                                  tz=int(timezone_hours * 60), message=message)
        await interaction.response.send_message(embed=brand.embed(
            self.bot, "ok", "Birthdays on", f"Channel: {channel.mention}\nRole: {role.mention if role else 'none'}\nTimezone: UTC{timezone_hours:+g}",
            guild=g), ephemeral=True)

    # ------------------------------------------------------------ daily loop
    @tasks.loop(minutes=5)
    async def birthday_loop(self):
        for g in self.bot.guilds:
            try:
                await self.run_guild(g)
            except Exception as exc:
                print(f"[birthday] {g.name}: {exc!r}")

    @birthday_loop.before_loop
    async def _ready(self):
        await self.bot.wait_until_ready()

    async def run_guild(self, g):
        cfg, today = await self.local_today(g.id)
        if not cfg["channel_id"] and not cfg["role_id"]:
            return
        pairs = [(today.month, today.day)]
        if (today.month, today.day) == (2, 28) and not is_leap(today.year):
            pairs.append((2, 29))
        ids = []
        for m, d in pairs:
            ids += [r["user_id"] for r in await self.bot.db.fetchall(
                "SELECT user_id FROM birthdays WHERE guild_id=? AND month=? AND day=?", (g.id, m, d))]
        done = await self.bot.db.get(g.id, "birthday_done", {})
        stamp = today.isoformat()
        role = g.get_role(cfg["role_id"]) if cfg["role_id"] else None
        ch = g.get_channel(cfg["channel_id"]) if cfg["channel_id"] else None
        changed = False
        for uid in ids:
            member = g.get_member(uid)
            if not member or done.get(str(uid)) == stamp:
                continue
            done[str(uid)] = stamp
            changed = True
            if role and role not in member.roles:
                try:
                    await member.add_roles(role, reason="Birthday")
                except discord.HTTPException as exc:
                    print(f"[birthday] cannot give role in {g.name}: {exc}")
            if ch:
                text = (cfg["message"] or "Everyone wish {user} a very happy birthday!").replace("{user}", member.mention).replace("{server}", g.name)
                try:
                    await ch.send(content=member.mention, embed=brand.embed(
                        self.bot, "premium", "Happy Birthday!", f"{{gift}} {text}\n{{star}} Have an amazing day!", guild=g,
                        thumbnail=member.display_avatar.url), allowed_mentions=discord.AllowedMentions(users=[member]))
                except discord.HTTPException as exc:
                    print(f"[birthday] cannot announce in {g.name}: {exc}")
        if changed:
            done = {k: v for k, v in done.items() if v == stamp}          # keep only today's marks
            await self.bot.db.set(g.id, "birthday_done", done)
        if role:
            for m in list(role.members):
                if m.id not in ids:
                    try:
                        await m.remove_roles(role, reason="Birthday is over")
                    except discord.HTTPException:
                        pass


class Counting(commands.Cog, name="Counting"):
    counting = app_commands.Group(name="counting", description="Counting game", guild_only=True)

    def __init__(self, bot):
        self.bot = bot
        self.locks = {}

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot or not message.guild:
            return
        db, gid = self.bot.db, message.guild.id
        cfg0 = await db.get(gid, "counting", None)
        if not cfg0 or message.channel.id != cfg0.get("channel_id"):
            return
        text = message.content.strip()
        if not text.isdigit() or len(text) > 12:
            return                                              # normal chat is allowed in the channel
        async with self.locks.setdefault(gid, asyncio.Lock()):
            cfg = await db.cfg(gid, "counting", COUNT_DEFAULTS)
            n = int(text)
            expected = cfg["count"] + 1
            same = message.author.id == cfg["last_user"] and cfg["count"] > 0
            if n == expected and not same:
                cfg["count"], cfg["last_user"] = n, message.author.id
                cfg["total"] += 1
                new_high = n > cfg["high"]
                cfg["high"] = max(cfg["high"], n)
                await db.set(gid, "counting", cfg)
                try:
                    await message.add_reaction("🏆" if new_high and n % 10 == 0 else "✅")
                except discord.HTTPException:
                    pass
                if n % 100 == 0:
                    try:
                        await message.channel.send(embed=brand.embed(
                            self.bot, "premium", f"{n} reached!", f"{{star}} Amazing teamwork, everyone! Keep going to **{n + 100}**!",
                            guild=message.guild, ts=False))
                    except discord.HTTPException:
                        pass
                return
            reason = "You cannot count twice in a row!" if same and n == expected else f"The next number was **{expected}**."
            lost = cfg["count"]
            cfg["count"], cfg["last_user"] = 0, None
            cfg["ruins"] += 1
            await db.set(gid, "counting", cfg)
            try:
                await message.add_reaction("❌")
            except discord.HTTPException:
                pass
            try:
                await message.channel.send(embed=brand.embed(
                    self.bot, "err", "Counting ruined!", f"{message.author.mention} broke the streak at **{lost}**.\n{reason}\n"
                    f"{{star}} High score: **{cfg['high']}** • Start again from **1**!", guild=message.guild, ts=False),
                    allowed_mentions=discord.AllowedMentions(users=[message.author]))
            except discord.HTTPException:
                pass

    @counting.command(name="setup", description="Admin: pick the counting channel")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setup_(self, interaction: discord.Interaction, channel: discord.TextChannel):
        g = interaction.guild
        perms = channel.permissions_for(g.me)
        if not (perms.send_messages and perms.add_reactions):
            return await interaction.response.send_message(embed=brand.embed(
                self.bot, "err", "Missing permission", f"I need **Send Messages** and **Add Reactions** in {channel.mention}.", guild=g), ephemeral=True)
        await self.bot.db.cfg_set(g.id, "counting", COUNT_DEFAULTS, channel_id=channel.id, count=0, last_user=None)
        await interaction.response.send_message(embed=brand.embed(
            self.bot, "ok", "Counting game ready", f"Start counting from **1** in {channel.mention}.\nRules: count one by one, nobody counts twice in a row.",
            guild=g), ephemeral=True)

    @counting.command(name="stats", description="Counting game stats")
    async def stats(self, interaction: discord.Interaction):
        cfg = await self.bot.db.cfg(interaction.guild.id, "counting", COUNT_DEFAULTS)
        if not cfg["channel_id"]:
            return await interaction.response.send_message(embed=brand.embed(
                self.bot, "err", "Not set up", "An admin must run `/counting setup`.", guild=interaction.guild), ephemeral=True)
        await interaction.response.send_message(embed=brand.embed(
            self.bot, "premium", "Counting stats", None, guild=interaction.guild, fields=[
                ("{stats} Current", str(cfg["count"]), True), ("{crown} High score", str(cfg["high"]), True),
                ("{cross} Times ruined", str(cfg["ruins"]), True), ("{check} Total correct", str(cfg["total"]), True),
                ("Channel", f"<#{cfg['channel_id']}>", True)]))

    @counting.command(name="reset", description="Admin: reset the count to 0")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def reset(self, interaction: discord.Interaction):
        await self.bot.db.cfg_set(interaction.guild.id, "counting", COUNT_DEFAULTS, count=0, last_user=None)
        await interaction.response.send_message(embed=brand.embed(
            self.bot, "ok", "Reset", "The count is back to 0.", guild=interaction.guild), ephemeral=True)


async def setup(bot):
    await bot.add_cog(Birthdays(bot))
    await bot.add_cog(Counting(bot))
