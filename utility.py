"""Utility & engagement: polls, suggestions, reminders, AFK, embed builder, reaction-role panels, sticky messages,
auto-responders, starboard, temp voice channels, fun commands."""
import json
import random
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import brand

NUM = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
STAR_DEFAULTS = {"channel_id": None, "threshold": 3}
TEMPVC_DEFAULTS = {"lobby_id": None, "category_id": None}
SUGGEST_DEFAULTS = {"channel_id": None}
EIGHT = ["Yes, definitely.", "It is certain.", "Most likely.", "Ask again later.", "Cannot predict now.", "Don't count on it.",
         "My sources say no.", "Very doubtful.", "Without a doubt.", "Signs point to yes."]


def bar(pct, n=12):
    filled = round(pct / 100 * n)
    return "█" * filled + "░" * (n - filled)


class EmbedModal(discord.ui.Modal, title="Embed builder"):
    e_title = discord.ui.TextInput(label="Title (custom emojis: {rocket})", max_length=256, required=False)
    e_desc = discord.ui.TextInput(label="Description", style=discord.TextStyle.paragraph, max_length=3500)
    e_color = discord.ui.TextInput(label="Hex colour (optional)", placeholder="#8b5cf6", max_length=7, required=False)
    e_image = discord.ui.TextInput(label="Image URL (optional)", required=False, max_length=300)

    def __init__(self, bot, channel):
        super().__init__()
        self.bot, self.channel = bot, channel

    async def on_submit(self, interaction: discord.Interaction):
        g = interaction.guild
        try:
            color = int(str(self.e_color).strip().lstrip("#") or "8b5cf6", 16)
        except ValueError:
            color = 0x8B5CF6
        em = discord.Embed(title=self.bot.emo.fmt(str(self.e_title), g) or None,
                           description=self.bot.emo.fmt(str(self.e_desc), g), color=color)
        if str(self.e_image).startswith("http"):
            em.set_image(url=str(self.e_image))
        em.set_footer(text=f"{brand.NAME} • Premium Hosting")
        try:
            msg = await self.channel.send(embed=em)
        except discord.HTTPException as exc:
            return await interaction.response.send_message(embed=brand.embed(
                self.bot, "err", "Could not post", exc.text, guild=g), ephemeral=True)
        await interaction.response.send_message(embed=brand.embed(self.bot, "ok", "Posted", msg.jump_url, guild=g), ephemeral=True)


class RolePanelPreview(discord.ui.View):
    def __init__(self, cog, author_id, guild, roles, brief, lang, data, note, channel):
        super().__init__(timeout=900)
        self.cog, self.author_id, self.guild, self.roles = cog, author_id, guild, roles
        self.brief, self.lang, self.data, self.note, self.channel = brief, lang, data, note, channel
        self.message = None

    def preview(self):
        return self.cog._panel_parts(self.guild, self.roles, self.data)

    def content(self):
        return f"⚠️ {self.note}" if self.note else f"Preview — posts to {self.channel.mention} when you press **Post**."

    async def interaction_check(self, interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This preview belongs to someone else.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Post", style=discord.ButtonStyle.success, emoji="✅")
    async def post(self, interaction: discord.Interaction, button: discord.ui.Button):
        em, view = self.preview()
        try:
            msg = await self.channel.send(embed=em, view=view)
        except discord.HTTPException as exc:
            return await interaction.response.send_message(f"Could not post: {exc.text}", ephemeral=True)
        self.stop()
        await interaction.response.edit_message(content=f"✅ Posted {msg.jump_url}", embed=None, view=None)

    @discord.ui.button(label="Regenerate", style=discord.ButtonStyle.primary, emoji="🔄")
    async def regen(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="🤖 Writing a new version...", view=None)
        self.data, self.note = await self.cog.bot.ai.make_role_panel(self.guild, [r.name for r in self.roles], self.brief, self.lang)
        await interaction.edit_original_response(content=self.content(), embed=self.preview()[0], view=self)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger, emoji="✖️")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(content="Cancelled.", embed=None, view=None)


