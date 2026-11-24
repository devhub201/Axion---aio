"""Hosting-business features: plans showcase with Order button, payment methods, vouches, customer role, TOS, status helpers.
Plans are also written to data/plans_kb.md so the ticket AI quotes your REAL prices."""
import json
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

    CATS = {
        "minecraft": ("Minecraft Hosting", "rocket", "{rocket} **Lag-free Minecraft servers** • Paper, Spigot, Forge, Fabric, Bedrock\n"),
        "vps": ("VPS Hosting", "server", "{server} **Full root access VPS** • NVMe storage • DDoS protected\n"),
        "perks": ("Invite & Booster Perks", "gift", "{gift} **Free rewards** for helping the community grow!\n"),
    }
    CAT_CHOICES = [app_commands.Choice(name="Minecraft", value="minecraft"), app_commands.Choice(name="VPS", value="vps"),
                   app_commands.Choice(name="Invite & Booster perks", value="perks")]

    def _write_kb(self, plans):
        os.makedirs("data", exist_ok=True)
        lines = ["## LIVE PLANS (official, set by staff — always quote these exactly)"]
        for key, (label, _, _) in self.CATS.items():
            items = [p for p in plans if p.get("category", "vps") == key]
            if items:
                lines.append(f"### {label}")
                lines += [f"- {p['name']}: {p['specs']} — {p['price']}" for p in items]
        if len(lines) == 1:
            lines.append("- (no plans published yet)")
        with open(PLANS_KB, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines))

    @plans.command(name="add", description="Add or update a plan / perk")
    @app_commands.describe(category="Minecraft, VPS or perks", name="Plan name", price="e.g. ₹199/month (perks: e.g. 5 invites)",
                           specs="e.g. 4GB RAM • 2 vCPU • 40GB NVMe", emoji="Emoji name, e.g. diamond")
    @app_commands.choices(category=CAT_CHOICES)
    async def plan_add(self, interaction: discord.Interaction, category: app_commands.Choice[str], name: str, price: str, specs: str,
                       emoji: str = "diamond"):
        g = interaction.guild
        plans = [p for p in await self._plans(g.id) if not (p["name"].lower() == name.lower() and p.get("category") == category.value)]
        plans.append({"category": category.value, "name": name[:40], "price": price[:60], "specs": specs[:200], "emoji": emoji.strip("{}:")[:20]})
        await self.bot.db.set(g.id, "plans", plans)
        self._write_kb(plans)
        await interaction.response.send_message(embed=brand.embed(
            self.bot, "ok", "Saved", f"**{name}** ({category.name}) — {price}\n{specs}\nThe ticket AI now knows this.", guild=g), ephemeral=True)

    @plans.command(name="remove", description="Remove a plan")
    async def plan_remove(self, interaction: discord.Interaction, name: str):
        g = interaction.guild
        old = await self._plans(g.id)
        plans = [p for p in old if p["name"].lower() != name.lower()]
        await self.bot.db.set(g.id, "plans", plans)
        self._write_kb(plans)
        await interaction.response.send_message(embed=brand.embed(
            self.bot, "ok" if len(plans) < len(old) else "err", "Removed" if len(plans) < len(old) else "Not found", name, guild=g), ephemeral=True)

    def _cat_embed(self, g, key, plans):
        label, icon, intro = self.CATS[key]
        items = [p for p in plans if p.get("category", "vps") == key]
        fields = [(f"{{{p['emoji']}}} {p['name']}", f"{p['specs']}\n**{p['price']}**", True) for p in items[:24]]
        tail = {"minecraft": "{shield} DDoS protected • {bolt} NVMe • {support} 24/7 support\n{ticket} Press **Order now** to open a ticket!",
                "vps": "{shield} DDoS protected • {bolt} NVMe • {support} 24/7 support\n{ticket} Press **Order now** to open a ticket!",
                "perks": "{members} Invite friends with `/invites` • {diamond} Boost the server to unlock more\n{ticket} Open a ticket to claim your reward!"}[key]
        em = brand.embed(self.bot, "premium", f"{brand.NAME} • {label}", f"{intro}{brand.LINE}\n{tail}", guild=g, fields=fields)
        em.color = {"minecraft": 0x22C55E, "vps": 0x3B82F6, "perks": 0xEC4899}[key]
        return em, bool(items)

    @plans.command(name="post", description="Post plan embeds (Minecraft, VPS, perks) to a channel")
    @app_commands.describe(category="Which one to post (default: all)", channel="Where to post")
    @app_commands.choices(category=[app_commands.Choice(name="All", value="all")] + CAT_CHOICES)
    async def plan_post(self, interaction: discord.Interaction, category: Optional[app_commands.Choice[str]] = None,
                        channel: Optional[discord.TextChannel] = None):
        g = interaction.guild
        plans = await self._plans(g.id)
        keys = list(self.CATS) if not category or category.value == "all" else [category.value]
        ch = channel or interaction.channel
        await interaction.response.defer(ephemeral=True)
        posted, missing = 0, []
        self._write_kb(plans)
        for key in keys:
            em, has = self._cat_embed(g, key, plans)
            if not has:
                missing.append(self.CATS[key][0])
                continue
            try:
                await ch.send(embed=em, view=OrderView(self.bot, g) if key != "perks" else None)
                posted += 1
            except discord.HTTPException as exc:
                return await interaction.followup.send(embed=brand.embed(self.bot, "err", "Could not post", exc.text, guild=g), ephemeral=True)
        note = f"\nNo items yet for: {', '.join(missing)} (add with `/plans add` or `/plans load-defaults`)." if missing else ""
        await interaction.followup.send(embed=brand.embed(
            self.bot, "ok" if posted else "err", "Posted" if posted else "Nothing to post", f"{posted} embed(s) sent to {ch.mention}.{note}", guild=g), ephemeral=True)

    @plans.command(name="load-defaults", description="Load the built-in AxionHost plans from plans_seed.json")
    async def plan_defaults(self, interaction: discord.Interaction):
        g = interaction.guild
        try:
            with open("plans_seed.json", "r", encoding="utf-8") as fh:
                seed = json.load(fh)
        except (OSError, ValueError) as exc:
            return await interaction.response.send_message(embed=brand.embed(
                self.bot, "err", "Cannot read plans_seed.json", str(exc), guild=g), ephemeral=True)
        plans = [p for p in seed if isinstance(p, dict) and p.get("name") and p.get("category") in self.CATS]
        if not plans:
            return await interaction.response.send_message(embed=brand.embed(
                self.bot, "err", "Seed is empty", "plans_seed.json has no plans yet.", guild=g), ephemeral=True)
        for p in plans:
            p.setdefault("emoji", "diamond")
        await self.bot.db.set(g.id, "plans", plans)
        self._write_kb(plans)
        await interaction.response.send_message(embed=brand.embed(
            self.bot, "ok", "Plans loaded", f"Loaded **{len(plans)}** items. Now run `/plans post` in the channel you want.", guild=g), ephemeral=True)

    @app_commands.command(name="plans-list", description="See our hosting plans")
    @app_commands.guild_only()
    async def plans_list(self, interaction: discord.Interaction):
        g = interaction.guild
        plans = await self._plans(g.id)
        embeds = [self._cat_embed(g, k, plans)[0] for k in self.CATS if self._cat_embed(g, k, plans)[1]]
        if not embeds:
            return await interaction.response.send_message(embed=brand.embed(self.bot, "info", "Plans", "Plans coming soon.", guild=g))
        await interaction.response.send_message(embeds=embeds[:3])

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
