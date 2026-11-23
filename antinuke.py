"""Anti-nuke: detects mass destructive actions via the audit log, undoes them, and punishes the attacker.
Also includes join-flood (anti-raid) protection and a one-command server lockdown."""
import asyncio
import time
from collections import defaultdict, deque
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import brand

DEFAULTS = {
    "enabled": False,
    "punish": "ban",                # ban | kick | strip
    "window": 30,                   # seconds in which actions are counted
    "restore": True,
    "whitelist": [],
    "limits": {"channel_delete": 2, "channel_create": 4, "role_delete": 2, "role_create": 4, "ban": 3, "kick": 3,
               "webhook_create": 2, "prune": 1, "bot_add": 1, "role_admin": 1, "guild_update": 2},
    "raid": {"enabled": True, "joins": 8, "secs": 10, "min_age_days": 3},
}

LABELS = {
    "channel_delete": "channel deletions", "channel_create": "channel creations", "role_delete": "role deletions",
    "role_create": "role creations", "ban": "mass bans", "kick": "mass kicks", "webhook_create": "webhook creations",
    "prune": "member prune", "bot_add": "unauthorized bot added", "role_admin": "admin permission given to a role",
    "guild_update": "server settings changes",
}

DANGEROUS = discord.Permissions(administrator=True, manage_guild=True, manage_roles=True, manage_channels=True,
                                ban_members=True, kick_members=True, manage_webhooks=True)
NEEDED = ("view_audit_log", "ban_members", "kick_members", "manage_channels", "manage_roles", "manage_webhooks", "manage_guild")


def _action_map():
    A = discord.AuditLogAction
    return {A.channel_delete: "channel_delete", A.channel_create: "channel_create", A.role_delete: "role_delete",
            A.role_create: "role_create", A.ban: "ban", A.kick: "kick", A.webhook_create: "webhook_create",
            A.member_prune: "prune", A.bot_add: "bot_add"}


