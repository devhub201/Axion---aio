"""Ticket system with a built-in self-hosted AI first responder."""
import asyncio
import html
import io
import re
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import ai as aimod
import brand

MAX_AI_REPLIES = 14

CATEGORIES = {
    "sales": ("Sales & Plans", "diamond", "Buy a plan, pricing, custom orders"),
    "support": ("Technical Support", "support", "Setup help, errors, downtime"),
    "billing": ("Billing & Payments", "billing", "Payments, invoices, renewals"),
    "partner": ("Partnership", "star", "Collabs, resellers, business"),
    "rewards": ("Claim Reward", "gift", "Claim invite / booster rewards"),
    "report": ("Report / Abuse", "shield", "Report abuse or a security problem"),
}

DEFAULT_PANEL = {
    "title": "{ticket} Open a Support Ticket",
    "description": (
        "{support} **Need help? Our team is one click away!**\n" + brand.LINE + "\n"
        "{diamond} **Sales** — plans, pricing, custom orders\n"
        "{support} **Technical Support** — setup help, errors, downtime\n"
        "{billing} **Billing** — payments, invoices, renewals\n"
        "{gift} **Claim Reward** — invite & booster rewards\n"
        "{star} **Partnership** — collabs & business\n"
        "{shield} **Report** — abuse or security issues\n" + brand.LINE + "\n"
        "{robot} **Axion AI** replies instantly, and {members} our staff steps in whenever you need.\n"
        "{bolt} Pick a category below to start."),
    "fields": [],
    "footer": "Fast support, 24/7",
}


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:24] or "user"


# ---------------------------------------------------------------------------
# views
# ---------------------------------------------------------------------------
class TicketPanelView(discord.ui.View):
    def __init__(self, bot, guild=None):
        super().__init__(timeout=None)
        self.bot, self.guild = bot, guild
        options = [
            discord.SelectOption(label=label, value=key, description=desc[:100], emoji=bot.emo.partial(icon, guild))
            for key, (label, icon, desc) in CATEGORIES.items()]
        select = discord.ui.Select(custom_id="ax:tkt:new", placeholder="Choose a category to open a ticket",
                                   options=options, min_values=1, max_values=1)
        select.callback = self.on_select
        self.add_item(select)

    async def on_select(self, interaction: discord.Interaction):
        key = interaction.data["values"][0]
        await interaction.response.send_modal(TicketModal(self.bot.get_cog("Tickets"), key))
        try:                                     # reset the dropdown so the same option can be picked again
            await interaction.message.edit(view=TicketPanelView(self.bot, interaction.guild))
        except discord.HTTPException:
            pass


class TicketModal(discord.ui.Modal):
    subject = discord.ui.TextInput(label="Subject", placeholder="Short summary of your problem", max_length=90)
    details = discord.ui.TextInput(label="Details", style=discord.TextStyle.paragraph, max_length=1000,
                                   placeholder="Explain what you need. Include error text, plan name, server name...")

    def __init__(self, cog, key):
        super().__init__(title=CATEGORIES[key][0][:45])
        self.cog, self.key = cog, key

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.create_ticket(interaction, self.key, str(self.subject), str(self.details))


class TicketControls(discord.ui.View):
    def __init__(self, cog, guild=None):
        super().__init__(timeout=None)
        self.cog = cog
        emo = cog.bot.emo
        self.close_btn.emoji = emo.partial("lock", guild)
        self.claim_btn.emoji = emo.partial("crown", guild)
        self.human_btn.emoji = emo.partial("members", guild)
        self.transcript_btn.emoji = emo.partial("billing", guild)
        self.paid_btn.emoji = emo.partial("money", guild)

    @discord.ui.button(label="Close", style=discord.ButtonStyle.danger, custom_id="ax:tkt:close")
    async def close_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.ask_close(interaction)

    @discord.ui.button(label="Claim", style=discord.ButtonStyle.success, custom_id="ax:tkt:claim")
    async def claim_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.claim(interaction)

    @discord.ui.button(label="Talk to Staff", style=discord.ButtonStyle.primary, custom_id="ax:tkt:human")
    async def human_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.talk_to_staff(interaction)

    @discord.ui.button(label="Mark Paid", style=discord.ButtonStyle.success, custom_id="ax:tkt:paid", row=1)
    async def paid_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        hosting = self.cog.bot.get_cog("Hosting")
        if hosting is None:
            return await interaction.response.send_message("Payments module is not loaded.", ephemeral=True)
        await hosting.open_paid_modal(interaction)

    @discord.ui.button(label="Transcript", style=discord.ButtonStyle.secondary, custom_id="ax:tkt:transcript")
    async def transcript_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.send_transcript(interaction)


