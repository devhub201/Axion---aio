"""BOLTs economy: daily, work, pay, gambling, richest list and a role shop."""
import random
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import brand

CUR = "BOLTs"
WORK_LINES = ["fixed a crashed node", "debugged a Wings container", "answered 20 support tickets", "migrated a customer's server",
              "optimized a Minecraft plugin", "patched a security hole", "wrote a deploy script", "upgraded the NVMe pool"]


class Economy(commands.Cog, name="Economy"):
    eco = app_commands.Group(name="economy", description="Economy admin",
                             default_permissions=discord.Permissions(manage_guild=True), guild_only=True)
    shop = app_commands.Group(name="shop", description="BOLTs shop", guild_only=True)

    def __init__(self, bot):
        self.bot = bot

    async def _ensure(self, gid, uid):
        await self.bot.db.execute("INSERT OR IGNORE INTO economy(guild_id,user_id) VALUES(?,?)", (gid, uid))

    async def balance_of(self, gid, uid):
        await self._ensure(gid, uid)
        return (await self.bot.db.fetchone("SELECT wallet FROM economy WHERE guild_id=? AND user_id=?", (gid, uid)))["wallet"]

    async def add(self, gid, uid, amount):
        await self._ensure(gid, uid)
        await self.bot.db.execute("UPDATE economy SET wallet=MAX(0, wallet+?) WHERE guild_id=? AND user_id=?", (amount, gid, uid))

    async def spend(self, gid, uid, amount):
        """Atomic: True only if the user had enough."""
        await self._ensure(gid, uid)
        n = await self.bot.db.rowcount("UPDATE economy SET wallet=wallet-? WHERE guild_id=? AND user_id=? AND wallet>=?",
                                       (amount, gid, uid, amount))
        return n > 0

    @app_commands.command(name="balance", description="Check your BOLTs")
    @app_commands.guild_only()
    async def balance(self, interaction: discord.Interaction, member: Optional[discord.Member] = None):
        m = member or interaction.user
        bal = await self.balance_of(interaction.guild.id, m.id)
        await interaction.response.send_message(embed=brand.embed(
            self.bot, "premium", f"{m.display_name}'s wallet", f"{{bolt}} **{bal:,}** {CUR}", guild=interaction.guild,
            thumbnail=m.display_avatar.url))

    @app_commands.command(name="daily", description="Claim your daily BOLTs (streak bonus!)")
    @app_commands.guild_only()
    async def daily(self, interaction: discord.Interaction):
        g, u = interaction.guild, interaction.user
        await self._ensure(g.id, u.id)
        row = await self.bot.db.fetchone("SELECT last_daily, streak FROM economy WHERE guild_id=? AND user_id=?", (g.id, u.id))
        now = time.time()
        left = 86400 - (now - row["last_daily"])
        if left > 0:
            return await interaction.response.send_message(embed=brand.embed(
                self.bot, "warn", "Come back later", f"{{bell}} Next daily in **{brand.human_duration(left)}**.", guild=g), ephemeral=True)
        streak = row["streak"] + 1 if now - row["last_daily"] < 172800 else 1
        reward = 100 + min(streak, 30) * 10
        await self.bot.db.execute("UPDATE economy SET wallet=wallet+?, last_daily=?, streak=? WHERE guild_id=? AND user_id=?",
                                  (reward, now, streak, g.id, u.id))
        await interaction.response.send_message(embed=brand.embed(
            self.bot, "ok", "Daily claimed", f"{{gift}} You got **{reward}** {CUR}!\n{{fire}} Streak: **{streak}** day(s) — keep it going for bigger rewards.",
            guild=g))

    @app_commands.command(name="work", description="Work for BOLTs (1 hour cooldown)")
    @app_commands.guild_only()
    async def work(self, interaction: discord.Interaction):
        g, u = interaction.guild, interaction.user
        await self._ensure(g.id, u.id)
        row = await self.bot.db.fetchone("SELECT last_work FROM economy WHERE guild_id=? AND user_id=?", (g.id, u.id))
        left = 3600 - (time.time() - row["last_work"])
        if left > 0:
            return await interaction.response.send_message(embed=brand.embed(
                self.bot, "warn", "Tired", f"{{maintenance}} Rest for **{brand.human_duration(left)}** first.", guild=g), ephemeral=True)
        reward = random.randint(40, 140)
        await self.bot.db.execute("UPDATE economy SET wallet=wallet+?, last_work=? WHERE guild_id=? AND user_id=?", (reward, time.time(), g.id, u.id))
        await interaction.response.send_message(embed=brand.embed(
            self.bot, "ok", "Shift done", f"{{cpu}} You {random.choice(WORK_LINES)} and earned **{reward}** {CUR}.", guild=g))

    @app_commands.command(name="pay", description="Send BOLTs to someone")
    @app_commands.guild_only()
    async def pay(self, interaction: discord.Interaction, member: discord.Member, amount: app_commands.Range[int, 1, 1000000]):
        g = interaction.guild
        if member.bot or member.id == interaction.user.id:
            return await interaction.response.send_message(embed=brand.embed(
                self.bot, "err", "Invalid", "Pick another real member.", guild=g), ephemeral=True)
        if not await self.spend(g.id, interaction.user.id, amount):
            return await interaction.response.send_message(embed=brand.embed(
                self.bot, "err", "Not enough BOLTs", "Check `/balance`.", guild=g), ephemeral=True)
        await self.add(g.id, member.id, amount)
        await interaction.response.send_message(embed=brand.embed(
            self.bot, "ok", "Payment sent", f"{{bolt}} {interaction.user.mention} sent **{amount:,}** {CUR} to {member.mention}.", guild=g))

    @app_commands.command(name="coinflip", description="Double or nothing")
    @app_commands.choices(side=[app_commands.Choice(name="Heads", value="heads"), app_commands.Choice(name="Tails", value="tails")])
    @app_commands.guild_only()
    async def coinflip(self, interaction: discord.Interaction, bet: app_commands.Range[int, 1, 50000], side: app_commands.Choice[str]):
        g, u = interaction.guild, interaction.user
        if not await self.spend(g.id, u.id, bet):
            return await interaction.response.send_message(embed=brand.embed(
                self.bot, "err", "Not enough BOLTs", "Check `/balance`.", guild=g), ephemeral=True)
        result = random.choice(["heads", "tails"])
        if result == side.value:
            await self.add(g.id, u.id, bet * 2)
            em = brand.embed(self.bot, "ok", "You won!", f"It landed **{result}** — you won **{bet:,}** {CUR}!", guild=g)
        else:
            em = brand.embed(self.bot, "err", "You lost", f"It landed **{result}** — you lost **{bet:,}** {CUR}.", guild=g)
        await interaction.response.send_message(embed=em)

    @app_commands.command(name="slots", description="Spin the slots")
    @app_commands.guild_only()
    async def slots(self, interaction: discord.Interaction, bet: app_commands.Range[int, 1, 50000]):
        g, u = interaction.guild, interaction.user
        if not await self.spend(g.id, u.id, bet):
            return await interaction.response.send_message(embed=brand.embed(
                self.bot, "err", "Not enough BOLTs", "Check `/balance`.", guild=g), ephemeral=True)
        names = ["rocket", "server", "bolt", "diamond", "crown", "star"]
        weights = [30, 25, 20, 12, 8, 5]
        reel = random.choices(names, weights, k=3)
        shown = " ".join(self.bot.emo.get(n, g) for n in reel)
        if len(set(reel)) == 1:
            mult = {"rocket": 3, "server": 4, "bolt": 5, "diamond": 10, "crown": 20, "star": 50}[reel[0]]
            win = bet * mult
            await self.add(g.id, u.id, win)
            em = brand.embed(self.bot, "premium", "JACKPOT!", f"{shown}\nYou won **{win:,}** {CUR} (x{mult})!", guild=g)
        elif len(set(reel)) == 2:
            await self.add(g.id, u.id, bet)
            em = brand.embed(self.bot, "ok", "Two of a kind", f"{shown}\nYou got your **{bet:,}** {CUR} back.", guild=g)
        else:
            em = brand.embed(self.bot, "err", "No luck", f"{shown}\nYou lost **{bet:,}** {CUR}.", guild=g)
        await interaction.response.send_message(embed=em)

    @app_commands.command(name="richest", description="BOLTs leaderboard")
    @app_commands.guild_only()
    async def richest(self, interaction: discord.Interaction):
        rows = await self.bot.db.fetchall("SELECT user_id, wallet FROM economy WHERE guild_id=? AND wallet>0 ORDER BY wallet DESC LIMIT 10",
                                          (interaction.guild.id,))
        medals = ["🥇", "🥈", "🥉"]
        body = "\n".join(f"{medals[i] if i < 3 else f'`{i + 1}.`'} <@{r['user_id']}> — **{r['wallet']:,}** {CUR}"
                         for i, r in enumerate(rows)) or "Nobody has BOLTs yet. Try `/daily`!"
        await interaction.response.send_message(embed=brand.embed(self.bot, "premium", "Richest members", "{diamond} " + body,
                                                                  guild=interaction.guild))

    # ---------------------------------------------------------------- admin
    @eco.command(name="give", description="Give BOLTs to a member")
    async def give(self, interaction: discord.Interaction, member: discord.Member, amount: app_commands.Range[int, 1, 10000000]):
        await self.add(interaction.guild.id, member.id, amount)
        await interaction.response.send_message(embed=brand.embed(
            self.bot, "ok", "Given", f"{member.mention} received **{amount:,}** {CUR}.", guild=interaction.guild), ephemeral=True)

    @eco.command(name="take", description="Remove BOLTs from a member")
    async def take(self, interaction: discord.Interaction, member: discord.Member, amount: app_commands.Range[int, 1, 10000000]):
        await self.add(interaction.guild.id, member.id, -amount)
        await interaction.response.send_message(embed=brand.embed(
            self.bot, "ok", "Taken", f"Removed up to **{amount:,}** {CUR} from {member.mention}.", guild=interaction.guild), ephemeral=True)

    # ---------------------------------------------------------------- shop
    @shop.command(name="list", description="See what you can buy")
    async def shop_list(self, interaction: discord.Interaction):
        rows = await self.bot.db.fetchall("SELECT * FROM shop WHERE guild_id=? ORDER BY price", (interaction.guild.id,))
        body = "\n".join(f"{{diamond}} **{r['name']}** — {r['price']:,} {CUR}\n└ {r['description'] or 'Role reward'}" for r in rows) \
            or "The shop is empty. Admins can add items with `/shop add`."
        await interaction.response.send_message(embed=brand.embed(self.bot, "premium", "BOLTs shop", body, guild=interaction.guild))

    @shop.command(name="buy", description="Buy an item")
    async def shop_buy(self, interaction: discord.Interaction, name: str):
        g, u = interaction.guild, interaction.user
        item = await self.bot.db.fetchone("SELECT * FROM shop WHERE guild_id=? AND LOWER(name)=LOWER(?)", (g.id, name))
        if not item:
            return await interaction.response.send_message(embed=brand.embed(self.bot, "err", "Not found", "No item with that name.", guild=g), ephemeral=True)
        role = g.get_role(item["role_id"]) if item["role_id"] else None
        if role and role in u.roles:
            return await interaction.response.send_message(embed=brand.embed(self.bot, "info", "Already owned", "You already have this role.", guild=g), ephemeral=True)
        if not await self.spend(g.id, u.id, item["price"]):
            return await interaction.response.send_message(embed=brand.embed(self.bot, "err", "Not enough BOLTs", f"You need **{item['price']:,}** {CUR}.", guild=g), ephemeral=True)
        if role:
            try:
                await u.add_roles(role, reason="Shop purchase")
            except discord.HTTPException as exc:
                await self.add(g.id, u.id, item["price"])                  # refund: never keep money without delivering
                return await interaction.response.send_message(embed=brand.embed(
                    self.bot, "err", "Could not give the role", f"You were refunded. ({exc.text})", guild=g), ephemeral=True)
        await interaction.response.send_message(embed=brand.embed(
            self.bot, "ok", "Purchased!", f"{{check}} You bought **{item['name']}** for {item['price']:,} {CUR}.", guild=g))

    @shop.command(name="add", description="Add a role item to the shop (admin)")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def shop_add(self, interaction: discord.Interaction, name: str, price: app_commands.Range[int, 1, 10000000], role: discord.Role,
                       description: str = ""):
        g = interaction.guild
        if role >= g.me.top_role:
            return await interaction.response.send_message(embed=brand.embed(self.bot, "err", "Role too high", "Pick a role below mine.", guild=g), ephemeral=True)
        try:
            await self.bot.db.execute("INSERT INTO shop(guild_id,name,price,role_id,description) VALUES(?,?,?,?,?)",
                                      (g.id, name[:40], price, role.id, description[:120]))
        except Exception:
            return await interaction.response.send_message(embed=brand.embed(self.bot, "err", "Exists", "An item with that name exists.", guild=g), ephemeral=True)
        await interaction.response.send_message(embed=brand.embed(self.bot, "ok", "Item added", f"**{name}** — {price:,} {CUR}", guild=g), ephemeral=True)

    @shop.command(name="remove", description="Remove a shop item (admin)")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def shop_remove(self, interaction: discord.Interaction, name: str):
        n = await self.bot.db.rowcount("DELETE FROM shop WHERE guild_id=? AND LOWER(name)=LOWER(?)", (interaction.guild.id, name))
        await interaction.response.send_message(embed=brand.embed(
            self.bot, "ok" if n else "err", "Removed" if n else "Not found", name, guild=interaction.guild), ephemeral=True)


async def setup(bot):
    await bot.add_cog(Economy(bot))