class Utility(commands.Cog, name="Utility"):
    poll_g = app_commands.Group(name="poll", description="Polls", guild_only=True)
    sticky = app_commands.Group(name="sticky", description="Sticky messages",
                                default_permissions=discord.Permissions(manage_messages=True), guild_only=True)
    responder = app_commands.Group(name="responder", description="Auto-responders / FAQ",
                                   default_permissions=discord.Permissions(manage_guild=True), guild_only=True)
    rolepanel = app_commands.Group(name="rolepanel", description="Self-assign role panels",
                                   default_permissions=discord.Permissions(manage_roles=True), guild_only=True)
    star = app_commands.Group(name="starboard", description="Starboard",
                              default_permissions=discord.Permissions(manage_guild=True), guild_only=True)
    tempvc = app_commands.Group(name="tempvc", description="Join-to-create voice channels",
                                default_permissions=discord.Permissions(manage_channels=True), guild_only=True)
    suggestion = app_commands.Group(name="suggestion", description="Suggestions", guild_only=True)

    def __init__(self, bot):
        self.bot = bot
        self.temp_channels = set()
        self.sticky_cool = {}
        self.resp_cool = {}

    async def cog_load(self):
        self.remind_loop.start()
        self.poll_loop.start()
        self.stats_loop.start()

    async def cog_unload(self):
        self.remind_loop.cancel()
        self.poll_loop.cancel()
        self.stats_loop.cancel()

    # ================================================================== listeners
    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot or not message.guild:
            return
        g, db = message.guild, self.bot.db
        # --- AFK: clear own, notify mentions
        row = await db.fetchone("SELECT since FROM afk WHERE guild_id=? AND user_id=?", (g.id, message.author.id))
        if row:
            await db.execute("DELETE FROM afk WHERE guild_id=? AND user_id=?", (g.id, message.author.id))
            try:
                await message.reply(embed=brand.embed(self.bot, "ok", "Welcome back!",
                                                      f"AFK removed. You were away for **{brand.human_duration(time.time() - row['since'])}**.",
                                                      guild=g, ts=False), delete_after=8, mention_author=False)
            except discord.HTTPException:
                pass
        for m in message.mentions[:3]:
            a = await db.fetchone("SELECT reason, since FROM afk WHERE guild_id=? AND user_id=?", (g.id, m.id))
            if a:
                try:
                    await message.reply(f"💤 **{m.display_name}** is AFK: {discord.utils.escape_mentions(a['reason'])} (<t:{int(a['since'])}:R>)",
                                        delete_after=10, mention_author=False)
                except discord.HTTPException:
                    pass
        # --- auto responders
        low = message.content.lower()
        if low and time.monotonic() - self.resp_cool.get(message.channel.id, 0) > 5:
            for r in await db.fetchall("SELECT trigger, reply FROM responders WHERE guild_id=?", (g.id,)):
                if r["trigger"] in low:
                    self.resp_cool[message.channel.id] = time.monotonic()
                    try:
                        await message.reply(self.bot.emo.fmt(r["reply"], g)[:1900], mention_author=False,
                                            allowed_mentions=discord.AllowedMentions.none())
                    except discord.HTTPException:
                        pass
                    break
        # --- sticky message
        st = await db.fetchone("SELECT * FROM stickies WHERE channel_id=?", (message.channel.id,))
        if st and time.monotonic() - self.sticky_cool.get(message.channel.id, 0) > 4:
            self.sticky_cool[message.channel.id] = time.monotonic()
            if st["last_msg_id"]:
                try:
                    await (await message.channel.fetch_message(st["last_msg_id"])).delete()
                except discord.HTTPException:
                    pass
            try:
                new = await message.channel.send(embed=brand.embed(self.bot, "info", "Pinned notice", st["text"], guild=g, ts=False))
                await db.execute("UPDATE stickies SET last_msg_id=? WHERE channel_id=?", (new.id, message.channel.id))
            except discord.HTTPException:
                pass

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type != discord.InteractionType.component:
            return
        cid = (interaction.data or {}).get("custom_id", "")
        if cid.startswith("ax:rr:"):
            await self._role_button(interaction, int(cid.split(":")[2]))
        elif cid.startswith("ax:poll:"):
            await self._poll_vote(interaction, int(cid.split(":")[2]))
        elif cid.startswith("ax:sg:"):
            await self._suggest_button(interaction, cid.split(":")[2])

    # ================================================================== fun / info
    @app_commands.command(name="avatar", description="Show someone's avatar")
    async def avatar(self, interaction: discord.Interaction, member: Optional[discord.Member] = None):
        m = member or interaction.user
        await interaction.response.send_message(embed=brand.embed(
            self.bot, "info", f"{m.display_name}'s avatar", None, guild=interaction.guild, image=m.display_avatar.url))

    @app_commands.command(name="8ball", description="Ask the magic 8-ball")
    async def eightball(self, interaction: discord.Interaction, question: str):
        await interaction.response.send_message(embed=brand.embed(
            self.bot, "premium", "Magic 8-ball", f"**Q:** {discord.utils.escape_markdown(question[:200])}\n**A:** {random.choice(EIGHT)}",
            guild=interaction.guild))

    @app_commands.command(name="roll", description="Roll dice, e.g. 2d6")
    async def roll(self, interaction: discord.Interaction, dice: str = "1d6"):
        try:
            n, s = dice.lower().split("d")
            n, s = int(n or 1), int(s)
            assert 1 <= n <= 20 and 2 <= s <= 1000
        except Exception:
            return await interaction.response.send_message(embed=brand.embed(
                self.bot, "err", "Bad dice", "Use `2d6`, `1d20` etc. (max 20 dice, 1000 sides).", guild=interaction.guild), ephemeral=True)
        rolls = [random.randint(1, s) for _ in range(n)]
        await interaction.response.send_message(embed=brand.embed(
            self.bot, "ok", f"🎲 {dice}", f"{rolls} → **{sum(rolls)}**", guild=interaction.guild))

    @app_commands.command(name="say", description="Make the bot say something (staff)")
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.guild_only()
    async def say(self, interaction: discord.Interaction, text: str, channel: Optional[discord.TextChannel] = None):
        ch = channel or interaction.channel
        try:
            await ch.send(self.bot.emo.fmt(text, interaction.guild)[:1900], allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as exc:
            return await interaction.response.send_message(embed=brand.embed(self.bot, "err", "Could not send", exc.text, guild=interaction.guild), ephemeral=True)
        await interaction.response.send_message("Sent.", ephemeral=True)

    @app_commands.command(name="embed", description="Build and post your own custom embed (staff)")
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.guild_only()
    async def embed_cmd(self, interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
        await interaction.response.send_modal(EmbedModal(self.bot, channel or interaction.channel))

    @app_commands.command(name="afk", description="Set yourself AFK")
    @app_commands.guild_only()
    async def afk(self, interaction: discord.Interaction, reason: str = "AFK"):
        await self.bot.db.execute("INSERT OR REPLACE INTO afk(guild_id,user_id,reason,since) VALUES(?,?,?,?)",
                                  (interaction.guild.id, interaction.user.id, reason[:100], time.time()))
        await interaction.response.send_message(embed=brand.embed(
            self.bot, "ok", "You are AFK", f"💤 {discord.utils.escape_markdown(reason[:100])}", guild=interaction.guild), ephemeral=True)

    # ================================================================== reminders
    @app_commands.command(name="remind", description="Remind me later, e.g. 2h30m")
    async def remind(self, interaction: discord.Interaction, when: str, text: str):
        secs = brand.parse_duration(when)
        if not secs or secs > 90 * 86400:
            return await interaction.response.send_message(embed=brand.embed(
                self.bot, "err", "Bad time", "Use `10m`, `2h`, `1d` (max 90 days).", guild=interaction.guild), ephemeral=True)
        await self.bot.db.execute("INSERT INTO reminders(user_id,guild_id,channel_id,text,due) VALUES(?,?,?,?,?)",
                                  (interaction.user.id, interaction.guild_id or 0, interaction.channel_id, text[:300], time.time() + secs))
        await interaction.response.send_message(embed=brand.embed(
            self.bot, "ok", "Reminder set", f"{{bell}} I will remind you <t:{int(time.time() + secs)}:R>.", guild=interaction.guild), ephemeral=True)

    @tasks.loop(seconds=20)
    async def remind_loop(self):
        rows = await self.bot.db.fetchall("SELECT * FROM reminders WHERE due<=?", (time.time(),))
        for r in rows:
            await self.bot.db.execute("DELETE FROM reminders WHERE id=?", (r["id"],))
            g = self.bot.get_guild(r["guild_id"])
            em = brand.embed(self.bot, "premium", "Reminder", r["text"], guild=g)
            user = self.bot.get_user(r["user_id"]) or await self.bot.fetch_user(r["user_id"])
            try:
                await user.send(embed=em)
            except discord.HTTPException:
                ch = self.bot.get_channel(r["channel_id"])
                if ch:
                    try:
                        await ch.send(content=user.mention, embed=em, allowed_mentions=discord.AllowedMentions(users=[user]))
                    except discord.HTTPException as exc:
                        print(f"[remind] could not deliver reminder {r['id']}: {exc}")

    @remind_loop.before_loop
    async def _r_ready(self):
        await self.bot.wait_until_ready()

    # ================================================================== polls
    def _poll_embed(self, g, question, options, counts, ends_at=None, ended=False):
        total = sum(counts) or 0
        lines = []
        for i, o in enumerate(options):
            pct = (counts[i] / total * 100) if total else 0
            lines.append(f"{NUM[i]} **{discord.utils.escape_markdown(o)}**\n`{bar(pct)}` {counts[i]} • {pct:.0f}%")
        foot = f"\n{{bell}} {'Ended' if ended else 'Ends'} <t:{int(ends_at)}:R>" if ends_at else ""
        return brand.embed(self.bot, "premium", ("Poll ended: " if ended else "Poll: ") + question[:200],
                           "\n".join(lines) + f"\n\n{{members}} **{total}** vote(s)" + foot, guild=g, ts=False)

    async def _counts(self, mid, n):
        rows = await self.bot.db.fetchall("SELECT choice, COUNT(*) c FROM poll_votes WHERE message_id=? GROUP BY choice", (mid,))
        counts = [0] * n
        for r in rows:
            if 0 <= r["choice"] < n:
                counts[r["choice"]] = r["c"]
        return counts

    @poll_g.command(name="create", description="Create a poll with up to 5 options")
    @app_commands.describe(duration="e.g. 1h, 1d (default 1d)")
    async def poll_create(self, interaction: discord.Interaction, question: str, option1: str, option2: str,
                          option3: Optional[str] = None, option4: Optional[str] = None, option5: Optional[str] = None,
                          duration: str = "1d"):
        secs = brand.parse_duration(duration)
        if not secs or secs > 14 * 86400:
            return await interaction.response.send_message(embed=brand.embed(
                self.bot, "err", "Bad duration", "Use `1h`, `1d` (max 14 days).", guild=interaction.guild), ephemeral=True)
        options = [o[:60] for o in (option1, option2, option3, option4, option5) if o]
        ends = time.time() + secs
        view = discord.ui.View(timeout=None)
        for i, o in enumerate(options):
            view.add_item(discord.ui.Button(label=o[:40], emoji=NUM[i], style=discord.ButtonStyle.primary, custom_id=f"ax:poll:{i}"))
        await interaction.response.send_message(embed=self._poll_embed(interaction.guild, question, options, [0] * len(options), ends), view=view)
        msg = await interaction.original_response()
        await self.bot.db.execute("INSERT INTO polls(message_id,guild_id,question,options,ends_at,channel_id) VALUES(?,?,?,?,?,?)",
                                  (msg.id, interaction.guild.id, question, json.dumps(options), ends, msg.channel.id))

    async def _poll_vote(self, interaction, idx):
        row = await self.bot.db.fetchone("SELECT * FROM polls WHERE message_id=?", (interaction.message.id,))
        if not row or row["ended"]:
            return await interaction.response.send_message("This poll has ended.", ephemeral=True)
        await self.bot.db.execute("INSERT OR REPLACE INTO poll_votes(message_id,user_id,choice) VALUES(?,?,?)",
                                  (row["message_id"], interaction.user.id, idx))
        options = json.loads(row["options"])
        counts = await self._counts(row["message_id"], len(options))
        await interaction.response.edit_message(embed=self._poll_embed(interaction.guild, row["question"], options, counts, row["ends_at"]))

    @tasks.loop(seconds=20)
    async def poll_loop(self):
        for row in await self.bot.db.fetchall("SELECT * FROM polls WHERE ended=0 AND ends_at<=?", (time.time(),)):
            await self.bot.db.execute("UPDATE polls SET ended=1 WHERE message_id=?", (row["message_id"],))
            g = self.bot.get_guild(row["guild_id"])
            ch = g.get_channel(row["channel_id"]) if g else None
            if not ch:
                continue
            options = json.loads(row["options"])
            counts = await self._counts(row["message_id"], len(options))
            try:
                msg = await ch.fetch_message(row["message_id"])
                await msg.edit(embed=self._poll_embed(g, row["question"], options, counts, row["ends_at"], ended=True), view=None)
            except discord.HTTPException as exc:
                print(f"[poll] could not close poll {row['message_id']}: {exc}")

    @poll_loop.before_loop
    async def _p_ready(self):
        await self.bot.wait_until_ready()

    # ================================================================== suggestions
    @suggestion.command(name="channel", description="Set the suggestions channel (admin)")
    @app_commands.default_permissions(manage_guild=True)
    async def sg_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await self.bot.db.cfg_set(interaction.guild.id, "suggest", SUGGEST_DEFAULTS, channel_id=channel.id)
        await interaction.response.send_message(embed=brand.embed(
            self.bot, "ok", "Suggestions channel set", channel.mention, guild=interaction.guild), ephemeral=True)

    @suggestion.command(name="new", description="Suggest something for the server")
    async def sg_new(self, interaction: discord.Interaction, idea: str):
        g = interaction.guild
        cfg = await self.bot.db.cfg(g.id, "suggest", SUGGEST_DEFAULTS)
        ch = g.get_channel(cfg["channel_id"]) if cfg["channel_id"] else None
        if not ch:
            return await interaction.response.send_message(embed=brand.embed(
                self.bot, "err", "Not set up", "An admin must run `/suggestion channel` first.", guild=g), ephemeral=True)
        em = brand.embed(self.bot, "premium", "New suggestion", discord.utils.escape_markdown(idea[:1800]), guild=g, thumbnail=interaction.user.display_avatar.url,
                         fields=[("Author", interaction.user.mention, True), ("Status", "🕓 Open", True), ("Votes", "👍 0  •  👎 0", True)])
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(emoji="👍", label="Upvote", style=discord.ButtonStyle.success, custom_id="ax:sg:up"))
        view.add_item(discord.ui.Button(emoji="👎", label="Downvote", style=discord.ButtonStyle.danger, custom_id="ax:sg:down"))
        view.add_item(discord.ui.Button(label="Approve", style=discord.ButtonStyle.secondary, custom_id="ax:sg:approve"))
        view.add_item(discord.ui.Button(label="Deny", style=discord.ButtonStyle.secondary, custom_id="ax:sg:deny"))
        try:
            msg = await ch.send(embed=em, view=view)
        except discord.HTTPException as exc:
            return await interaction.response.send_message(embed=brand.embed(self.bot, "err", "Could not post", exc.text, guild=g), ephemeral=True)
        await self.bot.db.execute("INSERT INTO suggestions(guild_id,message_id,user_id,text) VALUES(?,?,?,?)",
                                  (g.id, msg.id, interaction.user.id, idea[:1800]))
        await interaction.response.send_message(embed=brand.embed(self.bot, "ok", "Suggestion sent", msg.jump_url, guild=g), ephemeral=True)

    async def _suggest_button(self, interaction, action):
        row = await self.bot.db.fetchone("SELECT * FROM suggestions WHERE message_id=?", (interaction.message.id,))
        if not row:
            return await interaction.response.send_message("Suggestion not found.", ephemeral=True)
        db = self.bot.db
        if action in ("approve", "deny"):
            if not interaction.user.guild_permissions.manage_guild:
                return await interaction.response.send_message("Only staff can do that.", ephemeral=True)
            status = "approved" if action == "approve" else "denied"
            await db.execute("UPDATE suggestions SET status=? WHERE message_id=?", (status, row["message_id"]))
        else:
            if row["status"] != "open":
                return await interaction.response.send_message("Voting is closed.", ephemeral=True)
            await db.execute("INSERT OR REPLACE INTO suggestion_votes(message_id,user_id,vote) VALUES(?,?,?)",
                             (row["message_id"], interaction.user.id, 1 if action == "up" else -1))
        status = (await db.fetchone("SELECT status FROM suggestions WHERE message_id=?", (row["message_id"],)))["status"]
        up = (await db.fetchone("SELECT COUNT(*) c FROM suggestion_votes WHERE message_id=? AND vote=1", (row["message_id"],)))["c"]
        down = (await db.fetchone("SELECT COUNT(*) c FROM suggestion_votes WHERE message_id=? AND vote=-1", (row["message_id"],)))["c"]
        em = interaction.message.embeds[0]
        label = {"open": "🕓 Open", "approved": "✅ Approved", "denied": "❌ Denied"}[status]
        color = {"open": 0x8B5CF6, "approved": 0x22C55E, "denied": 0xEF4444}[status]
        em.color = color
        em.clear_fields()
        em.add_field(name="Author", value=f"<@{row['user_id']}>", inline=True)
        em.add_field(name="Status", value=label, inline=True)
        em.add_field(name="Votes", value=f"👍 {up}  •  👎 {down}", inline=True)
        view = None if status != "open" else discord.ui.View.from_message(interaction.message)
        await interaction.response.edit_message(embed=em, view=view)

    # ================================================================== reaction-role panels
    @rolepanel.command(name="create", description="Post a panel where members click buttons to get roles")
    @app_commands.describe(title="Panel title", role1="First role", role2="Second role", role3="Third role", role4="Fourth role",
                           role5="Fifth role", channel="Where to post")
    async def rp_create(self, interaction: discord.Interaction, title: str, role1: discord.Role, role2: Optional[discord.Role] = None,
                        role3: Optional[discord.Role] = None, role4: Optional[discord.Role] = None, role5: Optional[discord.Role] = None,
                        channel: Optional[discord.TextChannel] = None):
        g = interaction.guild
        roles = [r for r in (role1, role2, role3, role4, role5) if r]
        bad = [r.name for r in roles if r >= g.me.top_role or r.managed or r.is_default()]
        if bad:
            return await interaction.response.send_message(embed=brand.embed(
                self.bot, "err", "Role problem", "I cannot hand out: " + ", ".join(bad) + "\nMove my role above them.", guild=g), ephemeral=True)
        view = discord.ui.View(timeout=None)
        for r in roles:
            view.add_item(discord.ui.Button(label=r.name[:40], style=discord.ButtonStyle.secondary, custom_id=f"ax:rr:{r.id}"))
        em = brand.embed(self.bot, "premium", title, "{star} Click a button to **get** or **remove** a role.\n" + "\n".join(f"• {r.mention}" for r in roles), guild=g)
        try:
            msg = await (channel or interaction.channel).send(embed=em, view=view)
        except discord.HTTPException as exc:
            return await interaction.response.send_message(embed=brand.embed(self.bot, "err", "Could not post", exc.text, guild=g), ephemeral=True)
        await interaction.response.send_message(embed=brand.embed(self.bot, "ok", "Role panel posted", msg.jump_url, guild=g), ephemeral=True)

    async def _role_button(self, interaction, role_id):
        g = interaction.guild
        role = g.get_role(role_id)
        if not role:
            return await interaction.response.send_message("That role no longer exists.", ephemeral=True)
        try:
            if role in interaction.user.roles:
                await interaction.user.remove_roles(role, reason="Role panel")
                text = f"Removed {role.mention}."
            else:
                await interaction.user.add_roles(role, reason="Role panel")
                text = f"You now have {role.mention}."
        except discord.HTTPException as exc:
            return await interaction.response.send_message(f"I could not change that role: {exc.text}", ephemeral=True)
        await interaction.response.send_message(embed=brand.embed(self.bot, "ok", "Roles updated", text, guild=g), ephemeral=True)

    # ================================================================== AI role panels + auto roles
    autorole = app_commands.Group(name="autorole", description="Roles given automatically when someone joins",
                                  default_permissions=discord.Permissions(manage_roles=True), guild_only=True)
    serverstats = app_commands.Group(name="serverstats", description="Live member-count voice channels",
                                     default_permissions=discord.Permissions(manage_channels=True), guild_only=True)

    def _panel_parts(self, g, roles, data):
        emo = self.bot.emo
        lines = [f"{emo.get(r['emoji'], g)} {roles[i].mention} — {r['blurb']}" for i, r in enumerate(data["roles"])]
        em = discord.Embed(title=emo.fmt(data["title"], g)[:256], description=emo.fmt(data["description"], g) + f"\n{brand.LINE}\n" + "\n".join(lines),
                           color=0x8B5CF6)
        em.set_footer(text=f"{brand.NAME} • Premium Hosting")
        em.timestamp = discord.utils.utcnow()
        view = discord.ui.View(timeout=None)
        for i, r in enumerate(data["roles"]):
            view.add_item(discord.ui.Button(label=roles[i].name[:40], emoji=emo.partial(r["emoji"], g),
                                            style=discord.ButtonStyle.secondary, custom_id=f"ax:rr:{roles[i].id}"))
        return em, view

    @rolepanel.command(name="ai", description="Give me only the roles — AI writes the title, description and emojis")
    @app_commands.describe(brief="Optional hint, e.g. 'notification roles'", lang="Language of the text", channel="Where to post")
    @app_commands.choices(lang=[app_commands.Choice(name="Hinglish", value="hinglish"), app_commands.Choice(name="English", value="english")])
    async def rp_ai(self, interaction: discord.Interaction, role1: discord.Role, role2: Optional[discord.Role] = None,
                    role3: Optional[discord.Role] = None, role4: Optional[discord.Role] = None, role5: Optional[discord.Role] = None,
                    role6: Optional[discord.Role] = None, role7: Optional[discord.Role] = None, role8: Optional[discord.Role] = None,
                    brief: str = "", lang: Optional[app_commands.Choice[str]] = None, channel: Optional[discord.TextChannel] = None):
        g = interaction.guild
        roles = []
        for r in (role1, role2, role3, role4, role5, role6, role7, role8):
            if r and r not in roles:
                roles.append(r)
        bad = [r.name for r in roles if r >= g.me.top_role or r.managed or r.is_default()]
        if bad:
            return await interaction.response.send_message(embed=brand.embed(
                self.bot, "err", "Role problem", "I cannot hand out: " + ", ".join(bad) + "\nMove my role above them.", guild=g), ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        target = channel or interaction.channel
        data, note = await self.bot.ai.make_role_panel(g, [r.name for r in roles], brief, lang.value if lang else None)
        view = RolePanelPreview(self, interaction.user.id, g, roles, brief, lang.value if lang else None, data, note, target)
        view.message = await interaction.followup.send(content=view.content(), embed=view.preview()[0], view=view, ephemeral=True, wait=True)

    @autorole.command(name="add", description="Give this role automatically to new members (or bots)")
    @app_commands.choices(who=[app_commands.Choice(name="Humans", value="humans"), app_commands.Choice(name="Bots", value="bots")])
    async def ar_add(self, interaction: discord.Interaction, role: discord.Role, who: Optional[app_commands.Choice[str]] = None):
        g = interaction.guild
        if role >= g.me.top_role or role.managed or role.is_default():
            return await interaction.response.send_message(embed=brand.embed(
                self.bot, "err", "Role problem", "Pick a normal role that is below my highest role.", guild=g), ephemeral=True)
        key = who.value if who else "humans"
        cfg = await self.bot.db.get(g.id, "autoroles", {"humans": [], "bots": []})
        if role.id not in cfg[key]:
            cfg[key].append(role.id)
        await self.bot.db.set(g.id, "autoroles", cfg)
        await interaction.response.send_message(embed=brand.embed(
            self.bot, "ok", "Auto role added", f"New **{key}** will get {role.mention}.", guild=g), ephemeral=True)

    @autorole.command(name="remove", description="Stop giving a role automatically")
    async def ar_remove(self, interaction: discord.Interaction, role: discord.Role):
        g = interaction.guild
        cfg = await self.bot.db.get(g.id, "autoroles", {"humans": [], "bots": []})
        for k in cfg:
            cfg[k] = [r for r in cfg[k] if r != role.id]
        await self.bot.db.set(g.id, "autoroles", cfg)
        await interaction.response.send_message(embed=brand.embed(self.bot, "ok", "Auto role removed", role.mention, guild=g), ephemeral=True)

    @autorole.command(name="list", description="Show auto roles")
    async def ar_list(self, interaction: discord.Interaction):
        cfg = await self.bot.db.get(interaction.guild.id, "autoroles", {"humans": [], "bots": []})
        fmt = lambda ids: " ".join(f"<@&{i}>" for i in ids) or "none"
        await interaction.response.send_message(embed=brand.embed(
            self.bot, "info", "Auto roles", None, guild=interaction.guild,
            fields=[("{members} Humans", fmt(cfg.get("humans", [])), False), ("{robot} Bots", fmt(cfg.get("bots", [])), False)]), ephemeral=True)

    @commands.Cog.listener()
    async def on_member_join(self, member):
        cfg = await self.bot.db.get(member.guild.id, "autoroles", None)
        if not cfg:
            return
        for rid in cfg.get("bots" if member.bot else "humans", []):
            role = member.guild.get_role(rid)
            if role:
                try:
                    await member.add_roles(role, reason="Auto role")
                except discord.HTTPException as exc:
                    print(f"[autorole] cannot give {role.name} in {member.guild.name}: {exc} (my role must be above it)")

    # ---- server stats voice channels
    async def _stat_values(self, g):
        tick = (await self.bot.db.fetchone("SELECT COUNT(*) c FROM tickets WHERE guild_id=? AND status='open'", (g.id,)))["c"]
        bots = sum(1 for m in g.members if m.bot)
        return {"members": f"👥 Members: {g.member_count}", "bots": f"🤖 Bots: {bots}",
                "boosts": f"💎 Boosts: {g.premium_subscription_count}", "tickets": f"🎫 Open tickets: {tick}"}

    @serverstats.command(name="setup", description="Create live stat channels (members, bots, boosts, open tickets)")
    async def ss_setup(self, interaction: discord.Interaction):
        g = interaction.guild
        await interaction.response.defer(ephemeral=True)
        old = await self.bot.db.get(g.id, "serverstats", None)
        if old:
            return await interaction.followup.send(embed=brand.embed(
                self.bot, "err", "Already set up", "Run `/serverstats remove` first.", guild=g), ephemeral=True)
        ow = {g.default_role: discord.PermissionOverwrite(connect=False, view_channel=True)}
        try:
            cat = await g.create_category("📊 Server Stats", overwrites=ow, position=0)
            vals = await self._stat_values(g)
            ids = {}
            for key, name in vals.items():
                ch = await g.create_voice_channel(name, category=cat, overwrites=ow)
                ids[key] = ch.id
        except discord.HTTPException as exc:
            return await interaction.followup.send(embed=brand.embed(
                self.bot, "err", "Could not create channels", f"{exc.text}\nI need **Manage Channels**.", guild=g), ephemeral=True)
        await self.bot.db.set(g.id, "serverstats", {"category": cat.id, "channels": ids})
        await interaction.followup.send(embed=brand.embed(
            self.bot, "ok", "Server stats live", "Updates every 10 minutes (Discord limits channel renames).", guild=g), ephemeral=True)

    @serverstats.command(name="remove", description="Delete the stat channels")
    async def ss_remove(self, interaction: discord.Interaction):
        g = interaction.guild
        cfg = await self.bot.db.get(g.id, "serverstats", None)
        if not cfg:
            return await interaction.response.send_message(embed=brand.embed(self.bot, "err", "Not set up", "", guild=g), ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        for cid in list(cfg["channels"].values()) + [cfg["category"]]:
            ch = g.get_channel(cid)
            if ch:
                try:
                    await ch.delete(reason="Server stats removed")
                except discord.HTTPException:
                    pass
        await self.bot.db.set(g.id, "serverstats", None)
        await interaction.followup.send(embed=brand.embed(self.bot, "ok", "Removed", "Stat channels deleted.", guild=g), ephemeral=True)

    @tasks.loop(minutes=10)
    async def stats_loop(self):
        for g in self.bot.guilds:
            cfg = await self.bot.db.get(g.id, "serverstats", None)
            if not cfg:
                continue
            vals = await self._stat_values(g)
            for key, cid in cfg["channels"].items():
                ch = g.get_channel(cid)
                if ch and ch.name != vals.get(key):
                    try:
                        await ch.edit(name=vals[key], reason="Server stats")
                    except discord.HTTPException as exc:
                        print(f"[serverstats] {g.name}: {exc}")

    @stats_loop.before_loop
    async def _s_ready(self):
        await self.bot.wait_until_ready()

    # ================================================================== sticky + responders
    @sticky.command(name="set", description="Keep a message at the bottom of this channel")
    async def sticky_set(self, interaction: discord.Interaction, text: str):
        await self.bot.db.execute("INSERT OR REPLACE INTO stickies(channel_id,guild_id,text,last_msg_id) VALUES(?,?,?,NULL)",
                                  (interaction.channel.id, interaction.guild.id, self.bot.emo.fmt(text, interaction.guild)[:1800]))
        await interaction.response.send_message(embed=brand.embed(self.bot, "ok", "Sticky set", "It appears after the next message.", guild=interaction.guild), ephemeral=True)

    @sticky.command(name="remove", description="Remove the sticky message here")
    async def sticky_remove(self, interaction: discord.Interaction):
        n = await self.bot.db.rowcount("DELETE FROM stickies WHERE channel_id=?", (interaction.channel.id,))
        await interaction.response.send_message(embed=brand.embed(self.bot, "ok" if n else "err", "Removed" if n else "No sticky here", "", guild=interaction.guild), ephemeral=True)

    @responder.command(name="add", description="When a message contains the trigger, the bot replies")
    async def resp_add(self, interaction: discord.Interaction, trigger: str, reply: str):
        try:
            await self.bot.db.execute("INSERT INTO responders(guild_id,trigger,reply) VALUES(?,?,?)",
                                      (interaction.guild.id, trigger.lower()[:60], reply[:1800]))
        except Exception:
            return await interaction.response.send_message(embed=brand.embed(self.bot, "err", "Exists", "That trigger exists. Remove it first.", guild=interaction.guild), ephemeral=True)
        await interaction.response.send_message(embed=brand.embed(self.bot, "ok", "Responder added", f"`{trigger.lower()}` → {reply[:200]}", guild=interaction.guild), ephemeral=True)

    @responder.command(name="remove", description="Delete a trigger")
    async def resp_remove(self, interaction: discord.Interaction, trigger: str):
        n = await self.bot.db.rowcount("DELETE FROM responders WHERE guild_id=? AND trigger=?", (interaction.guild.id, trigger.lower()))
        await interaction.response.send_message(embed=brand.embed(self.bot, "ok" if n else "err", "Removed" if n else "Not found", trigger, guild=interaction.guild), ephemeral=True)

    @responder.command(name="list", description="List triggers")
    async def resp_list(self, interaction: discord.Interaction):
        rows = await self.bot.db.fetchall("SELECT trigger, reply FROM responders WHERE guild_id=?", (interaction.guild.id,))
        body = "\n".join(f"`{r['trigger']}` → {r['reply'][:70]}" for r in rows) or "None yet."
        await interaction.response.send_message(embed=brand.embed(self.bot, "info", "Auto-responders", body[:3900], guild=interaction.guild), ephemeral=True)

    # ================================================================== starboard
    @star.command(name="setup", description="Messages with enough ⭐ reactions get featured")
    async def star_setup(self, interaction: discord.Interaction, channel: discord.TextChannel, threshold: app_commands.Range[int, 1, 30] = 3):
        await self.bot.db.cfg_set(interaction.guild.id, "starboard", STAR_DEFAULTS, channel_id=channel.id, threshold=threshold)
        await interaction.response.send_message(embed=brand.embed(
            self.bot, "ok", "Starboard on", f"{channel.mention} • needs **{threshold}** ⭐", guild=interaction.guild), ephemeral=True)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload):
        if not payload.guild_id or str(payload.emoji) != "⭐":
            return
        g = self.bot.get_guild(payload.guild_id)
        cfg = await self.bot.db.cfg(payload.guild_id, "starboard", STAR_DEFAULTS)
        board = g.get_channel(cfg["channel_id"]) if g and cfg["channel_id"] else None
        if not board or payload.channel_id == board.id:
            return
        ch = g.get_channel(payload.channel_id)
        try:
            msg = await ch.fetch_message(payload.message_id)
        except discord.HTTPException:
            return
        if msg.author.bot:
            return
        react = discord.utils.get(msg.reactions, emoji="⭐")
        count = react.count if react else 0
        if count < cfg["threshold"]:
            return
        em = brand.embed(self.bot, "premium", f"⭐ {count}", msg.content[:1500] or "(attachment)", guild=g, ts=False,
                         fields=[("Source", f"[Jump to message]({msg.jump_url})", False)], thumbnail=msg.author.display_avatar.url)
        em.set_author(name=msg.author.display_name, icon_url=msg.author.display_avatar.url)
        if msg.attachments and msg.attachments[0].content_type and msg.attachments[0].content_type.startswith("image"):
            em.set_image(url=msg.attachments[0].url)
        row = await self.bot.db.fetchone("SELECT board_msg_id FROM starred WHERE message_id=?", (msg.id,))
        try:
            if row:
                await (await board.fetch_message(row["board_msg_id"])).edit(embed=em)
            else:
                sent = await board.send(embed=em)
                await self.bot.db.execute("INSERT INTO starred(message_id,guild_id,board_msg_id) VALUES(?,?,?)", (msg.id, g.id, sent.id))
        except discord.HTTPException as exc:
            print(f"[starboard] {exc}")

    # ================================================================== temp voice
    @tempvc.command(name="setup", description="Join this voice channel to get your own private voice channel")
    async def tv_setup(self, interaction: discord.Interaction, lobby: discord.VoiceChannel, category: Optional[discord.CategoryChannel] = None):
        await self.bot.db.cfg_set(interaction.guild.id, "tempvc", TEMPVC_DEFAULTS, lobby_id=lobby.id,
                                  category_id=(category or lobby.category).id if (category or lobby.category) else None)
        await interaction.response.send_message(embed=brand.embed(
            self.bot, "ok", "Temp voice ready", f"Join {lobby.mention} to create your own channel. It deletes itself when empty.", guild=interaction.guild), ephemeral=True)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        g = member.guild
        cfg = await self.bot.db.cfg(g.id, "tempvc", TEMPVC_DEFAULTS)
        if after.channel and cfg["lobby_id"] and after.channel.id == cfg["lobby_id"]:
            cat = g.get_channel(cfg["category_id"]) if cfg["category_id"] else None
            try:
                ch = await g.create_voice_channel(f"🔊 {member.display_name}'s room", category=cat, overwrites={
                    member: discord.PermissionOverwrite(manage_channels=True, move_members=True, connect=True)})
                self.temp_channels.add(ch.id)
                await member.move_to(ch)
            except discord.HTTPException as exc:
                print(f"[tempvc] {g.name}: {exc} (need Manage Channels + Move Members)")
        if before.channel and before.channel.id in self.temp_channels and not before.channel.members:
            self.temp_channels.discard(before.channel.id)
            try:
                await before.channel.delete(reason="Temp voice empty")
            except discord.HTTPException:
                pass


async def setup(bot):
    await bot.add_cog(Utility(bot))
