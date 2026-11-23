"""Custom emoji manager: the bot draws its own animated emojis (icons.py) and uploads them."""
import asyncio
import re

import discord
from discord import app_commands
from discord.ext import commands

import brand
import icons

PREFIX = "ax_"
_TOKEN = re.compile(r"\{(\w+)\}|:(\w+):")


class EmojiManager:
    """bot.emo — resolves {rocket} style placeholders to the guild's custom emojis."""

    def __init__(self, bot):
        self.bot = bot

    def _find(self, name, guild=None):
        full = name if name.startswith(PREFIX) else PREFIX + name
        e = None
        if guild is not None:
            e = discord.utils.get(guild.emojis, name=full)
        if e is None:
            e = discord.utils.get(self.bot.emojis, name=full)
        return e

    def get(self, name, guild=None):
        e = self._find(name, guild)
        if e:
            return str(e)
        return icons.FALLBACK.get(name, "▫️")

    def partial(self, name, guild=None):
        """For select options / buttons: PartialEmoji if installed, unicode otherwise."""
        e = self._find(name, guild)
        if e:
            return discord.PartialEmoji(name=e.name, id=e.id, animated=e.animated)
        return icons.FALLBACK.get(name, "▫️")

    def fmt(self, text, guild=None):
        """Replace {name} / :name: with custom emojis (only for names we know)."""
        if not text:
            return text or ""

        def repl(m):
            name = m.group(1) or m.group(2)
            if name in icons.ICONS or self._find(name, guild):
                return self.get(name, guild)
            return m.group(0)
        return _TOKEN.sub(repl, text)

    def has(self, guild):
        return self._find("rocket", guild) is not None


async def install_emojis(bot, guild, overwrite=False, progress=None):
    """Draw + upload every icon. Returns (created, skipped, failed[(name, reason)])."""
    created, skipped, failed = [], [], []
    existing = {e.name: e for e in guild.emojis}
    todo = []
    for name in icons.ICONS:
        full = PREFIX + name
        if full in existing and not overwrite:
            skipped.append(name)
        else:
            todo.append(name)

    animated_used = sum(1 for e in guild.emojis if e.animated)
    free = guild.emoji_limit - animated_used
    if len(todo) > free:
        for name in todo[free:]:
            failed.append((name, f"no free animated emoji slots (limit {guild.emoji_limit}); boost the server or delete some"))
        todo = todo[:max(free, 0)]

    rendered = await asyncio.to_thread(lambda: {n: icons.render(n) for n in todo})
    for i, name in enumerate(todo, 1):
        full = PREFIX + name
        try:
            if full in existing:
                await existing[full].delete(reason=f"{brand.NAME} emoji refresh")
            await guild.create_custom_emoji(name=full, image=rendered[name], reason=f"{brand.NAME} icon pack")
            created.append(name)
        except discord.Forbidden:
            failed.append((name, "missing 'Manage Expressions' permission"))
            break
        except discord.HTTPException as exc:
            failed.append((name, f"{exc.status}: {exc.text[:120]}"))
            if exc.status == 429:
                await asyncio.sleep(10)
        if progress and i % 7 == 0:
            await progress(i, len(todo))
        await asyncio.sleep(1.3)     # stay below Discord's emoji-create rate limit
    return created, skipped, failed


