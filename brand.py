"""Shared branding helpers: premium embeds, colours, logging, staff checks, duration parsing."""
import os
import re

import discord

NAME = os.getenv("BRAND_NAME", "AxionHost")
LINE = "━━━━━━━━━━━━━━━━━━━━━━"

COLORS = {
    "ok": 0x22C55E, "err": 0xEF4444, "warn": 0xF59E0B,
    "info": 0x3B82F6, "premium": 0x8B5CF6, "ai": 0x06B6D4,
}
ICONS = {"ok": "check", "err": "cross", "warn": "warn", "info": "globe", "premium": "diamond", "ai": "robot"}

TICKET_DEFAULTS = {
    "category_id": None, "log_id": None, "staff_role_id": None, "max_open": 2, "ai": True,
}


def embed(bot, kind, title=None, desc=None, *, guild=None, fields=None, footer=None,
          ts=True, thumbnail=None, image=None):
    """Build a branded embed. {emoji_name} placeholders in title/desc/fields become custom emojis."""
    emo = bot.emo
    head = f"{emo.get(ICONS[kind], guild)} {emo.fmt(title, guild)}" if title else None
    em = discord.Embed(title=head[:256] if head else None,
                       description=emo.fmt(desc, guild)[:4000] if desc else None,
                       color=COLORS[kind])
    for name, value, inline in fields or []:
        em.add_field(name=emo.fmt(name, guild)[:256], value=emo.fmt(value, guild)[:1024], inline=inline)
    em.set_footer(text=footer or f"{NAME} • Premium Hosting")
    if ts:
        em.timestamp = discord.utils.utcnow()
    if thumbnail:
        em.set_thumbnail(url=thumbnail)
    if image:
        em.set_image(url=image)
    return em


async def send_log(bot, guild, em, *, file=None):
    """Send an embed to the configured log channel. Failures are printed, never silent."""
    cid = await bot.db.get(guild.id, "log_channel")
    if not cid:
        return False
    ch = guild.get_channel(cid)
    if ch is None:
        try:
            ch = await guild.fetch_channel(cid)
        except discord.HTTPException as exc:
            print(f"[log] cannot fetch log channel {cid} in {guild.id}: {exc}")
            return False
    try:
        await ch.send(embed=em, file=file) if file else await ch.send(embed=em)
        return True
    except discord.HTTPException as exc:
        print(f"[log] cannot send to log channel {cid} in {guild.id}: {exc}")
        return False


async def staff_role_id(bot, guild_id):
    cfg = await bot.db.cfg(guild_id, "tickets", TICKET_DEFAULTS)
    return cfg.get("staff_role_id")


async def is_staff(bot, member):
    if member.guild_permissions.manage_messages or member.guild_permissions.administrator:
        return True
    rid = await staff_role_id(bot, member.guild.id)
    return bool(rid and any(r.id == rid for r in member.roles))


_DUR = re.compile(r"(\d+)\s*([smhdw])", re.I)
_MULT = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_duration(text):
    """'1h30m' -> 5400. Returns None if nothing valid was found."""
    total = 0
    found = False
    for num, unit in _DUR.findall(text or ""):
        total += int(num) * _MULT[unit.lower()]
        found = True
    return total if found and total > 0 else None


def human_duration(seconds):
    seconds = int(seconds)
    parts = []
    for label, size in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        if seconds >= size:
            parts.append(f"{seconds // size}{label}")
            seconds %= size
    return " ".join(parts) or "0s"
