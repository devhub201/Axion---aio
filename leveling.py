"""XP / levels with a rank card image, level-up announcements and level roles."""
import asyncio
import io
import random
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from PIL import Image, ImageDraw

import brand
import icons

DEFAULTS = {"enabled": True, "channel_id": None, "multiplier": 1.0, "no_xp_channels": [], "announce": True}


def xp_for(level):
    """XP needed to go from `level` to `level + 1`."""
    return 5 * level * level + 50 * level + 100


def progress(total_xp):
    level, xp = 0, total_xp
    while xp >= xp_for(level):
        xp -= xp_for(level)
        level += 1
    return level, xp, xp_for(level)


def rank_card(name, avatar_bytes, level, cur, need, rank, total_msgs):
    W, H = 760, 220
    im = Image.new("RGBA", (W, H), (15, 23, 42, 255))
    d = ImageDraw.Draw(im)
    for i in range(W):                                      # violet -> cyan gradient strip
        t = i / W
        col = (int(139 + (34 - 139) * t), int(92 + (211 - 92) * t), int(246 + (238 - 246) * t))
        d.line([(i, 0), (i, 6)], fill=col)
    d.rounded_rectangle((14, 18, W - 14, H - 14), 22, fill=(30, 41, 59, 255))
    try:
        av = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA").resize((128, 128))
        mask = Image.new("L", (128, 128), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, 127, 127), fill=255)
        im.paste(av, (40, 46), mask)
        d.ellipse((36, 42, 172, 178), outline=(139, 92, 246, 255), width=4)
    except Exception:
        d.ellipse((40, 46, 168, 174), fill=(71, 85, 105, 255))
    big, mid, small = icons._font(34), icons._font(22), icons._font(17)
    d.text((196, 40), name[:22], font=big, fill=(248, 250, 252))
    d.text((196, 88), f"Rank #{rank}", font=mid, fill=(56, 189, 248))
    lv = f"LEVEL {level}"
    d.text((W - 40 - d.textlength(lv, font=big), 38), lv, font=big, fill=(250, 204, 21))
    d.text((196, 124), f"{cur:,} / {need:,} XP   •   {total_msgs:,} messages", font=small, fill=(148, 163, 184))
    bx0, bx1, by0, by1 = 196, W - 40, 156, 182
    d.rounded_rectangle((bx0, by0, bx1, by1), 13, fill=(15, 23, 42, 255))
    frac = max(0.02, min(1.0, cur / need))
    fw = int((bx1 - bx0) * frac)
    bar = Image.new("RGBA", (bx1 - bx0, by1 - by0), (0, 0, 0, 0))
    bd = ImageDraw.Draw(bar)
    for x in range(fw):
        t = x / max(1, bx1 - bx0)
        bd.line([(x, 0), (x, by1 - by0)], fill=(int(139 + (34 - 139) * t), int(92 + (211 - 92) * t), int(246 + (238 - 246) * t), 255))
    m = Image.new("L", bar.size, 0)
    ImageDraw.Draw(m).rounded_rectangle((0, 0, fw, by1 - by0), 13, fill=255)
    im.paste(bar, (bx0, by0), m)
    buf = io.BytesIO()
    im.convert("RGB").save(buf, "PNG")
    buf.seek(0)
    return buf


