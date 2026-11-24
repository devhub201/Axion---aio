"""Hosting-business features: plans showcase with Order button, payment methods, vouches, customer role, TOS, status helpers.
Plans are also written to data/plans_kb.md so the ticket AI quotes your REAL prices."""
import json
import os
import re
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import brand
import tickets

PLANS_KB = os.path.join("data", "plans_kb.md")
HOST_DEFAULTS = {"vouch_channel_id": None, "customer_role_id": None, "payments": None, "tos": None, "payment_log_id": None}


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


class ClaimView(discord.ui.View):
    """Persistent 'Claim reward' button -> opens a Claim Reward ticket form."""

    def __init__(self, bot, guild=None):
        super().__init__(timeout=None)
        self.bot = bot
        self.claim_btn.emoji = bot.emo.partial("gift", guild)

    @discord.ui.button(label="Claim reward", style=discord.ButtonStyle.primary, custom_id="ax:claim")
    async def claim_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(tickets.TicketModal(self.bot.get_cog("Tickets"), "rewards"))


class PaidModal(discord.ui.Modal, title="Confirm payment"):
    plan = discord.ui.TextInput(label="Plan bought", placeholder="e.g. Ryzen Minecraft GOLD", max_length=80)
    amount = discord.ui.TextInput(label="Amount received (INR)", placeholder="384", max_length=12)
    method = discord.ui.TextInput(label="Payment method", default="UPI", max_length=30)

    def __init__(self, cog):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.confirm_payment(interaction, str(self.plan), str(self.amount), str(self.method))


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
        self.bot.add_view(ClaimView(self.bot))

    async def cfg(self, gid):
        return await self.bot.db.cfg(gid, "host", HOST_DEFAULTS)

    # ================================================================== plans
    async def _sections(self, gid):
        return await self.bot.db.get(gid, "plan_sections", [])

    def _write_kb(self, sections):
        os.makedirs("data", exist_ok=True)
        clean = lambda t: re.sub(r"\{\w+\}\s*", "", t).replace("**", "")
        lines = ["## LIVE PLANS & REWARDS (official, set by staff — always quote these exactly; prices are per month in INR)"]
        for sec in sections:
            lines.append(f"### {sec['title']}")
            lines += [clean(h) for h in sec.get("header", [])]
            for p in sec.get("plans", []):
                lines.append(f"- {p['name']}: " + " | ".join(clean(x) for x in p["lines"]))
            for blk in sec.get("blocks", []):
                lines.append(f"{clean(blk['name'])}: " + " ; ".join(clean(x) for x in blk["lines"]))
            if sec["kind"] == "rewards":
                lines.append("Rewards are claimed by opening a ticket after reaching the invites/boosts.")
        with open(PLANS_KB, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines)[:6000])

    def _section_embed(self, g, sec):
        emo = self.bot.emo
        f = lambda t: emo.fmt(t, g)
        em = discord.Embed(title=f"{emo.get('crown' if sec['kind'] == 'rewards' else ('rocket' if sec['kind'] == 'minecraft' else 'server'), g)} {sec['title']}",
                           description="\n".join(f(h) for h in sec.get("header", [])) + f"\n{brand.LINE}", color=sec.get("color", 0x8B5CF6))
        for p in sec.get("plans", []):
            icon = p.get("emoji", "diamond")
            shown = emo.get(icon, g) if icon.isalnum() else icon
            em.add_field(name=f"{shown} {p['name']}", value="\n".join(f(l) for l in p["lines"]), inline=True)
        for blk in sec.get("blocks", []):
            em.add_field(name=f(blk["name"]), value="\n".join(f"> {f(l)}" for l in blk["lines"])[:1024], inline=False)
        if sec.get("footer"):
            em.add_field(name="\u200b", value="\n".join(f"> {f(l)}" for l in sec["footer"].split("\n"))[:1024], inline=False)
        em.set_footer(text=f"{brand.NAME} • Premium Hosting")
        em.timestamp = discord.utils.utcnow()
        return em

    GROUPS = [app_commands.Choice(name="Everything", value="all"), app_commands.Choice(name="Minecraft plans (Intel/AMD/Ryzen)", value="minecraft"),
              app_commands.Choice(name="VPS plans (Intel/AMD/Ryzen)", value="vps"), app_commands.Choice(name="Invite & Booster rewards", value="rewards")]
    TIERS = [app_commands.Choice(name="Intel", value="intel"), app_commands.Choice(name="AMD", value="amd"), app_commands.Choice(name="Ryzen", value="ryzen")]

    @plans.command(name="load-defaults", description="Load the AxionHost plans from plans_seed.json (run again after editing the file)")
    async def plan_defaults(self, interaction: discord.Interaction):
        g = interaction.guild
        try:
            with open("plans_seed.json", "r", encoding="utf-8") as fh:
                seed = json.load(fh)
        except (OSError, ValueError) as exc:
            return await interaction.response.send_message(embed=brand.embed(
                self.bot, "err", "Cannot read plans_seed.json", str(exc), guild=g), ephemeral=True)
        secs = [x for x in seed if isinstance(x, dict) and x.get("title") and x.get("kind") in ("minecraft", "vps", "rewards")]
        if not secs:
            return await interaction.response.send_message(embed=brand.embed(
                self.bot, "err", "Seed is empty", "plans_seed.json has no valid sections.", guild=g), ephemeral=True)
        await self.bot.db.set(g.id, "plan_sections", secs)
        self._write_kb(secs)
        await interaction.response.send_message(embed=brand.embed(
            self.bot, "ok", "Plans loaded", f"Loaded **{len(secs)}** sections ({sum(len(x.get('plans', [])) for x in secs)} plans). "
            "The ticket AI now knows every price.\nNext: `/plans post` in the channel you want.", guild=g), ephemeral=True)

    @plans.command(name="post", description="Post plan embeds to a channel")
    @app_commands.describe(group="What to post (default: everything)", tier="Only one CPU tier", channel="Where to post (default: here)")
    @app_commands.choices(group=GROUPS, tier=TIERS)
    async def plan_post(self, interaction: discord.Interaction, group: Optional[app_commands.Choice[str]] = None,
                        tier: Optional[app_commands.Choice[str]] = None, channel: Optional[discord.TextChannel] = None):
        g = interaction.guild
        secs = await self._sections(g.id)
        if not secs:
            return await interaction.response.send_message(embed=brand.embed(
                self.bot, "err", "No plans loaded", "Run `/plans load-defaults` first.", guild=g), ephemeral=True)
        want = group.value if group else "all"
        chosen = [x for x in secs if (want == "all" or x["group"] == want) and (not tier or x["key"].startswith(tier.value + "-"))]
        if not chosen:
            return await interaction.response.send_message(embed=brand.embed(
                self.bot, "err", "Nothing matches", "That group/tier combination has no plans.", guild=g), ephemeral=True)
        ch = channel or interaction.channel
        perms = ch.permissions_for(g.me)
        if not (perms.send_messages and perms.embed_links):
            return await interaction.response.send_message(embed=brand.embed(
                self.bot, "err", "Missing permission", f"I need **Send Messages** + **Embed Links** in {ch.mention}.", guild=g), ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        posted = 0
        for sec in chosen:
            view = ClaimView(self.bot, g) if sec["kind"] == "rewards" else OrderView(self.bot, g)
            try:
                await ch.send(embed=self._section_embed(g, sec), view=view)
                posted += 1
            except discord.HTTPException as exc:
                return await interaction.followup.send(embed=brand.embed(
                    self.bot, "err", "Stopped", f"Posted {posted}, then Discord refused: {exc.text}", guild=g), ephemeral=True)
        await interaction.followup.send(embed=brand.embed(
            self.bot, "ok", "Posted", f"{posted} embed(s) sent to {ch.mention}.", guild=g), ephemeral=True)

    @app_commands.command(name="plans-list", description="See our hosting plans")
    @app_commands.guild_only()
    @app_commands.choices(group=GROUPS[1:], tier=TIERS)
    async def plans_list(self, interaction: discord.Interaction, group: app_commands.Choice[str], tier: Optional[app_commands.Choice[str]] = None):
        g = interaction.guild
        secs = [x for x in await self._sections(g.id) if x["group"] == group.value and (not tier or x["key"].startswith(tier.value + "-") or x["kind"] == "rewards")]
        if not secs:
            return await interaction.response.send_message(embed=brand.embed(self.bot, "info", "Plans", "Nothing here yet.", guild=g), ephemeral=True)
        await interaction.response.send_message(embeds=[self._section_embed(g, x) for x in secs[:3]])

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

    # ================================================================== payment confirmation
    async def _paid_context(self, interaction):
        """Returns the open ticket row if the caller is staff inside a ticket, else answers with an error and returns None."""
        if not interaction.guild or not isinstance(interaction.user, discord.Member) or not await brand.is_staff(self.bot, interaction.user):
            await interaction.response.send_message(embed=brand.embed(
                self.bot, "err", "Staff only", "Only staff can confirm payments.", guild=interaction.guild), ephemeral=True)
            return None
        row = await self.bot.get_cog("Tickets").get_ticket(interaction.channel.id)
        if not row:
            await interaction.response.send_message(embed=brand.embed(
                self.bot, "err", "Not a ticket", "Use this inside the customer's open ticket.", guild=interaction.guild), ephemeral=True)
            return None
        return row

    async def open_paid_modal(self, interaction):
        if await self._paid_context(interaction):
            await interaction.response.send_modal(PaidModal(self))

    @app_commands.command(name="paid", description="Staff: confirm a payment in this ticket and give the customer role")
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.guild_only()
    async def paid(self, interaction: discord.Interaction, plan: str, amount: str, method: str = "UPI"):
        if await self._paid_context(interaction):
            await self.confirm_payment(interaction, plan, amount, method)

    async def confirm_payment(self, interaction, plan, amount_text, method):
        g = interaction.guild
        row = await self._paid_context_quiet(interaction)
        if not row:
            return await interaction.response.send_message(embed=brand.embed(
                self.bot, "err", "Not a ticket", "This channel is not an open ticket anymore.", guild=g), ephemeral=True)
        digits = re.sub(r"[^\d.]", "", amount_text)
        try:
            amount = float(digits)
            assert amount > 0
        except (ValueError, AssertionError):
            return await interaction.response.send_message(embed=brand.embed(
                self.bot, "err", "Bad amount", "Enter the amount as a number, e.g. `384`.", guild=g), ephemeral=True)
        member = g.get_member(row["user_id"])
        pid = await self.bot.db.execute(
            "INSERT INTO payments(guild_id,ticket_id,user_id,staff_id,plan,amount,method,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (g.id, row["id"], row["user_id"], interaction.user.id, plan[:80], amount, method[:30] or "UPI", time.time()))
        cfg = await self.cfg(g.id)
        role = g.get_role(cfg["customer_role_id"]) if cfg["customer_role_id"] else None
        notes = []
        if role and member:
            try:
                await member.add_roles(role, reason=f"Payment #{pid} confirmed by {interaction.user}")
            except discord.HTTPException as exc:
                notes.append(f"⚠️ Could not give {role.mention}: {exc.text} (my role must be above it).")
        elif not role:
            notes.append("Tip: set the customer role with `/host customer-role` so buyers get it automatically.")
        shown = f"₹{amount:,.0f}" if amount == int(amount) else f"₹{amount:,.2f}"
        receipt = brand.embed(self.bot, "ok", f"Payment confirmed • #{pid:04d}",
                              f"{{check}} Thank you <@{row['user_id']}>! Your payment has been received and confirmed.\n"
                              f"{brand.LINE}\n{{rocket}} Our team will now set up your service.\n{{star}} Loved it? Leave a review with `/vouch`.",
                              guild=g, fields=[("{members} Customer", f"<@{row['user_id']}>", True), ("{diamond} Plan", plan[:80], True),
                                               ("{money} Amount", shown, True), ("{billing} Method", method[:30] or "UPI", True),
                                               ("{crown} Confirmed by", interaction.user.mention, True), ("{ticket} Ticket", f"#{row['id']:04d}", True)])
        await interaction.response.send_message("✅ Payment recorded.", ephemeral=True)
        try:
            await interaction.channel.send(content=f"<@{row['user_id']}>", embed=receipt,
                                           allowed_mentions=discord.AllowedMentions(users=True))
        except discord.HTTPException as exc:
            notes.append(f"Could not post the receipt here: {exc.text}")
        if member:
            try:
                await member.send(embed=receipt)
            except discord.HTTPException:
                pass                                              # DMs closed
        log = g.get_channel(cfg["payment_log_id"]) if cfg["payment_log_id"] else None
        if log:
            try:
                await log.send(embed=receipt)
            except discord.HTTPException as exc:
                notes.append(f"Could not post in the payment log: {exc.text}")
        else:
            await brand.send_log(self.bot, g, receipt)
        if notes:
            await interaction.followup.send("\n".join(notes), ephemeral=True)

    async def _paid_context_quiet(self, interaction):
        return await self.bot.get_cog("Tickets").get_ticket(interaction.channel.id)

    @host.command(name="payment-log", description="Channel where payment receipts are logged")
    async def host_payment_log(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await self.bot.db.cfg_set(interaction.guild.id, "host", HOST_DEFAULTS, payment_log_id=channel.id)
        await interaction.response.send_message(embed=brand.embed(self.bot, "ok", "Payment log set", channel.mention, guild=interaction.guild), ephemeral=True)

    @app_commands.command(name="revenue", description="Staff: income summary from confirmed payments")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def revenue(self, interaction: discord.Interaction):
        g, db = interaction.guild, self.bot.db
        tot = await db.fetchone("SELECT COUNT(*) c, COALESCE(SUM(amount),0) s FROM payments WHERE guild_id=?", (g.id,))
        mon = await db.fetchone("SELECT COUNT(*) c, COALESCE(SUM(amount),0) s FROM payments WHERE guild_id=? AND "
                                "strftime('%Y-%m', created_at, 'unixepoch', '+5 hours', '+30 minutes') = strftime('%Y-%m', 'now', '+5 hours', '+30 minutes')", (g.id,))
        by = await db.fetchall("SELECT method, COUNT(*) c, SUM(amount) s FROM payments WHERE guild_id=? GROUP BY method ORDER BY s DESC LIMIT 5", (g.id,))
        top = await db.fetchall("SELECT plan, COUNT(*) c FROM payments WHERE guild_id=? GROUP BY plan ORDER BY c DESC LIMIT 3", (g.id,))
        methods = "\n".join(f"• {r['method']}: ₹{r['s']:,.0f} ({r['c']})" for r in by) or "—"
        plans = "\n".join(f"• {r['plan']}: {r['c']} sold" for r in top) or "—"
        await interaction.response.send_message(embed=brand.embed(
            self.bot, "premium", "Revenue", None, guild=g, fields=[
                ("{money} All time", f"₹{tot['s']:,.0f}\n{tot['c']} payment(s)", True),
                ("{stats} This month (IST)", f"₹{mon['s']:,.0f}\n{mon['c']} payment(s)", True),
                ("{billing} By method", methods, False), ("{diamond} Best sellers", plans, False)]), ephemeral=True)

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
