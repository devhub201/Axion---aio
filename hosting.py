"""Hosting-business features: plans showcase with Order button, payment methods, vouches, customer role, TOS, status helpers.
Plans are also written to data/plans_kb.md so the ticket AI quotes your REAL prices."""
import os
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import brand
import tickets

PLANS_KB = os.path.join("data", "plans_kb.md")
HOST_DEFAULTS = {"vouch_channel_id": None, "customer_role_id": None, "payments": None, "tos": None}


class OrderView(discord.ui.View):
    """Persistent 'Order now' button -> opens the Sales ticket form."""

    def __init__(self, bot, guild=None):
        super().__init__(timeout=None)
        self.bot = bot
        self.order_btn.emoji = bot.emo.partial("rocket", guild)

    @discord.ui.button(label="Order now", style=discord.ButtonStyle.success, custom_id="ax:order")
    async def order_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = self.bot.get_cog("Tickets")
        await interaction.response.send_modal(tickets.TicketModal(cog, "sales"))


class VouchModal(discord.ui.Modal, title="Leave a vouch"):
    stars = discord.ui.TextInput(label="Rating (1-5)", max_length=1, placeholder="5")
    text = discord.ui.TextInput(label="Your review", style=discord.TextStyle.paragraph, max_length=500,
                                placeholder="Fast setup, great uptime, helpful staff...")

    def __init__(self, cog):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.save_vouch(interaction, str(self.stars), str(self.text))