class EmojiCog(commands.Cog, name="Emojis"):
    emoji = app_commands.Group(
        name="emoji", description="Create and manage the server's custom animated emojis",
        default_permissions=discord.Permissions(manage_expressions=True), guild_only=True)

    def __init__(self, bot):
        self.bot = bot
        self._installing = set()

    @commands.Cog.listener()
    async def on_ready(self):
        if not self.bot.auto_emojis:
            return
        for guild in self.bot.guilds:
            await self._auto_install(guild)

    @commands.Cog.listener()
    async def on_guild_join(self, guild):
        if self.bot.auto_emojis:
            await self._auto_install(guild)

    async def _auto_install(self, guild):
        if self.bot.emo.has(guild) or guild.id in self._installing:
            return
        if not guild.me.guild_permissions.manage_expressions:
            print(f"[emoji] {guild.name}: missing Manage Expressions, skipping auto-install")
            return
        self._installing.add(guild.id)
        try:
            print(f"[emoji] {guild.name}: creating {len(icons.ICONS)} animated emojis...")
            created, _, failed = await install_emojis(self.bot, guild)
            print(f"[emoji] {guild.name}: created {len(created)}, failed {len(failed)}")
            for name, why in failed:
                print(f"[emoji]   {name}: {why}")
        finally:
            self._installing.discard(guild.id)

    # ------------------------------------------------------------------ commands
    @emoji.command(name="install", description="Draw and upload the full AxionHost animated icon pack")
    @app_commands.describe(overwrite="Replace icons that already exist")
    async def install(self, interaction: discord.Interaction, overwrite: bool = False):
        guild = interaction.guild
        if guild.id in self._installing:
            return await interaction.response.send_message(
                embed=brand.embed(self.bot, "warn", "Already running", "An install is already in progress here.", guild=guild),
                ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        self._installing.add(guild.id)
        try:
            await interaction.followup.send(embed=brand.embed(
                self.bot, "info", "Drawing icons...",
                f"Creating **{len(icons.ICONS)}** animated emojis. This takes about a minute.", guild=guild), ephemeral=True)
            created, skipped, failed = await install_emojis(self.bot, guild, overwrite=overwrite)
        finally:
            self._installing.discard(guild.id)
        kind = "ok" if not failed else ("warn" if created else "err")
        fields = [("Created", f"{len(created)}", True), ("Skipped (already there)", f"{len(skipped)}", True),
                  ("Failed", f"{len(failed)}", True)]
        desc = " ".join(self.bot.emo.get(n, guild) for n in created[:28]) or "Nothing new was created."
        if failed:
            desc += "\n\n**Problems:**\n" + "\n".join(f"• `{n}` — {w}" for n, w in failed[:8])
        await interaction.followup.send(embed=brand.embed(self.bot, kind, "Emoji pack install", desc,
                                                          guild=guild, fields=fields), ephemeral=True)

    @emoji.command(name="list", description="Show every AxionHost emoji and the placeholder to use in embeds")
    async def list_(self, interaction: discord.Interaction):
        guild = interaction.guild
        lines = []
        for name, what in icons.DESCRIPTIONS.items():
            lines.append(f"{self.bot.emo.get(name, guild)} `{{{name}}}` — {what}")
        extra = [e for e in guild.emojis if e.name.startswith(PREFIX) and e.name[len(PREFIX):] not in icons.ICONS]
        if extra:
            lines.append("\n**Custom made:** " + " ".join(f"{e} `{{{e.name[len(PREFIX):]}}}`" for e in extra[:20]))
        installed = "✅ installed" if self.bot.emo.has(guild) else "⚠️ not installed yet — run `/emoji install`"
        em = brand.embed(self.bot, "premium", "AxionHost emoji pack",
                         f"Status: {installed}\nUse the placeholders in AI prompts or embeds.\n\n" + "\n".join(lines),
                         guild=guild)
        await interaction.response.send_message(embed=em, ephemeral=True)

    @emoji.command(name="make", description="Let the bot draw a brand-new animated emoji")
    @app_commands.describe(name="Emoji name (letters/numbers)", shape="Animation style",
                           color="Hex colour like #ff3366", text="Optional 1-3 characters inside the badge")
    @app_commands.choices(shape=[app_commands.Choice(name=s, value=s) for s in icons.SHAPES])
    async def make(self, interaction: discord.Interaction, name: str, shape: app_commands.Choice[str],
                   color: str = "#8b5cf6", text: str = ""):
        guild = interaction.guild
        clean = re.sub(r"[^a-z0-9_]", "", name.lower())[:24]
        if len(clean) < 2:
            return await interaction.response.send_message(
                embed=brand.embed(self.bot, "err", "Bad name", "Use at least 2 letters/numbers.", guild=guild), ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        data = await asyncio.to_thread(icons.make_custom, shape.value, color, text)
        try:
            e = await guild.create_custom_emoji(name=PREFIX + clean, image=data, reason=f"/emoji make by {interaction.user}")
        except discord.HTTPException as exc:
            return await interaction.followup.send(embed=brand.embed(
                self.bot, "err", "Could not create emoji", f"{exc.status}: {exc.text}", guild=guild), ephemeral=True)
        await interaction.followup.send(embed=brand.embed(
            self.bot, "ok", "Emoji created", f"{e} — use it as `{{{clean}}}` in AI prompts and embeds.", guild=guild), ephemeral=True)

    @emoji.command(name="add", description="Upload your own image/GIF as an emoji")
    async def add(self, interaction: discord.Interaction, name: str, image: discord.Attachment):
        guild = interaction.guild
        clean = re.sub(r"[^a-z0-9_]", "", name.lower())[:24]
        if len(clean) < 2:
            return await interaction.response.send_message(
                embed=brand.embed(self.bot, "err", "Bad name", "Use at least 2 letters/numbers.", guild=guild), ephemeral=True)
        if image.size > 256 * 1024:
            return await interaction.response.send_message(
                embed=brand.embed(self.bot, "err", "File too big", "Discord emojis must be under 256 KB.", guild=guild), ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        try:
            e = await guild.create_custom_emoji(name=PREFIX + clean, image=await image.read(), reason=f"/emoji add by {interaction.user}")
        except discord.HTTPException as exc:
            return await interaction.followup.send(embed=brand.embed(
                self.bot, "err", "Could not create emoji", f"{exc.status}: {exc.text}", guild=guild), ephemeral=True)
        await interaction.followup.send(embed=brand.embed(
            self.bot, "ok", "Emoji added", f"{e} — placeholder `{{{clean}}}`.", guild=guild), ephemeral=True)

    @emoji.command(name="remove", description="Delete one of the bot's emojis")
    async def remove(self, interaction: discord.Interaction, name: str):
        guild = interaction.guild
        clean = name.lower().removeprefix(PREFIX)
        e = discord.utils.get(guild.emojis, name=PREFIX + clean)
        if not e:
            return await interaction.response.send_message(
                embed=brand.embed(self.bot, "err", "Not found", f"No emoji named `{PREFIX}{clean}`.", guild=guild), ephemeral=True)
        await e.delete(reason=f"/emoji remove by {interaction.user}")
        await interaction.response.send_message(
            embed=brand.embed(self.bot, "ok", "Removed", f"`{PREFIX}{clean}` deleted.", guild=guild), ephemeral=True)


async def setup(bot):
    if not hasattr(bot, "emo"):
        bot.emo = EmojiManager(bot)
    await bot.add_cog(EmojiCog(bot))
