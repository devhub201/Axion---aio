#!/usr/bin/env python3
"""AxionHost all-in-one Discord bot — entry point."""
import asyncio
import logging
import os
import sys
import traceback


def load_env(path=".env"):
    """Tiny .env loader (no extra dependency)."""
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()

import discord  # noqa: E402
from discord import app_commands  # noqa: E402
from discord.ext import commands  # noqa: E402

import brand  # noqa: E402
from db import Database  # noqa: E402
from emojis import EmojiManager  # noqa: E402

EXTENSIONS = ["emojis", "ai", "tickets", "automod", "antinuke", "community", "leveling", "economy", "utility", "hosting"]


class AxionBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True            # welcome, invites, anti-raid, logs   (enable in the Developer Portal)
        intents.message_content = True    # AutoMod + AI ticket replies         (enable in the Developer Portal)
        owner_ids = {int(x) for x in os.getenv("OWNER_IDS", "").replace(" ", "").split(",") if x.isdigit()}
        super().__init__(command_prefix=commands.when_mentioned, intents=intents, owner_ids=owner_ids,
                         help_command=None, allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True))
        self.db = Database(os.getenv("DB_PATH", "data/axionhost.db"))
        self.emo = EmojiManager(self)
        self.auto_emojis = os.getenv("AUTO_EMOJIS", "1") != "0"
        self.guild_id = int(os.getenv("GUILD_ID", "0") or 0)

    async def setup_hook(self):
        for ext in EXTENSIONS:
            try:
                await self.load_extension(ext)
                print(f"[boot] loaded {ext}")
            except Exception:
                print(f"[boot] FAILED to load {ext}:")
                traceback.print_exc()
                raise
        self.tree.on_error = self.on_tree_error
        if self.guild_id:
            guild = discord.Object(id=self.guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            print(f"[boot] synced {len(synced)} commands to guild {self.guild_id} (instant)")
        else:
            synced = await self.tree.sync()
            print(f"[boot] synced {len(synced)} global commands (can take a few minutes to appear)")

    async def on_ready(self):
        print(f"[boot] online as {self.user} in {len(self.guilds)} server(s)")
        await self.change_presence(activity=discord.Activity(type=discord.ActivityType.watching,
                                                             name=f"{brand.NAME} • /help"))

    async def on_tree_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        err = getattr(error, "original", error)
        if isinstance(error, app_commands.MissingPermissions):
            text = "You need: **" + ", ".join(p.replace("_", " ") for p in error.missing_permissions) + "**."
        elif isinstance(error, app_commands.CommandOnCooldown):
            text = f"Try again in {error.retry_after:.0f}s."
        elif isinstance(error, app_commands.CheckFailure):
            text = "You are not allowed to use this command."
        elif isinstance(err, discord.Forbidden):
            text = "Discord blocked that: I am missing a permission or my role is too low. Move my role up and check channel permissions."
        else:
            print(f"[error] /{interaction.command.qualified_name if interaction.command else '?'}:")
            traceback.print_exception(type(err), err, err.__traceback__)
            text = f"Something went wrong: `{err.__class__.__name__}: {str(err)[:200]}`"
        em = brand.embed(self, "err", "Command failed", text, guild=interaction.guild)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(embed=em, ephemeral=True)
            else:
                await interaction.response.send_message(embed=em, ephemeral=True)
        except discord.HTTPException:
            pass


def main():
    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token or token.startswith("PUT_"):
        sys.exit("DISCORD_TOKEN is missing. Copy .env.example to .env and paste your bot token.")
    logging.basicConfig(level=logging.WARNING)
    bot = AxionBot()
    try:
        bot.run(token, log_handler=None)
    except discord.PrivilegedIntentsRequired:
        sys.exit("Enable SERVER MEMBERS INTENT and MESSAGE CONTENT INTENT in the Developer Portal → Bot page, then restart.")
    except discord.LoginFailure:
        sys.exit("Discord rejected the token. Reset it in the Developer Portal and update .env.")


if __name__ == "__main__":
    main()