class Hosting(commands.Cog, name="Hosting"):
    plans = app_commands.Group(name="plans", description="Hosting plans showcase",
                               default_permissions=discord.Permissions(manage_guild=True), guild_only=True)
    host = app_commands.Group(name="host", description="Hosting business settings",
                              default_permissions=discord.Permissions(manage_guild=True), guild_only=True)

    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        self.bot.add_view(OrderView(self.bot))

    async def cfg(self, gid):
        return await self.bot.db.cfg(gid, "host", HOST_DEFAULTS)

    # ================================================================== plans
    async def _plans(self, gid):
        return await self.bot.db.get(gid, "plans", [])

    def _write_kb(self, plans):
        os.makedirs("data", exist_ok=True)
        lines = ["## LIVE PLANS (official, set by staff — always quote these exactly)"]
        lines += [f"- {p['name']}: {p['specs']} — {p['price']}" for p in plans] or ["- (no plans published yet)"]
        with open(PLANS_KB, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines))

    @plans.command(name="add", description="Add or update a plan")
    @app_commands.describe(name="Plan name", price="e.g. ₹199/month", specs="e.g. 4GB RAM • 2 vCPU • 40GB NVMe", emoji="Emoji name, e.g. diamond")
    async def plan_add(self, interaction: discord.Interaction, name: str, price: str, specs: str, emoji: str = "diamond"):
        g = interaction.guild
        plans = [p for p in await self._plans(g.id) if p["name"].lower() != name.lower()]
        plans.append({"name": name[:40], "price": price[:40], "specs": specs[:150], "emoji": emoji.strip("{}:")[:20]})
        await self.bot.db.set(g.id, "plans", plans)
        self._write_kb(plans)
        await interaction.response.send_message(embed=brand.embed(
            self.bot, "ok", "Plan saved", f"**{name}** — {price}\n{specs}\nThe ticket AI now knows this plan.", guild=g), ephemeral=True)

    @plans.command(name="remove", description="Remove a plan")
    async def plan_remove(self, interaction: discord.Interaction, name: str):
        g = interaction.guild
        old = await self._plans(g.id)
        plans = [p for p in old if p["name"].lower() != name.lower()]
        await self.bot.db.set(g.id, "plans", plans)
        self._write_kb(plans)
        await interaction.response.send_message(embed=brand.embed(
            self.bot, "ok" if len(plans) < len(old) else "err", "Removed" if len(plans) < len(old) else "Not found", name, guild=g), ephemeral=True)

    @plans.command(name="post", description="Post the plans embed with an Order button")
    async def plan_post(self, interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
        g = interaction.guild
        plans = await self._plans(g.id)
        if not plans:
            return await interaction.response.send_message(embed=brand.embed(
                self.bot, "err", "No plans", "Add some with `/plans add` first.", guild=g), ephemeral=True)
        fields = [(f"{{{p['emoji']}}} {p['name']}", f"{p['specs']}\n**{p['price']}**", True) for p in plans[:24]]
        em = brand.embed(self.bot, "premium", f"{brand.NAME} Plans",
                         f"{{rocket}} **Fast. Secure. Premium.**\n{brand.LINE}\n{{shield}} DDoS protected • {{bolt}} NVMe • {{support}} 24/7 support\n{brand.LINE}",
                         guild=g, fields=fields)
        self._write_kb(plans)
        try:
            msg = await (channel or interaction.channel).send(embed=em, view=OrderView(self.bot, g))
        except discord.HTTPException as exc:
            return await interaction.response.send_message(embed=brand.embed(self.bot, "err", "Could not post", exc.text, guild=g), ephemeral=True)
        await interaction.response.send_message(embed=brand.embed(self.bot, "ok", "Plans posted", msg.jump_url, guild=g), ephemeral=True)

    @app_commands.command(name="plans-list", description="See our hosting plans")
    @app_commands.guild_only()
    async def plans_list(self, interaction: discord.Interaction):
        g = interaction.guild
        plans = await self._plans(g.id)
        fields = [(f"{{{p['emoji']}}} {p['name']}", f"{p['specs']}\n**{p['price']}**", True) for p in plans[:24]]
        await interaction.response.send_message(embed=brand.embed(
            self.bot, "premium", f"{brand.NAME} Plans", "Open a ticket to order!" if plans else "Plans coming soon.", guild=g, fields=fields))

    # ================================================================== payments / tos / customer role / vouch channel
    @host.command(name="payments", description="Set the payment-methods text, e.g. UPI id, crypto addresses")
    async def host_payments(self, interaction: discord.Interaction, text: str):
        await self.bot.db.cfg_set(interaction.guild.id, "host", HOST_DEFAULTS, payments=text.replace("\\n", "\n")[:1500])
        await interaction.response.send_message(embed=brand.embed(self.bot, "ok", "Payment info saved", "Members can see it with `/payments`.", guild=interaction.guild), ephemeral=True)

    @app_commands.command(name="payments", description="How to pay")
    @app_commands.guild_only()
    async def payments(self, interaction: discord.Interaction):
        cfg = await self.cfg(interaction.guild.id)
        await interaction.response.send_message(embed=brand.embed(
            self.bot, "premium", "Payment methods",
            ("{billing} " + cfg["payments"] + f"\n{brand.LINE}\n{{shield}} Always pay inside a ticket and wait for staff confirmation.")
            if cfg["payments"] else "Payment info is not set yet. Open a ticket!", guild=interaction.guild))

    @host.command(name="tos", description="Set the Terms of Service text")
    async def host_tos(self, interaction: discord.Interaction, text: str):
        await self.bot.db.cfg_set(interaction.guild.id, "host", HOST_DEFAULTS, tos=text.replace("\\n", "\n")[:3500])
        await interaction.response.send_message(embed=brand.embed(self.bot, "ok", "TOS saved", "Show it with `/tos`.", guild=interaction.guild), ephemeral=True)

    @app_commands.command(name="tos", description="Terms of Service")
    @app_commands.guild_only()
    async def tos(self, interaction: discord.Interaction):
        cfg = await self.cfg(interaction.guild.id)
        await interaction.response.send_message(embed=brand.embed(
            self.bot, "info", "Terms of Service", ("{shield} " + cfg["tos"]) if cfg["tos"] else "No TOS set yet.", guild=interaction.guild))

    @host.command(name="vouch-channel", description="Where new vouches are posted")
    async def host_vouch_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await self.bot.db.cfg_set(interaction.guild.id, "host", HOST_DEFAULTS, vouch_channel_id=channel.id)
        await interaction.response.send_message(embed=brand.embed(self.bot, "ok", "Vouch channel set", channel.mention, guild=interaction.guild), ephemeral=True)

    @host.command(name="customer-role", description="Role given to paying customers")
    async def host_customer_role(self, interaction: discord.Interaction, role: discord.Role):
        if role >= interaction.guild.me.top_role:
            return await interaction.response.send_message(embed=brand.embed(self.bot, "err", "Role too high", "Pick a role below mine.", guild=interaction.guild), ephemeral=True)
        await self.bot.db.cfg_set(interaction.guild.id, "host", HOST_DEFAULTS, customer_role_id=role.id)
        await interaction.response.send_message(embed=brand.embed(self.bot, "ok", "Customer role set", role.mention, guild=interaction.guild), ephemeral=True)

    @app_commands.command(name="customer", description="Mark a member as a customer and thank them (staff)")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def customer(self, interaction: discord.Interaction, member: discord.Member, plan: str = ""):
        g = interaction.guild
        cfg = await self.cfg(g.id)
        role = g.get_role(cfg["customer_role_id"]) if cfg["customer_role_id"] else None
        note = ""
        if role:
            try:
                await member.add_roles(role, reason=f"Customer by {interaction.user}")
            except discord.HTTPException as exc:
                note = f"\n⚠️ Could not give the role: {exc.text}"
        else:
            note = "\nTip: set a role with `/host customer-role`."
        await interaction.response.send_message(embed=brand.embed(
            self.bot, "premium", "Welcome, customer!", f"{{crown}} {member.mention} is now an **{brand.NAME} customer**"
            + (f" on **{plan}**" if plan else "") + f"!\nThank you for trusting us {{diamond}}\nLeave a review with `/vouch`.{note}", guild=g))

    # ================================================================== vouches
    @app_commands.command(name="vouch", description="Leave a review for the service")
    @app_commands.guild_only()
    async def vouch(self, interaction: discord.Interaction):
        await interaction.response.send_modal(VouchModal(self))

    async def save_vouch(self, interaction, stars, text):
        g = interaction.guild
        if not stars.isdigit() or not 1 <= int(stars) <= 5:
            return await interaction.response.send_message(embed=brand.embed(
                self.bot, "err", "Bad rating", "Rating must be a number from 1 to 5.", guild=g), ephemeral=True)
        n = int(stars)
        cfg = await self.cfg(g.id)
        ch = g.get_channel(cfg["vouch_channel_id"]) if cfg["vouch_channel_id"] else None
        if not ch:
            return await interaction.response.send_message(embed=brand.embed(
                self.bot, "err", "Not set up", "An admin must run `/host vouch-channel` first.", guild=g), ephemeral=True)
        vid = await self.bot.db.execute("INSERT INTO vouches(guild_id,user_id,stars,text,created_at) VALUES(?,?,?,?,strftime('%s','now'))",
                                        (g.id, interaction.user.id, n, text[:500]))
        em = brand.embed(self.bot, "premium", f"Vouch #{vid}", f"{'⭐' * n}{'☆' * (5 - n)}\n>>> {discord.utils.escape_markdown(text[:500])}",
                         guild=g, thumbnail=interaction.user.display_avatar.url, fields=[("Customer", interaction.user.mention, True)])
        try:
            await ch.send(embed=em)
        except discord.HTTPException as exc:
            return await interaction.response.send_message(embed=brand.embed(
                self.bot, "err", "Could not post", exc.text, guild=g), ephemeral=True)
        await interaction.response.send_message(embed=brand.embed(self.bot, "ok", "Thanks for the vouch!", f"{{heart}} Posted in {ch.mention}".replace("{heart}", "💜"), guild=g), ephemeral=True)

    @app_commands.command(name="vouches", description="Our rating from customers")
    @app_commands.guild_only()
    async def vouches(self, interaction: discord.Interaction):
        row = await self.bot.db.fetchone("SELECT COUNT(*) c, AVG(stars) a FROM vouches WHERE guild_id=?", (interaction.guild.id,))
        if not row["c"]:
            return await interaction.response.send_message(embed=brand.embed(
                self.bot, "info", "Vouches", "No vouches yet. Be the first with `/vouch`!", guild=interaction.guild))
        avg = row["a"]
        await interaction.response.send_message(embed=brand.embed(
            self.bot, "premium", "Customer rating", f"{'⭐' * round(avg)}  **{avg:.2f} / 5**\nFrom **{row['c']}** verified vouch(es).",
            guild=interaction.guild))


async def setup(bot):
    await bot.add_cog(Hosting(bot))