class Leveling(commands.Cog, name="Leveling"):
    level = app_commands.Group(name="level", description="Leveling settings",
                               default_permissions=discord.Permissions(manage_guild=True), guild_only=True)

    def __init__(self, bot):
        self.bot = bot
        self.cool = {}

    async def cfg(self, gid):
        return await self.bot.db.cfg(gid, "leveling", DEFAULTS)

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot or not message.guild or not message.content:
            return
        g = message.guild
        key = (g.id, message.author.id)
        now = time.monotonic()
        if now - self.cool.get(key, 0) < 60:
            return
        cfg = await self.cfg(g.id)
        if not cfg["enabled"] or message.channel.id in cfg["no_xp_channels"] or len(message.content) < 4:
            return
        self.cool[key] = now
        gain = max(1, int(random.randint(15, 25) * float(cfg["multiplier"])))
        db = self.bot.db
        await db.execute(
            "INSERT INTO levels(guild_id,user_id,xp,msgs) VALUES(?,?,?,1) "
            "ON CONFLICT(guild_id,user_id) DO UPDATE SET xp=xp+?, msgs=msgs+1", (g.id, message.author.id, gain, gain))
        row = await db.fetchone("SELECT xp, level FROM levels WHERE guild_id=? AND user_id=?", (g.id, message.author.id))
        new_level = progress(row["xp"])[0]
        if new_level > row["level"]:
            await db.execute("UPDATE levels SET level=? WHERE guild_id=? AND user_id=?", (new_level, g.id, message.author.id))
            await self.level_up(message, new_level, cfg)

    async def level_up(self, message, level, cfg):
        g, m = message.guild, message.author
        roles = await self.bot.db.get(g.id, "level_roles", {})
        gained = []
        for lv, rid in roles.items():
            role = g.get_role(rid)
            if role and int(lv) <= level and role not in m.roles:
                try:
                    await m.add_roles(role, reason=f"Reached level {level}")
                    gained.append(role.mention)
                except discord.HTTPException as exc:
                    print(f"[level] cannot give {role.name}: {exc}")
        if not cfg["announce"]:
            return
        ch = g.get_channel(cfg["channel_id"]) if cfg["channel_id"] else message.channel
        text = f"{{star}} {m.mention} reached **level {level}**!"
        if gained:
            text += f"\n{{crown}} New role: {' '.join(gained)}"
        try:
            await ch.send(embed=brand.embed(self.bot, "premium", "Level up!", text, guild=g, ts=False,
                                            thumbnail=m.display_avatar.url),
                          allowed_mentions=discord.AllowedMentions(users=[m]))
        except discord.HTTPException:
            pass

    async def rank_of(self, gid, uid):
        row = await self.bot.db.fetchone(
            "SELECT COUNT(*) + 1 r FROM levels WHERE guild_id=? AND xp > (SELECT COALESCE(xp,0) FROM levels WHERE guild_id=? AND user_id=?)",
            (gid, gid, uid))
        return row["r"]

    @app_commands.command(name="rank", description="Show your rank card")
    @app_commands.guild_only()
    async def rank(self, interaction: discord.Interaction, member: Optional[discord.Member] = None):
        m = member or interaction.user
        await interaction.response.defer()
        row = await self.bot.db.fetchone("SELECT xp, msgs FROM levels WHERE guild_id=? AND user_id=?", (interaction.guild.id, m.id))
        xp, msgs = (row["xp"], row["msgs"]) if row else (0, 0)
        lv, cur, need = progress(xp)
        rank = await self.rank_of(interaction.guild.id, m.id)
        try:
            av = await m.display_avatar.replace(size=256, format="png").read()
        except discord.HTTPException:
            av = b""
        buf = await asyncio.to_thread(rank_card, m.display_name, av, lv, cur, need, rank, msgs)
        await interaction.followup.send(file=discord.File(buf, "rank.png"))

    @app_commands.command(name="leaderboard", description="Top members by XP")
    @app_commands.guild_only()
    async def leaderboard(self, interaction: discord.Interaction):
        rows = await self.bot.db.fetchall("SELECT user_id, xp FROM levels WHERE guild_id=? ORDER BY xp DESC LIMIT 10",
                                          (interaction.guild.id,))
        medals = ["🥇", "🥈", "🥉"]
        body = "\n".join(f"{medals[i] if i < 3 else f'`{i + 1}.`'} <@{r['user_id']}> — level **{progress(r['xp'])[0]}** • {r['xp']:,} XP"
                         for i, r in enumerate(rows)) or "Nobody has XP yet. Start chatting!"
        await interaction.response.send_message(embed=brand.embed(self.bot, "premium", "XP leaderboard", "{crown} " + body,
                                                                  guild=interaction.guild))

    @level.command(name="settings", description="Configure leveling")
    @app_commands.describe(enabled="Turn leveling on/off", announce="Announce level-ups", channel="Level-up channel (default: same channel)",
                           multiplier="XP multiplier, e.g. 2 for double XP")
    async def settings(self, interaction: discord.Interaction, enabled: Optional[bool] = None, announce: Optional[bool] = None,
                       channel: Optional[discord.TextChannel] = None, multiplier: Optional[app_commands.Range[float, 0.1, 10.0]] = None):
        ch = {}
        if enabled is not None:
            ch["enabled"] = enabled
        if announce is not None:
            ch["announce"] = announce
        if channel:
            ch["channel_id"] = channel.id
        if multiplier is not None:
            ch["multiplier"] = multiplier
        cfg = await self.bot.db.cfg_set(interaction.guild.id, "leveling", DEFAULTS, **ch)
        where = "<#%d>" % cfg["channel_id"] if cfg["channel_id"] else "same as message"
        await interaction.response.send_message(embed=brand.embed(
            self.bot, "ok", "Leveling", f"Enabled: **{cfg['enabled']}** • Announce: **{cfg['announce']}** • "
            f"Multiplier: **x{cfg['multiplier']}** • Channel: {where}", guild=interaction.guild), ephemeral=True)

    @level.command(name="role", description="Give a role automatically at a level")
    @app_commands.choices(action=[app_commands.Choice(name="add", value="add"), app_commands.Choice(name="remove", value="remove"),
                                  app_commands.Choice(name="list", value="list")])
    async def role(self, interaction: discord.Interaction, action: app_commands.Choice[str], level: Optional[app_commands.Range[int, 1, 200]] = None,
                   role: Optional[discord.Role] = None):
        g = interaction.guild
        roles = await self.bot.db.get(g.id, "level_roles", {})
        if action.value == "list":
            body = "\n".join(f"Level **{k}** → <@&{v}>" for k, v in sorted(roles.items(), key=lambda x: int(x[0]))) or "No level roles."
            return await interaction.response.send_message(embed=brand.embed(self.bot, "info", "Level roles", body, guild=g), ephemeral=True)
        if not level:
            return await interaction.response.send_message(embed=brand.embed(self.bot, "err", "Missing level", "Give a `level`.", guild=g), ephemeral=True)
        if action.value == "add":
            if not role or role >= g.me.top_role:
                return await interaction.response.send_message(embed=brand.embed(
                    self.bot, "err", "Bad role", "Pick a role that is below my highest role.", guild=g), ephemeral=True)
            roles[str(level)] = role.id
        else:
            roles.pop(str(level), None)
        await self.bot.db.set(g.id, "level_roles", roles)
        await interaction.response.send_message(embed=brand.embed(self.bot, "ok", "Level roles updated", f"{len(roles)} reward(s) set.", guild=g), ephemeral=True)

    @level.command(name="set", description="Set a member's XP level (admin)")
    async def set_level(self, interaction: discord.Interaction, member: discord.Member, level: app_commands.Range[int, 0, 500]):
        xp = sum(xp_for(i) for i in range(level))
        await self.bot.db.execute(
            "INSERT INTO levels(guild_id,user_id,xp,level) VALUES(?,?,?,?) ON CONFLICT(guild_id,user_id) DO UPDATE SET xp=?, level=?",
            (interaction.guild.id, member.id, xp, level, xp, level))
        await interaction.response.send_message(embed=brand.embed(
            self.bot, "ok", "Level set", f"{member.mention} is now level **{level}**.", guild=interaction.guild), ephemeral=True)

    @level.command(name="ignore", description="Toggle XP in a channel")
    async def ignore(self, interaction: discord.Interaction, channel: discord.TextChannel):
        cfg = await self.cfg(interaction.guild.id)
        lst = set(cfg["no_xp_channels"])
        on = channel.id not in lst
        (lst.add if on else lst.discard)(channel.id)
        await self.bot.db.cfg_set(interaction.guild.id, "leveling", DEFAULTS, no_xp_channels=list(lst))
        await interaction.response.send_message(embed=brand.embed(
            self.bot, "ok", "Updated", f"{channel.mention}: XP {'disabled' if on else 'enabled'}.", guild=interaction.guild), ephemeral=True)


async def setup(bot):
    await bot.add_cog(Leveling(bot))