class RatingView(discord.ui.View):
    def __init__(self, ticket_id):
        super().__init__(timeout=None)
        for n in range(1, 6):
            self.add_item(discord.ui.Button(label=str(n), emoji="⭐", style=discord.ButtonStyle.secondary, custom_id=f"ax:rate:{ticket_id}:{n}"))


class ConfirmClose(discord.ui.View):
    def __init__(self, cog, row, author_id):
        super().__init__(timeout=60)
        self.cog, self.row, self.author_id = cog, row, author_id

    async def interaction_check(self, interaction):
        return interaction.user.id == self.author_id

    @discord.ui.button(label="Yes, close it", style=discord.ButtonStyle.danger)
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(content="Closing...", view=None)
        await self.cog.close_ticket(interaction, self.row)

    @discord.ui.button(label="Keep open", style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(content="Ticket stays open.", view=None)


TRANSCRIPT_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>Ticket #{num} transcript</title>
<style>body{{background:#0f172a;color:#e2e8f0;font-family:Segoe UI,Arial,sans-serif;margin:0;padding:24px}}
h1{{color:#a78bfa}}.m{{padding:10px 14px;margin:8px 0;background:#1e293b;border-left:4px solid #8b5cf6;border-radius:6px}}
.a{{font-weight:600;color:#38bdf8}}.t{{color:#64748b;font-size:12px;margin-left:8px}}
.embed{{margin-top:6px;padding:8px;background:#0f172a;border-left:3px solid #06b6d4;border-radius:4px}}a{{color:#22d3ee}}</style></head>
<body><h1>{brand} ticket #{num}</h1><p>{meta}</p>{rows}</body></html>"""


# ---------------------------------------------------------------------------
class Tickets(commands.Cog, name="Tickets"):
    ticket = app_commands.Group(name="ticket", description="Support ticket system", guild_only=True)

    def __init__(self, bot):
        self.bot = bot
        self.ai_map = {}          # channel_id -> opener user id (tickets where the AI is active)
        self.locks = {}
        self.err_notified = set()

    async def cog_load(self):
        self.bot.add_view(TicketPanelView(self.bot))
        self.bot.add_view(TicketControls(self))
        rows = await self.bot.db.fetchall("SELECT channel_id, user_id FROM tickets WHERE status='open' AND ai_enabled=1")
        self.ai_map = {r["channel_id"]: r["user_id"] for r in rows}

    async def cfg(self, guild_id):
        return await self.bot.db.cfg(guild_id, "tickets", brand.TICKET_DEFAULTS)

    async def get_ticket(self, channel_id):
        return await self.bot.db.fetchone("SELECT * FROM tickets WHERE channel_id=? AND status='open'", (channel_id,))

    async def _fail(self, interaction, title, text):
        em = brand.embed(self.bot, "err", title, text, guild=interaction.guild)
        if interaction.response.is_done():
            await interaction.followup.send(embed=em, ephemeral=True)
        else:
            await interaction.response.send_message(embed=em, ephemeral=True)

    # ------------------------------------------------------------ creating
    async def create_ticket(self, interaction, key, subject, details):
        guild, user = interaction.guild, interaction.user
        await interaction.response.defer(ephemeral=True)
        cfg = await self.cfg(guild.id)
        category = guild.get_channel(cfg["category_id"]) if cfg["category_id"] else None
        staff_role = guild.get_role(cfg["staff_role_id"]) if cfg["staff_role_id"] else None
        if not category or not staff_role:
            return await self._fail(interaction, "Tickets are not set up",
                                    "An admin must run `/ticket setup` first (category + staff role).")
        rows = await self.bot.db.fetchall(
            "SELECT channel_id FROM tickets WHERE guild_id=? AND user_id=? AND status='open'", (guild.id, user.id))
        alive = [r for r in rows if guild.get_channel(r["channel_id"])]
        if len(alive) >= cfg["max_open"]:
            links = ", ".join(f"<#{r['channel_id']}>" for r in alive)
            return await self._fail(interaction, "Ticket limit reached",
                                    f"You already have **{len(alive)}** open ticket(s): {links}\nPlease close one first.")
        label, icon, _ = CATEGORIES[key]
        me = guild.me
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True,
                                              attach_files=True, embed_links=True),
            staff_role: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True,
                                                    attach_files=True, manage_messages=True),
            me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True,
                                            embed_links=True, attach_files=True, manage_channels=True,
                                            manage_messages=True),
        }
        try:
            channel = await guild.create_text_channel(
                name=f"{key}-{slug(user.name)}", category=category, overwrites=overwrites,
                topic=f"{brand.NAME} ticket • {user} ({user.id}) • {label}", reason=f"Ticket by {user}")
        except discord.HTTPException as exc:
            return await self._fail(interaction, "Could not create the ticket channel",
                                    f"Discord said: `{exc.status} {exc.text}`\nStaff: check my **Manage Channels** permission and the category's channel limit.")
        tid = await self.bot.db.execute(
            "INSERT INTO tickets(guild_id,channel_id,user_id,category,subject,created_at) VALUES(?,?,?,?,?,?)",
            (guild.id, channel.id, user.id, key, subject, time.time()))

        safe = discord.utils.escape_mentions(details)
        em = brand.embed(
            self.bot, "premium", f"Ticket #{tid:04d} • {label}",
            f"{{support}} Welcome {user.mention}! Our team has been notified and will be with you shortly.\n"
            f"{brand.LINE}\n**{discord.utils.escape_markdown(subject)}**\n>>> {safe}",
            guild=guild, thumbnail=user.display_avatar.url,
            fields=[("{members} Customer", user.mention, True), (f"{{{icon}}} Category", label, True),
                    ("{robot} AI assistant", "Active — replies instantly" if cfg["ai"] else "Off", True)])
        msg = await channel.send(content=f"{user.mention} {staff_role.mention}", embed=em,
                                 view=TicketControls(self, guild),
                                 allowed_mentions=discord.AllowedMentions(users=[user], roles=[staff_role]))
        try:
            await msg.pin()
        except discord.HTTPException:
            pass
        await interaction.followup.send(embed=brand.embed(
            self.bot, "ok", "Ticket created", f"Head over to {channel.mention} — we'll help you there!", guild=guild),
            ephemeral=True)

        if cfg["ai"]:
            self.ai_map[channel.id] = user.id
            asyncio.create_task(self._ai_respond(channel, user, key, subject,
                                                 [{"role": "user", "content": f"{subject}\n{details}"}]))
        else:
            await self.bot.db.execute("UPDATE tickets SET ai_enabled=0 WHERE channel_id=?", (channel.id,))

    # ------------------------------------------------------------ AI brain
    async def _ai_respond(self, channel, user, key, subject, history):
        lock = self.locks.setdefault(channel.id, asyncio.Lock())
        async with lock:
            row = await self.get_ticket(channel.id)
            if not row or not row["ai_enabled"]:
                return
            if row["ai_replies"] >= MAX_AI_REPLIES:
                return await self.escalate(channel, "The AI reached its reply limit for this ticket.")
            try:
                async with channel.typing():
                    text, escalate = await self.bot.ai.ticket_reply(channel.guild, history, CATEGORIES[key][0],
                                                                    subject, user.display_name)
            except aimod.AIError as exc:
                return await self._ai_failed(channel, exc)
            self.err_notified.discard(channel.id)
            await channel.send(embed=brand.embed(
                self.bot, "ai", "Axion AI", text, guild=channel.guild, ts=False,
                footer=f"{aimod.AI_FOOTER} • press “Talk to Staff” for a human"))
            await self.bot.db.execute("UPDATE tickets SET ai_replies=ai_replies+1 WHERE channel_id=?", (channel.id,))
            if escalate:
                await self.escalate(channel, "The AI asked for a human to take over.")

    async def _ai_failed(self, channel, exc):
        print(f"[ai] ticket {channel.id}: {exc}")
        if channel.id in self.err_notified:
            return
        self.err_notified.add(channel.id)
        rid = (await self.cfg(channel.guild.id))["staff_role_id"]
        role = channel.guild.get_role(rid) if rid else None
        await channel.send(
            content=role.mention if role else None,
            embed=brand.embed(self.bot, "warn", "AI is unavailable right now",
                              f"I could not generate a reply.\n**Reason:** {exc}\n\n{{support}} A staff member will help you manually.",
                              guild=channel.guild),
            allowed_mentions=discord.AllowedMentions(roles=True))

    async def escalate(self, channel, reason):
        await self.bot.db.execute("UPDATE tickets SET ai_enabled=0 WHERE channel_id=?", (channel.id,))
        self.ai_map.pop(channel.id, None)
        rid = (await self.cfg(channel.guild.id))["staff_role_id"]
        role = channel.guild.get_role(rid) if rid else None
        await channel.send(
            content=role.mention if role else None,
            embed=brand.embed(self.bot, "warn", "Staff requested",
                              f"{reason}\n{{members}} A team member will take over this ticket shortly.", guild=channel.guild),
            allowed_mentions=discord.AllowedMentions(roles=True))

    async def pause_ai(self, channel, staff):
        await self.bot.db.execute("UPDATE tickets SET ai_enabled=0 WHERE channel_id=?", (channel.id,))
        self.ai_map.pop(channel.id, None)
        await channel.send(embed=brand.embed(
            self.bot, "info", "Human on the way", f"{{members}} {staff.mention} joined — the AI is paused for this ticket.",
            guild=channel.guild, ts=False))

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot or not message.guild:
            return
        opener = self.ai_map.get(message.channel.id)
        if opener is None:
            return
        if message.author.id != opener:
            if isinstance(message.author, discord.Member) and await brand.is_staff(self.bot, message.author):
                await self.pause_ai(message.channel, message.author)
            return
        if not message.content.strip() and not message.attachments:
            return
        await asyncio.sleep(2.5)                         # debounce bursts of messages
        if message.channel.last_message_id != message.id:
            return
        row = await self.get_ticket(message.channel.id)
        if not row:
            return
        history = []
        async for m in message.channel.history(limit=14):
            if m.author.id == self.bot.user.id:
                if m.embeds and (m.embeds[0].footer.text or "").startswith(aimod.AI_FOOTER):
                    history.append({"role": "assistant", "content": m.embeds[0].description or ""})
            elif m.author.id == opener:
                text = m.content or ""
                if m.attachments:
                    text += " " + " ".join(f"[attachment: {a.filename}]" for a in m.attachments)
                history.append({"role": "user", "content": text.strip()})
        history.reverse()
        label_key = row["category"] if row["category"] in CATEGORIES else "support"
        await self._ai_respond(message.channel, message.author, label_key, row["subject"], history)

    # ------------------------------------------------------------ buttons
    async def ask_close(self, interaction):
        row = await self.get_ticket(interaction.channel.id)
        if not row:
            return await self._fail(interaction, "Not a ticket", "This channel is not an open ticket.")
        if interaction.user.id != row["user_id"] and not await brand.is_staff(self.bot, interaction.user):
            return await self._fail(interaction, "Not allowed", "Only the ticket owner or staff can close this.")
        await interaction.response.send_message("Close this ticket? A transcript will be saved.",
                                                view=ConfirmClose(self, row, interaction.user.id), ephemeral=True)

    async def claim(self, interaction):
        row = await self.get_ticket(interaction.channel.id)
        if not row:
            return await self._fail(interaction, "Not a ticket", "This channel is not an open ticket.")
        if not await brand.is_staff(self.bot, interaction.user):
            return await self._fail(interaction, "Staff only", "Only staff can claim tickets.")
        if row["claimed_by"]:
            return await self._fail(interaction, "Already claimed", f"Claimed by <@{row['claimed_by']}>.")
        await self.bot.db.execute("UPDATE tickets SET claimed_by=?, ai_enabled=0 WHERE channel_id=?",
                                  (interaction.user.id, interaction.channel.id))
        self.ai_map.pop(interaction.channel.id, None)
        await interaction.response.send_message(embed=brand.embed(
            self.bot, "ok", "Ticket claimed",
            f"{{crown}} {interaction.user.mention} is now handling this ticket.", guild=interaction.guild))

    async def talk_to_staff(self, interaction):
        row = await self.get_ticket(interaction.channel.id)
        if not row:
            return await self._fail(interaction, "Not a ticket", "This channel is not an open ticket.")
        if interaction.user.id != row["user_id"] and not await brand.is_staff(self.bot, interaction.user):
            return await self._fail(interaction, "Not allowed", "Only the ticket owner or staff can use this.")
        await interaction.response.send_message(embed=brand.embed(
            self.bot, "info", "Staff called", "{members} The AI is paused and the team has been pinged.",
            guild=interaction.guild))
        await self.escalate(interaction.channel, f"{interaction.user.mention} asked for a human.")

    async def send_transcript(self, interaction):
        row = await self.get_ticket(interaction.channel.id)
        if not row:
            return await self._fail(interaction, "Not a ticket", "This channel is not an open ticket.")
        if interaction.user.id != row["user_id"] and not await brand.is_staff(self.bot, interaction.user):
            return await self._fail(interaction, "Not allowed", "Only the ticket owner or staff can use this.")
        await interaction.response.defer(ephemeral=True)
        file = await self.build_transcript(interaction.channel, row)
        await interaction.followup.send("Here is the transcript so far:", file=file, ephemeral=True)

    async def build_transcript(self, channel, row):
        parts = []
        async for m in channel.history(limit=2000, oldest_first=True):
            stamp = m.created_at.strftime("%Y-%m-%d %H:%M UTC")
            body = html.escape(m.content or "").replace("\n", "<br>")
            for e in m.embeds:
                inner = f"<b>{html.escape(e.title or '')}</b><br>{html.escape(e.description or '').replace(chr(10), '<br>')}"
                body += f"<div class='embed'>{inner}</div>"
            for a in m.attachments:
                body += f"<div><a href='{html.escape(a.url)}'>attachment: {html.escape(a.filename)}</a></div>"
            parts.append(f"<div class='m'><span class='a'>{html.escape(str(m.author))}</span><span class='t'>{stamp}</span><br>{body}</div>")
        meta = html.escape(f"{row['category']} • opened by user {row['user_id']} • {row['subject']}")
        page = TRANSCRIPT_HTML.format(num=f"{row['id']:04d}", brand=html.escape(brand.NAME), meta=meta, rows="".join(parts))
        return discord.File(io.BytesIO(page.encode("utf-8")), filename=f"transcript-{row['id']:04d}.html")

    async def close_ticket(self, interaction, row):
        channel, guild = interaction.channel, interaction.guild
        await self.bot.db.execute("UPDATE tickets SET status='closed', closed_at=? WHERE channel_id=?",
                                  (time.time(), channel.id))
        self.ai_map.pop(channel.id, None)
        file = await self.build_transcript(channel, row)
        cfg = await self.cfg(guild.id)
        em = brand.embed(self.bot, "info", f"Ticket #{row['id']:04d} closed", None, guild=guild, fields=[
            ("{members} Opened by", f"<@{row['user_id']}>", True), ("{crown} Closed by", interaction.user.mention, True),
            ("{ticket} Category", row["category"], True), ("Subject", row["subject"][:200], False)])
        log = guild.get_channel(cfg["log_id"]) if cfg["log_id"] else None
        if log:
            try:
                await log.send(embed=em, file=file)
            except discord.HTTPException as exc:
                print(f"[tickets] cannot post transcript: {exc}")
        else:
            print(f"[tickets] no ticket log channel configured in {guild.id}; transcript not stored")
        member = guild.get_member(row["user_id"])
        if member:
            try:
                file2 = await self.build_transcript(channel, row)
                await member.send(embed=brand.embed(
                    self.bot, "info", "Your ticket was closed",
                    f"Your ticket **{row['subject'][:100]}** in **{guild.name}** has been closed.\nTranscript attached.",
                    guild=guild), file=file2)
                await member.send(embed=brand.embed(
                    self.bot, "premium", "How was our support?",
                    f"{{star}} Rate ticket **#{row['id']:04d}** from 1 to 5 — it helps us improve!", guild=guild),
                    view=RatingView(row["id"]))
            except discord.HTTPException:
                pass                                          # DMs closed — normal
        try:
            await channel.send(embed=brand.embed(self.bot, "warn", "Closing in 5 seconds",
                                                 "{lock} Thanks for choosing us!", guild=guild))
        except discord.HTTPException:
            pass
        await asyncio.sleep(5)
        try:
            await channel.delete(reason=f"Ticket closed by {interaction.user}")
        except discord.HTTPException as exc:
            print(f"[tickets] could not delete {channel.id}: {exc}")

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel):
        await self.bot.db.execute("UPDATE tickets SET status='closed', closed_at=? WHERE channel_id=? AND status='open'",
                                  (time.time(), channel.id))
        self.ai_map.pop(channel.id, None)

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type != discord.InteractionType.component:
            return
        cid = (interaction.data or {}).get("custom_id", "")
        if not cid.startswith("ax:rate:"):
            return
        _, _, tid, n = cid.split(":")
        row = await self.bot.db.fetchone("SELECT * FROM tickets WHERE id=?", (int(tid),))
        if not row or row["user_id"] != interaction.user.id:
            return await interaction.response.send_message("This rating is not for you.", ephemeral=True)
        if row["rating"]:
            return await interaction.response.send_message("You already rated this ticket. Thank you!", ephemeral=True)
        await self.bot.db.execute("UPDATE tickets SET rating=? WHERE id=?", (int(n), int(tid)))
        guild = self.bot.get_guild(row["guild_id"])
        await interaction.response.edit_message(embed=brand.embed(
            self.bot, "ok", "Thank you!", f"You rated this ticket {'⭐' * int(n)}", guild=guild), view=None)
        if guild:
            cfg = await self.cfg(guild.id)
            log = guild.get_channel(cfg["log_id"]) if cfg["log_id"] else None
            if log:
                try:
                    await log.send(embed=brand.embed(self.bot, "premium", f"Ticket #{row['id']:04d} rated",
                                                     f"{'⭐' * int(n)}{'☆' * (5 - int(n))}  by <@{row['user_id']}>", guild=guild))
                except discord.HTTPException:
                    pass

    # ------------------------------------------------------------ commands
    @ticket.command(name="setup", description="Configure the ticket system")
    @app_commands.describe(category="Category where ticket channels are created", log_channel="Where transcripts are sent",
                           staff_role="Role that can see and answer tickets", max_open="Open tickets allowed per user",
                           ai="Let the self-hosted AI answer first")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setup_(self, interaction: discord.Interaction, category: discord.CategoryChannel,
                     log_channel: discord.TextChannel, staff_role: discord.Role,
                     max_open: app_commands.Range[int, 1, 5] = 2, ai: bool = True):
        await self.bot.db.cfg_set(interaction.guild.id, "tickets", brand.TICKET_DEFAULTS, category_id=category.id,
                                  log_id=log_channel.id, staff_role_id=staff_role.id, max_open=max_open, ai=ai)
        missing = []
        perms = category.permissions_for(interaction.guild.me)
        if not perms.manage_channels:
            missing.append("Manage Channels (in that category)")
        if not log_channel.permissions_for(interaction.guild.me).send_messages:
            missing.append("Send Messages (in the log channel)")
        desc = (f"{{ticket}} Category: **{category.name}**\n{{billing}} Transcripts: {log_channel.mention}\n"
                f"{{crown}} Staff role: {staff_role.mention}\n{{robot}} AI first-responder: **{'on' if ai else 'off'}**\n"
                f"{{members}} Max open per user: **{max_open}**\n\nNow post the panel with `/ticket panel`.")
        if missing:
            desc += "\n\n⚠️ I am missing: " + ", ".join(missing)
        await interaction.response.send_message(embed=brand.embed(self.bot, "warn" if missing else "ok", "Tickets configured",
                                                                  desc, guild=interaction.guild), ephemeral=True)

    @ticket.command(name="panel", description="Post the ticket panel (AI can write the title and description)")
    @app_commands.describe(channel="Where to post it", use_ai="Let the AI write a fresh title + description",
                           brief="Optional: tell the AI what the panel should say")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def panel(self, interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None,
                    use_ai: bool = True, brief: Optional[str] = None):
        guild = interaction.guild
        cfg = await self.cfg(guild.id)
        if not cfg["category_id"] or not cfg["staff_role_id"]:
            return await self._fail(interaction, "Run setup first", "Use `/ticket setup` before posting a panel.")
        target = channel or interaction.channel
        if not target.permissions_for(guild.me).send_messages:
            return await self._fail(interaction, "Missing permission", f"I cannot send messages in {target.mention}.")
        await interaction.response.defer(ephemeral=True)

        async def post(embed):
            msg = await target.send(embed=embed, view=TicketPanelView(self.bot, guild))
            return msg.jump_url

        if use_ai:
            brief = brief or ("A support ticket panel. People pick a category from a dropdown: Sales & Plans, Technical "
                              "Support, Billing & Payments, Partnership, Report/Abuse. An AI assistant answers instantly "
                              "and staff steps in when needed. Support is fast.")
            await aimod.start_preview(self.bot, interaction, "ticket_panel", brief, None, post)
        else:
            await aimod.start_preview(self.bot, interaction, "ticket_panel", brief or "", None, post,
                                      data=dict(DEFAULT_PANEL, fields=[]))

    async def _in_ticket(self, interaction):
        row = await self.get_ticket(interaction.channel.id)
        if not row:
            await self._fail(interaction, "Not a ticket", "Use this inside an open ticket channel.")
            return None
        if not await brand.is_staff(self.bot, interaction.user):
            await self._fail(interaction, "Staff only", "Only staff can do this.")
            return None
        return row

    @ticket.command(name="add", description="Add a member to this ticket")
    async def add(self, interaction: discord.Interaction, member: discord.Member):
        if not await self._in_ticket(interaction):
            return
        await interaction.channel.set_permissions(member, view_channel=True, send_messages=True,
                                                  read_message_history=True, attach_files=True)
        await interaction.response.send_message(embed=brand.embed(
            self.bot, "ok", "Member added", f"{member.mention} can now see this ticket.", guild=interaction.guild))

    @ticket.command(name="remove", description="Remove a member from this ticket")
    async def remove(self, interaction: discord.Interaction, member: discord.Member):
        row = await self._in_ticket(interaction)
        if not row:
            return
        if member.id == row["user_id"]:
            return await self._fail(interaction, "Not allowed", "You cannot remove the ticket owner.")
        await interaction.channel.set_permissions(member, overwrite=None)
        await interaction.response.send_message(embed=brand.embed(
            self.bot, "ok", "Member removed", f"{member.mention} no longer has access.", guild=interaction.guild))

    @ticket.command(name="rename", description="Rename this ticket channel")
    async def rename(self, interaction: discord.Interaction, name: str):
        if not await self._in_ticket(interaction):
            return
        await interaction.response.defer()
        await interaction.channel.edit(name=slug(name) or "ticket")
        await interaction.followup.send(embed=brand.embed(self.bot, "ok", "Renamed", f"Now `#{slug(name)}`.",
                                                          guild=interaction.guild))

    @ticket.command(name="ai", description="Turn the AI on or off for this ticket")
    async def ai_toggle(self, interaction: discord.Interaction, enabled: bool):
        row = await self._in_ticket(interaction)
        if not row:
            return
        await self.bot.db.execute("UPDATE tickets SET ai_enabled=? WHERE channel_id=?",
                                  (1 if enabled else 0, interaction.channel.id))
        if enabled:
            self.ai_map[interaction.channel.id] = row["user_id"]
        else:
            self.ai_map.pop(interaction.channel.id, None)
        await interaction.response.send_message(embed=brand.embed(
            self.bot, "ok", "AI updated", f"{{robot}} AI is now **{'on' if enabled else 'off'}** in this ticket.",
            guild=interaction.guild))

    @ticket.command(name="stats", description="Ticket statistics")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def stats(self, interaction: discord.Interaction):
        g = interaction.guild.id
        counts = await self.bot.db.fetchall("SELECT status, COUNT(*) c FROM tickets WHERE guild_id=? GROUP BY status", (g,))
        by = {r["status"]: r["c"] for r in counts}
        top = await self.bot.db.fetchall(
            "SELECT claimed_by, COUNT(*) c FROM tickets WHERE guild_id=? AND claimed_by IS NOT NULL "
            "GROUP BY claimed_by ORDER BY c DESC LIMIT 5", (g,))
        board = "\n".join(f"{i + 1}. <@{r['claimed_by']}> — {r['c']}" for i, r in enumerate(top)) or "No claims yet."
        rt = await self.bot.db.fetchone("SELECT AVG(rating) a, COUNT(rating) c FROM tickets WHERE guild_id=? AND rating IS NOT NULL", (g,))
        avg_text = f"{rt['a']:.2f} / 5 ({rt['c']} ratings)" if rt["c"] else "no ratings yet"
        await interaction.response.send_message(embed=brand.embed(
            self.bot, "info", "Ticket stats", None, guild=interaction.guild,
            fields=[("{online} Open", str(by.get("open", 0)), True), ("{check} Closed", str(by.get("closed", 0)), True),
                    ("{star} Avg rating", avg_text, True), ("{crown} Top staff", board, False)]), ephemeral=True)


async def setup(bot):
    await bot.add_cog(Tickets(bot))