class AntiNuke(commands.Cog, name="AntiNuke"):
    antinuke = app_commands.Group(name="antinuke", description="Protect the server from nukes and raids",
                                  default_permissions=discord.Permissions(administrator=True), guild_only=True)

    def __init__(self, bot):
        self.bot = bot
        self.actions = _action_map()
        self.counts = defaultdict(deque)       # (guild, actor, key) -> deque[(monotonic, target_id)]
        self.snap_ch, self.snap_role = {}, {}
        self.joins = defaultdict(deque)
        self.raid_until = {}

    async def cfg(self, guild_id):
        cfg = await self.bot.db.cfg(guild_id, "antinuke", DEFAULTS)
        cfg["limits"] = {**DEFAULTS["limits"], **(cfg.get("limits") or {})}
        cfg["raid"] = {**DEFAULTS["raid"], **(cfg.get("raid") or {})}
        return cfg

    def is_owner(self, interaction):
        return interaction.user.id == interaction.guild.owner_id or interaction.user.id in self.bot.owner_ids

    async def _owner_only(self, interaction):
        if self.is_owner(interaction):
            return True
        await interaction.response.send_message(embed=brand.embed(
            self.bot, "err", "Owner only", "Only the server owner can change this, so a hacked admin account cannot weaken protection.",
            guild=interaction.guild), ephemeral=True)
        return False

    # ------------------------------------------------------------ snapshots
    def _snap_channel(self, ch):
        return dict(id=ch.id, type=ch.type, name=ch.name, position=ch.position, category=ch.category_id,
                    overwrites=dict(ch.overwrites), topic=getattr(ch, "topic", None), nsfw=getattr(ch, "nsfw", False),
                    slowmode=getattr(ch, "slowmode_delay", 0), user_limit=getattr(ch, "user_limit", None))

    def _snap_role(self, r):
        return dict(id=r.id, name=r.name, permissions=r.permissions, colour=r.colour, hoist=r.hoist,
                    mentionable=r.mentionable, position=r.position, managed=r.managed)

    def cache_guild(self, guild):
        for ch in guild.channels:
            self.snap_ch[ch.id] = self._snap_channel(ch)
        for r in guild.roles:
            self.snap_role[r.id] = self._snap_role(r)

    @commands.Cog.listener()
    async def on_ready(self):
        for g in self.bot.guilds:
            self.cache_guild(g)

    @commands.Cog.listener()
    async def on_guild_join(self, guild):
        self.cache_guild(guild)

    @commands.Cog.listener()
    async def on_guild_channel_create(self, ch):
        self.snap_ch[ch.id] = self._snap_channel(ch)

    @commands.Cog.listener()
    async def on_guild_channel_update(self, before, after):
        self.snap_ch[after.id] = self._snap_channel(after)

    @commands.Cog.listener()
    async def on_guild_role_create(self, role):
        self.snap_role[role.id] = self._snap_role(role)

    @commands.Cog.listener()
    async def on_guild_role_update(self, before, after):
        # do not overwrite the snapshot with an attacker's admin grant
        if after.permissions.administrator and not before.permissions.administrator:
            return
        self.snap_role[after.id] = self._snap_role(after)

    # ------------------------------------------------------------ detection
    @commands.Cog.listener()
    async def on_audit_log_entry_create(self, entry):
        guild = entry.guild
        cfg = await self.cfg(guild.id)
        if not cfg["enabled"]:
            return
        actor = entry.user_id
        if actor is None or actor == self.bot.user.id or actor == guild.owner_id \
                or actor in cfg["whitelist"] or actor in self.bot.owner_ids:
            return
        A = discord.AuditLogAction
        key = self.actions.get(entry.action)
        if entry.action == A.role_update:
            before, after = getattr(entry.before, "permissions", None), getattr(entry.after, "permissions", None)
            if before is not None and after is not None and after.administrator and not before.administrator:
                key = "role_admin"
        elif entry.action == A.guild_update and getattr(entry.before, "name", None) is not None:
            key = "guild_update"
        if not key:
            return
        target_id = getattr(entry.target, "id", None)
        now = time.monotonic()
        dq = self.counts[(guild.id, actor, key)]
        dq.append((now, target_id))
        while dq and now - dq[0][0] > cfg["window"]:
            dq.popleft()

        await self.remediate(entry, key, cfg, target_id)

        limit = cfg["limits"].get(key, 1)
        if len(dq) >= limit:
            victims = [t for _, t in dq if t]
            dq.clear()
            await self.cleanup(guild, key, victims)
            await self.punish(guild, actor, f"Anti-nuke: {LABELS[key]} ({len(victims) or limit}x in {cfg['window']}s)", cfg)

    async def remediate(self, entry, key, cfg, target_id):
        """Undo the single unauthorized action right away."""
        guild = entry.guild
        try:
            if key == "channel_delete" and cfg["restore"] and target_id in self.snap_ch:
                await self.restore_channel(guild, self.snap_ch[target_id])
            elif key == "role_delete" and cfg["restore"] and target_id in self.snap_role:
                await self.restore_role(guild, self.snap_role[target_id])
            elif key == "webhook_create" and target_id:
                wh = await self.bot.fetch_webhook(target_id)
                await wh.delete(reason="Anti-nuke: unauthorized webhook")
            elif key == "bot_add" and target_id:
                await guild.kick(discord.Object(target_id), reason="Anti-nuke: unauthorized bot")
            elif key == "role_admin" and target_id:
                role = guild.get_role(target_id)
                before = getattr(entry.before, "permissions", None)
                if role and before is not None:
                    await role.edit(permissions=before, reason="Anti-nuke: reverted admin grant")
            elif key == "guild_update":
                old = getattr(entry.before, "name", None)
                if old:
                    await guild.edit(name=old, reason="Anti-nuke: reverted server rename")
        except discord.HTTPException as exc:
            await self.alert(guild, "warn", "Anti-nuke could not undo an action",
                             f"Action: **{LABELS[key]}**\nProblem: {exc.status} {exc.text}\nMake sure my role is at the top and I have the needed permissions.")

    async def cleanup(self, guild, key, victims):
        """Extra cleanup once the limit is crossed (delete spam-created stuff, unban victims)."""
        for tid in victims:
            try:
                if key == "channel_create":
                    ch = guild.get_channel(tid)
                    if ch:
                        await ch.delete(reason="Anti-nuke: channel spam")
                elif key == "role_create":
                    role = guild.get_role(tid)
                    if role:
                        await role.delete(reason="Anti-nuke: role spam")
                elif key == "ban":
                    await guild.unban(discord.Object(tid), reason="Anti-nuke: mass ban reverted")
            except discord.HTTPException:
                continue

    async def punish(self, guild, user_id, reason, cfg):
        mode = cfg["punish"]
        result = ""
        try:
            if mode == "ban":
                await guild.ban(discord.Object(user_id), reason=reason, delete_message_seconds=0)
                result = "banned"
            elif mode == "kick":
                await guild.kick(discord.Object(user_id), reason=reason)
                result = "kicked"
            else:
                member = guild.get_member(user_id)
                roles = [r for r in (member.roles if member else []) if not r.is_default() and not r.managed
                         and r.permissions & DANGEROUS]
                if member and roles:
                    await member.remove_roles(*roles, reason=reason)
                result = f"dangerous roles removed ({len(roles)})"
        except discord.HTTPException as exc:
            result = f"**FAILED** ({exc.status} {exc.text}) — move my role ABOVE theirs!"
        await self.alert(guild, "err", "Anti-nuke triggered", f"{{shield}} <@{user_id}> (`{user_id}`)\n**{reason}**\nResult: {result}",
                         dm_owner=True)

    async def alert(self, guild, kind, title, text, dm_owner=False):
        em = brand.embed(self.bot, kind, title, text, guild=guild)
        sent = await brand.send_log(self.bot, guild, em)
        if dm_owner or not sent:
            try:
                owner = guild.owner or await guild.fetch_member(guild.owner_id)
                await owner.send(embed=em)
            except discord.HTTPException:
                print(f"[antinuke] {guild.name}: alert could not be delivered: {title}")

    # ------------------------------------------------------------ restoring
    async def restore_channel(self, guild, s):
        cat = guild.get_channel(s["category"]) if s["category"] else None
        ow = {}
        for target, perms in s["overwrites"].items():
            if isinstance(target, discord.Role) and guild.get_role(target.id):
                ow[target] = perms
            elif isinstance(target, discord.Member) and guild.get_member(target.id):
                ow[target] = perms
        reason = "Anti-nuke: restoring deleted channel"
        T = discord.ChannelType
        if s["type"] == T.text:
            ch = await guild.create_text_channel(s["name"], category=cat, overwrites=ow, topic=s["topic"], nsfw=s["nsfw"],
                                                 slowmode_delay=s["slowmode"] or 0, reason=reason)
        elif s["type"] == T.voice:
            ch = await guild.create_voice_channel(s["name"], category=cat, overwrites=ow,
                                                  user_limit=s["user_limit"] or 0, reason=reason)
        elif s["type"] == T.category:
            ch = await guild.create_category(s["name"], overwrites=ow, reason=reason)
        else:
            return
        try:
            await ch.edit(position=s["position"])
        except discord.HTTPException:
            pass
        await self.alert(guild, "ok", "Channel restored", f"{{check}} Recreated **{s['name']}** after an unauthorized delete.")

    async def restore_role(self, guild, s):
        if s["managed"]:
            return
        role = await guild.create_role(name=s["name"], permissions=s["permissions"], colour=s["colour"], hoist=s["hoist"],
                                       mentionable=s["mentionable"], reason="Anti-nuke: restoring deleted role")
        try:
            await role.edit(position=min(s["position"], guild.me.top_role.position - 1))
        except discord.HTTPException:
            pass
        await self.alert(guild, "ok", "Role restored", f"{{check}} Recreated **{s['name']}** (members must be re-added).")

    # ------------------------------------------------------------ anti-raid
    @commands.Cog.listener()
    async def on_member_join(self, member):
        guild = member.guild
        cfg = await self.cfg(guild.id)
        raid = cfg["raid"]
        if not cfg["enabled"] or not raid["enabled"]:
            return
        now = time.monotonic()
        dq = self.joins[guild.id]
        dq.append(now)
        while dq and now - dq[0] > raid["secs"]:
            dq.popleft()
        if len(dq) >= raid["joins"] and now > self.raid_until.get(guild.id, 0):
            await self.alert(guild, "warn", "Raid detected",
                             f"{{warn}} **{len(dq)}** joins in {raid['secs']}s. For the next minute accounts younger than "
                             f"**{raid['min_age_days']} days** will be kicked. Use `/antinuke lockdown` for a full lock.")
        if len(dq) >= raid["joins"]:
            self.raid_until[guild.id] = now + 60
        if now < self.raid_until.get(guild.id, 0) and not member.bot:
            age = (discord.utils.utcnow() - member.created_at).days
            if age < raid["min_age_days"]:
                try:
                    await member.kick(reason=f"Anti-raid: account only {age} day(s) old during a join flood")
                except discord.HTTPException as exc:
                    print(f"[antinuke] raid kick failed for {member.id}: {exc}")

    # ------------------------------------------------------------ commands
    @antinuke.command(name="enable", description="Turn anti-nuke protection on")
    async def enable(self, interaction: discord.Interaction):
        g = interaction.guild
        await self.bot.db.cfg_set(g.id, "antinuke", DEFAULTS, enabled=True)
        self.cache_guild(g)
        perms = g.me.guild_permissions
        missing = [n.replace("_", " ") for n in NEEDED if not getattr(perms, n)]
        top = max((r.position for r in g.roles), default=0)
        notes = []
        if missing:
            notes.append("❌ I am missing: **" + ", ".join(missing) + "**")
        if g.me.top_role.position < top:
            notes.append("⚠️ Drag my role to the **top** of the role list, otherwise I cannot punish higher roles.")
        notes.append("Tip: add your trusted admins with `/antinuke whitelist` (owner only). The server owner is always safe.")
        await interaction.response.send_message(embed=brand.embed(
            self.bot, "warn" if missing else "ok", "Anti-nuke ENABLED",
            "{shield} Mass deletes, bans, kicks, webhook and bot spam, admin-role grants and raids are now watched.\n\n"
            + "\n".join(notes), guild=g), ephemeral=True)

    @antinuke.command(name="disable", description="Turn anti-nuke protection off (owner only)")
    async def disable(self, interaction: discord.Interaction):
        if not await self._owner_only(interaction):
            return
        await self.bot.db.cfg_set(interaction.guild.id, "antinuke", DEFAULTS, enabled=False)
        await interaction.response.send_message(embed=brand.embed(
            self.bot, "warn", "Anti-nuke DISABLED", "{offline} The server is no longer protected.", guild=interaction.guild),
            ephemeral=True)

    @antinuke.command(name="status", description="Show protection status and limits")
    async def status(self, interaction: discord.Interaction):
        g = interaction.guild
        cfg = await self.cfg(g.id)
        limits = "\n".join(f"• {LABELS[k]}: **{v}**" for k, v in cfg["limits"].items())
        wl = " ".join(f"<@{u}>" for u in cfg["whitelist"]) or "nobody (only the owner)"
        r = cfg["raid"]
        await interaction.response.send_message(embed=brand.embed(
            self.bot, "premium", "Anti-nuke status", None, guild=g, fields=[
                ("Protection", "{online} ON" if cfg["enabled"] else "{offline} OFF", True),
                ("Punishment", f"`{cfg['punish']}`", True), ("Auto-restore", "yes" if cfg["restore"] else "no", True),
                (f"Limits per {cfg['window']}s", limits, False),
                ("Anti-raid", f"{'on' if r['enabled'] else 'off'} • {r['joins']} joins/{r['secs']}s • kick accounts < {r['min_age_days']}d", False),
                ("Whitelist", wl, False)]), ephemeral=True)

    @antinuke.command(name="whitelist", description="Trust a user so they are never punished (owner only)")
    @app_commands.choices(action=[app_commands.Choice(name=a, value=a) for a in ("add", "remove")])
    async def whitelist(self, interaction: discord.Interaction, action: app_commands.Choice[str], user: discord.User):
        if not await self._owner_only(interaction):
            return
        cfg = await self.cfg(interaction.guild.id)
        wl = set(cfg["whitelist"])
        (wl.add if action.value == "add" else wl.discard)(user.id)
        await self.bot.db.cfg_set(interaction.guild.id, "antinuke", DEFAULTS, whitelist=list(wl))
        await interaction.response.send_message(embed=brand.embed(
            self.bot, "ok", "Whitelist updated", f"{user.mention} {'is now trusted' if action.value == 'add' else 'was removed'}.",
            guild=interaction.guild), ephemeral=True)

    @antinuke.command(name="punishment", description="What happens to an attacker (owner only)")
    @app_commands.choices(mode=[app_commands.Choice(name="Ban", value="ban"), app_commands.Choice(name="Kick", value="kick"),
                                app_commands.Choice(name="Strip dangerous roles", value="strip")])
    async def punishment(self, interaction: discord.Interaction, mode: app_commands.Choice[str]):
        if not await self._owner_only(interaction):
            return
        await self.bot.db.cfg_set(interaction.guild.id, "antinuke", DEFAULTS, punish=mode.value)
        await interaction.response.send_message(embed=brand.embed(
            self.bot, "ok", "Punishment set", f"Attackers will be: **{mode.name}**", guild=interaction.guild), ephemeral=True)

    @antinuke.command(name="limit", description="Change how many actions trigger protection (owner only)")
    @app_commands.choices(action=[app_commands.Choice(name=v, value=k) for k, v in LABELS.items()])
    async def limit(self, interaction: discord.Interaction, action: app_commands.Choice[str],
                    count: app_commands.Range[int, 1, 20]):
        if not await self._owner_only(interaction):
            return
        cfg = await self.cfg(interaction.guild.id)
        limits = dict(cfg["limits"], **{action.value: count})
        await self.bot.db.cfg_set(interaction.guild.id, "antinuke", DEFAULTS, limits=limits)
        await interaction.response.send_message(embed=brand.embed(
            self.bot, "ok", "Limit updated", f"**{action.name}**: punish at **{count}**.", guild=interaction.guild), ephemeral=True)

    @antinuke.command(name="raid", description="Configure join-flood protection (owner only)")
    async def raid(self, interaction: discord.Interaction, enabled: Optional[bool] = None,
                   joins: Optional[app_commands.Range[int, 3, 50]] = None, seconds: Optional[app_commands.Range[int, 3, 60]] = None,
                   min_age_days: Optional[app_commands.Range[int, 0, 60]] = None):
        if not await self._owner_only(interaction):
            return
        cfg = await self.cfg(interaction.guild.id)
        r = dict(cfg["raid"])
        for k, v in (("enabled", enabled), ("joins", joins), ("secs", seconds), ("min_age_days", min_age_days)):
            if v is not None:
                r[k] = v
        await self.bot.db.cfg_set(interaction.guild.id, "antinuke", DEFAULTS, raid=r)
        await interaction.response.send_message(embed=brand.embed(
            self.bot, "ok", "Anti-raid updated",
            f"{'ON' if r['enabled'] else 'OFF'} • {r['joins']} joins / {r['secs']}s • kicks accounts younger than {r['min_age_days']} days.",
            guild=interaction.guild), ephemeral=True)

    @antinuke.command(name="lockdown", description="Lock every text channel for @everyone right now")
    async def lockdown(self, interaction: discord.Interaction):
        g = interaction.guild
        await interaction.response.defer(ephemeral=True)
        saved = {}
        failed = 0
        for ch in g.text_channels:
            ow = ch.overwrites_for(g.default_role)
            saved[str(ch.id)] = ow.send_messages
            ow.send_messages = False
            try:
                await ch.set_permissions(g.default_role, overwrite=ow, reason=f"Lockdown by {interaction.user}")
            except discord.HTTPException:
                failed += 1
            await asyncio.sleep(0.4)
        await self.bot.db.set(g.id, "lockdown", saved)
        await interaction.followup.send(embed=brand.embed(
            self.bot, "warn" if failed else "ok", "Server locked down",
            f"{{lock}} Locked **{len(saved) - failed}** channels." + (f"\n❌ Failed in {failed} channels (permissions)." if failed else "")
            + "\nUse `/antinuke unlock` to reopen.", guild=g), ephemeral=True)
        await self.alert(g, "warn", "LOCKDOWN", f"{{lock}} {interaction.user.mention} locked the whole server.")

    @antinuke.command(name="unlock", description="Undo the lockdown")
    async def unlock(self, interaction: discord.Interaction):
        g = interaction.guild
        saved = await self.bot.db.get(g.id, "lockdown")
        if not saved:
            return await interaction.response.send_message(embed=brand.embed(
                self.bot, "err", "No lockdown", "There is no saved lockdown to undo.", guild=g), ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        n = 0
        for cid, prev in saved.items():
            ch = g.get_channel(int(cid))
            if not ch:
                continue
            ow = ch.overwrites_for(g.default_role)
            ow.send_messages = prev
            try:
                await ch.set_permissions(g.default_role, overwrite=ow if not ow.is_empty() else None,
                                         reason=f"Unlock by {interaction.user}")
                n += 1
            except discord.HTTPException:
                pass
            await asyncio.sleep(0.4)
        await self.bot.db.set(g.id, "lockdown", {})
        await interaction.followup.send(embed=brand.embed(
            self.bot, "ok", "Server unlocked", f"Restored **{n}** channels.", guild=g), ephemeral=True)


async def setup(bot):
    await bot.add_cog(AntiNuke(bot))
