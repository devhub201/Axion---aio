"""Self-hosted AI (Ollama on YOUR VPS) + AI announcement / embed writer."""
import asyncio
import json
import os
import re
import time
from typing import Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

import brand
import icons

ESCALATE = "[[ESCALATE]]"
AI_FOOTER = f"{brand.NAME} AI"

KINDS = {
    "announcement": ("{megaphone}", "big news for the whole community; exciting and clear"),
    "maintenance": ("{maintenance}", "scheduled maintenance / downtime notice; calm, reassuring, clear about impact"),
    "update": ("{rocket}", "new feature or upgrade release; exciting, lists what changed"),
    "offer": ("{fire}", "limited-time sale or discount; urgent, hype, clear call to action"),
    "rules": ("{shield}", "server / service rules; firm, clean, easy to scan"),
    "welcome": ("{star}", "welcome message for new members; warm, shows where to go next"),
    "giveaway": ("{gift}", "giveaway or event announcement; fun and exciting"),
    "ticket_panel": ("{ticket}", "support ticket panel; tells people how to open a ticket and what categories exist"),
    "custom": ("{diamond}", "whatever the brief says; premium hosting brand voice"),
}
KIND_COLOR = {
    "announcement": 0x8B5CF6, "maintenance": 0xF59E0B, "update": 0x38BDF8, "offer": 0xFBBF24,
    "rules": 0xEF4444, "welcome": 0x22C55E, "giveaway": 0xEC4899, "ticket_panel": 0x3B82F6,
    "custom": 0x8B5CF6,
}


class AIError(Exception):
    """Raised with a human-readable reason so callers can show it instead of failing silently."""


class AI:
    def __init__(self, bot):
        self.bot = bot
        self.url = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
        self.model = os.getenv("OLLAMA_MODEL", "qwen2.5:3b")
        self.timeout = float(os.getenv("OLLAMA_TIMEOUT", "180"))
        self.default_lang = os.getenv("AI_LANG", "hinglish")
        self.kb_path = os.getenv("KB_PATH", "kb.md")
        self.meta_key = os.getenv("META_API_KEY", "").strip()
        self.meta_url = os.getenv("META_API_URL", "https://api.llama.com/compat/v1").rstrip("/")
        self.meta_model = os.getenv("META_MODEL", "Llama-3.3-70B-Instruct")
        self.provider = "meta" if self.meta_key else "ollama"          # a Meta key in .env switches the AI to Meta
        self.token = os.getenv("OLLAMA_TOKEN", "").strip()      # optional: for a reverse proxy in front of a remote Ollama
        self._sem = asyncio.Semaphore(int(os.getenv("AI_CONCURRENCY", "1")))
        self._session: Optional[aiohttp.ClientSession] = None
        self._kb_cache = ("", None)

    # ---------------------------------------------------------------- plumbing
    @property
    def session(self):
        if self._session is None or self._session.closed:
            headers = {"Authorization": f"Bearer {self.token}"} if self.token else None
            self._session = aiohttp.ClientSession(headers=headers)
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    def kb(self):
        text = ""
        try:
            mtime = os.path.getmtime(self.kb_path)
        except OSError:
            mtime = 0.0
        plans_path = os.path.join("data", "plans_kb.md")
        try:
            pm = os.path.getmtime(plans_path)
        except OSError:
            pm = 0.0
        stamp = (mtime, pm)
        if stamp != self._kb_cache[1]:
            parts = []
            for path in (plans_path, self.kb_path):
                try:
                    with open(path, "r", encoding="utf-8") as fh:
                        parts.append(fh.read())
                except OSError:
                    pass
            self._kb_cache = ("\n\n".join(parts)[:7000], stamp)
        return self._kb_cache[0]

    @property
    def label(self):
        return f"Meta • {self.meta_model}" if self.provider == "meta" else f"Ollama • {self.model}"

    async def chat(self, messages, *, json_mode=False, temperature=0.7, max_tokens=450):
        if self.provider == "meta":
            return await self._chat_meta(messages, json_mode, temperature, max_tokens)
        return await self._chat_ollama(messages, json_mode=json_mode, temperature=temperature, max_tokens=max_tokens)

    async def _chat_meta(self, messages, json_mode, temperature, max_tokens):
        body = {"model": self.meta_model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        headers = {"Authorization": f"Bearer {self.meta_key}", "Content-Type": "application/json"}
        async with self._sem:
            for attempt in (1, 2):
                try:
                    async with self.session.post(f"{self.meta_url}/chat/completions", json=body, headers=headers,
                                                 timeout=aiohttp.ClientTimeout(total=min(self.timeout, 90))) as resp:
                        raw = await resp.text()
                        status = resp.status
                except asyncio.TimeoutError:
                    raise AIError("Meta AI took too long to answer. Try again in a moment.")
                except aiohttp.ClientError as exc:
                    raise AIError(f"Could not reach Meta AI at {self.meta_url}: {exc}")
                if status == 400 and "response_format" in body:       # model/endpoint without JSON mode: our prompts still ask for JSON
                    body.pop("response_format")
                    continue
                break
        if status in (401, 403):
            raise AIError("Meta AI rejected the API key. Check META_API_KEY in .env (no spaces, not expired).")
        if status == 404:
            raise AIError(f"Meta AI says model/endpoint not found. Check META_MODEL (`{self.meta_model}`) and META_API_URL.")
        if status == 429:
            raise AIError("Meta AI rate limit reached. Wait a little or check your plan/credits.")
        if status != 200:
            raise AIError(f"Meta AI returned HTTP {status}: {raw[:200]}")
        try:
            data = json.loads(raw)
            msg = data["choices"][0]["message"]["content"]
            if isinstance(msg, list):                                  # some APIs return content parts
                msg = "".join(p.get("text", "") for p in msg if isinstance(p, dict))
            text = (msg or "").strip()
        except (ValueError, KeyError, IndexError, TypeError):
            raise AIError("Meta AI sent a reply I could not read. Check META_API_URL points to the OpenAI-compatible endpoint.")
        if not text:
            raise AIError("Meta AI returned an empty answer.")
        return text

    async def _chat_ollama(self, messages, *, json_mode=False, temperature=0.7, max_tokens=450):
        payload = {
            "model": self.model, "messages": messages, "stream": False, "keep_alive": "30m",
            "options": {"temperature": temperature, "num_predict": max_tokens, "num_ctx": 4096},
        }
        if json_mode:
            payload["format"] = "json"
        async with self._sem:
            try:
                async with self.session.post(f"{self.url}/api/chat", json=payload,
                                             timeout=aiohttp.ClientTimeout(total=self.timeout)) as resp:
                    body = await resp.text()
                    if resp.status != 200:
                        raise AIError(f"Ollama returned HTTP {resp.status}: {body[:220]}")
                    data = json.loads(body)
            except aiohttp.ClientConnectorError:
                raise AIError(f"Cannot reach Ollama at {self.url}. Is `ollama serve` running on the VPS?")
            except asyncio.TimeoutError:
                raise AIError(f"The AI took longer than {int(self.timeout)}s. The VPS may be overloaded or the model too big.")
            except aiohttp.ClientError as exc:
                raise AIError(f"Network error talking to Ollama: {exc}")
            except ValueError:
                raise AIError("Ollama sent a reply that was not valid JSON.")
        text = ((data.get("message") or {}).get("content") or "").strip()
        if not text:
            raise AIError("The model returned an empty answer.")
        return text

    async def health(self):
        out = {"reachable": False, "model_ready": False, "models": [], "error": None, "latency": None}
        if self.provider == "meta":
            t0 = time.monotonic()
            try:
                await self.chat([{"role": "user", "content": "Reply with the single word: ready"}], max_tokens=5)
            except AIError as exc:
                out["error"] = str(exc)
                out["reachable"] = "rejected" in str(exc) or "not found" in str(exc) or "rate limit" in str(exc)
                return out
            out.update(reachable=True, model_ready=True, models=[self.meta_model], latency=time.monotonic() - t0)
            return out
        try:
            async with self.session.get(f"{self.url}/api/tags", timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status != 200:
                    out["error"] = f"HTTP {resp.status}"
                    return out
                data = await resp.json()
        except Exception as exc:
            out["error"] = str(exc) or exc.__class__.__name__
            return out
        out["reachable"] = True
        out["models"] = [m.get("name", "?") for m in data.get("models", [])]
        base = self.model.split(":")[0]
        out["model_ready"] = any(n == self.model or (":" not in self.model and n.split(":")[0] == base)
                                 for n in out["models"])
        if out["model_ready"]:
            t0 = time.monotonic()
            try:
                await self.chat([{"role": "user", "content": "Reply with the single word: ready"}], max_tokens=5)
                out["latency"] = time.monotonic() - t0
            except AIError as exc:
                out["error"] = str(exc)
        else:
            out["error"] = f"Model `{self.model}` is not pulled. Run: ollama pull {self.model}"
        return out

    async def warmup(self):
        h = await self.health()
        if h["error"]:
            print(f"[ai] WARNING: {h['error']}")
        else:
            print(f"[ai] model {self.model} ready ({h['latency']:.1f}s first reply)")

    # ------------------------------------------------------------ ticket brain
    def _persona(self, guild, category, subject, opener):
        names = ", ".join("{" + n + "}" for n in icons.ICONS)
        return (
            f"You are Axion AI, the support assistant of {brand.NAME}, a premium hosting company, working inside a "
            f"Discord support ticket. Ticket category: {category}. Subject: {subject}. Customer: {opener}.\n"
            "RULES:\n"
            "- Reply in the SAME language/style the customer writes (English, Hindi, or Hinglish = Hindi in English letters).\n"
            "- Be friendly, confident and brief: max 110 words. Use short lines, **bold** key words and a few emoji placeholders.\n"
            f"- Emoji placeholders you may use (write exactly with braces): {names}\n"
            "- Use ONLY facts from the KNOWLEDGE BASE below. Never invent prices, plans, discounts, specs, links or policies. "
            "If something is not in the knowledge base, say a staff member will confirm it.\n"
            "- Ask ONE clear follow-up question when you need details (server name, error text, panel link, etc).\n"
            f"- If the customer asks for a refund/chargeback, reports abuse or a hack, is angry, wants a human, or you cannot "
            f"solve it, tell them staff will help and end your message with {ESCALATE}\n"
            "- Never ask for passwords, tokens, card numbers or OTPs. Never claim you did something you cannot do.\n\n"
            "KNOWLEDGE BASE:\n" + (self.kb() or "(empty)")
        )

    async def ticket_reply(self, guild, history, category, subject, opener):
        msgs = [{"role": "system", "content": self._persona(guild, category, subject, opener)}] + history[-10:]
        raw = await self.chat(msgs, temperature=0.5, max_tokens=320)
        escalate = ESCALATE in raw
        text = raw.replace(ESCALATE, "").strip()
        return self.bot.emo.fmt(text, guild)[:3500], escalate

    async def answer(self, guild, question):
        msgs = [{"role": "system", "content": self._persona(guild, "general question", "-", "staff")},
                {"role": "user", "content": question}]
        raw = await self.chat(msgs, temperature=0.5, max_tokens=350)
        return self.bot.emo.fmt(raw.replace(ESCALATE, "").strip(), guild)[:3500]

    # ----------------------------------------------------------- embed writer
    async def make_embed(self, guild, kind, brief, lang=None):
        """Returns (data, note). note is None on success, or a warning string when a template was used."""
        lang = lang or self.default_lang
        lead, vibe = KINDS.get(kind, KINDS["custom"])
        names = ", ".join("{" + n + "}" for n in icons.ICONS)
        system = (
            f"You write eye-catching Discord embeds for {brand.NAME}, a premium game & VPS hosting brand.\n"
            "Return ONLY a JSON object with this shape:\n"
            '{"title": "...", "description": "...", "fields": [{"name": "...", "value": "...", "inline": true}], "footer": "..."}\n'
            "STYLE RULES:\n"
            f"- Type of post: {kind} ({vibe}). Start the title with the placeholder {lead}.\n"
            "- title: max 70 characters, bold and hype, includes exactly one emoji placeholder at the start.\n"
            "- description: 400-900 characters. Short punchy lines. Almost every line starts with an emoji placeholder. "
            f"Use **bold** for key words, bullet-style lines, one divider line '{brand.LINE}' between sections, and finish with a call to action.\n"
            "- fields: 0 to 3 small useful fields (use only when the brief has real details).\n"
            "- footer: max 40 characters.\n"
            f"- Emoji placeholders (write exactly with braces, use only these): {names}\n"
            f"- Language: {'Hinglish (Hindi written in English letters, natural and cool)' if lang == 'hinglish' else 'English'}.\n"
            "- NEVER invent prices, dates, links, specs or promises that are not in the brief. If details are missing, stay general.\n"
            "EXAMPLE OUTPUT:\n"
            '{"title": "{rocket} Mega Upgrade is LIVE!", "description": "{fire} **Faster nodes. Lower ping. Zero lag.**\\n'
            f'{brand.LINE}\\n{{bolt}} **NVMe storage** on every plan\\n{{shield}} **DDoS protection** always on\\n{{globe}} **New nodes** added\\n'
            f'{brand.LINE}\\n{{ticket}} Questions? Open a ticket and our team will help!", "fields": [], "footer": "Powered by {brand.NAME}"}}'
        )
        try:
            raw = await self.chat([{"role": "system", "content": system},
                                   {"role": "user", "content": f"Brief from the admin: {brief}"}],
                                  json_mode=True, temperature=0.8, max_tokens=700)
            data = self._parse_embed(raw)
            if data:
                return data, None
            return fallback_embed(kind, brief), "The AI answered in a format I could not read, so a template was used."
        except AIError as exc:
            return fallback_embed(kind, brief), f"AI unavailable: {exc} — a template was used instead."

    async def make_role_panel(self, guild, role_names, brief="", lang=None):
        """AI writes the panel title, description and, per role, an emoji + one-line blurb. Returns (data, note)."""
        lang = lang or self.default_lang
        icon_names = ", ".join(icons.ICONS)
        system = (
            f"You design self-assign role panels for the Discord server of {brand.NAME}, a premium hosting brand.\n"
            "Return ONLY JSON: {\"title\": \"...\", \"description\": \"...\", \"roles\": [{\"name\": \"exact role name\", "
            "\"emoji\": \"icon name\", \"blurb\": \"max 60 chars\"}]}\n"
            f"- title: max 60 chars, starts with one emoji placeholder like {{star}}.\n"
            "- description: 150-350 chars, 2-4 short punchy lines, each starting with an emoji placeholder like {bell}, ending with a call to action to click the buttons.\n"
            f"- For each role pick the BEST matching icon name from this list only: {icon_names}\n"
            "- Use each given role name EXACTLY once, in the same order. Blurb says what the role is for, based only on its name.\n"
            f"- Language: {'Hinglish (Hindi in English letters, natural and cool)' if lang == 'hinglish' else 'English'}.\n"
            "- Never invent perks or promises that the role name does not suggest."
        )
        user = "Roles: " + ", ".join(role_names) + (f"\nExtra brief from admin: {brief}" if brief else "")
        try:
            raw = await self.chat([{"role": "system", "content": system}, {"role": "user", "content": user}],
                                  json_mode=True, temperature=0.8, max_tokens=700)
            data = self._parse_panel(raw, role_names)
            if data:
                return data, None
            return fallback_panel(role_names), "The AI answered in a format I could not read, so a template was used."
        except AIError as exc:
            return fallback_panel(role_names), f"AI unavailable: {exc} — a template was used instead."

    @staticmethod
    def _parse_panel(raw, role_names):
        try:
            obj = json.loads(raw)
        except ValueError:
            m = re.search(r"\{.*\}", raw, re.S)
            try:
                obj = json.loads(m.group(0)) if m else None
            except ValueError:
                obj = None
        if not isinstance(obj, dict) or not obj.get("title") or len(str(obj.get("description") or "")) < 30:
            return None
        by_name = {str(r.get("name", "")).lower(): r for r in (obj.get("roles") or []) if isinstance(r, dict)}
        roles = []
        for i, name in enumerate(role_names):
            r = by_name.get(name.lower(), {})
            emoji = str(r.get("emoji") or "").strip("{}: ").lower()
            roles.append({"name": name, "emoji": emoji if emoji in icons.ICONS else PANEL_ICONS[i % len(PANEL_ICONS)],
                          "blurb": str(r.get("blurb") or f"Get the {name} role")[:70]})
        return {"title": str(obj["title"])[:120], "description": str(obj["description"])[:900], "roles": roles}

    @staticmethod
    def _parse_embed(raw):
        obj = None
        try:
            obj = json.loads(raw)
        except ValueError:
            m = re.search(r"\{.*\}", raw, re.S)
            if m:
                try:
                    obj = json.loads(m.group(0))
                except ValueError:
                    obj = None
        if not isinstance(obj, dict):
            return None
        title = str(obj.get("title") or "").strip()
        desc = obj.get("description")
        if isinstance(desc, list):
            desc = "\n".join(str(x) for x in desc)
        desc = str(desc or "").strip()
        if not title or len(desc) < 40:
            return None
        fields = []
        for f in (obj.get("fields") or [])[:3]:
            if isinstance(f, dict) and f.get("name") and f.get("value"):
                fields.append({"name": str(f["name"])[:256], "value": str(f["value"])[:1024],
                               "inline": bool(f.get("inline", True))})
        return {"title": title[:256], "description": desc[:3800], "fields": fields,
                "footer": str(obj.get("footer") or "")[:60]}


PANEL_ICONS = ["bell", "star", "gift", "megaphone", "rocket", "members", "diamond", "fire", "shield", "bolt"]


def fallback_panel(role_names):
    return {"title": "{star} Pick Your Roles",
            "description": "{bell} Choose the roles you want below!\n{rocket} Click a button to **get** a role, click again to **remove** it.",
            "roles": [{"name": n, "emoji": PANEL_ICONS[i % len(PANEL_ICONS)], "blurb": f"Get the {n} role"} for i, n in enumerate(role_names)]}


def fallback_embed(kind, brief):
    lead, _ = KINDS.get(kind, KINDS["custom"])
    titles = {
        "announcement": "Big Announcement!", "maintenance": "Scheduled Maintenance", "update": "New Update is LIVE!",
        "offer": "Limited Time Offer!", "rules": "Server Rules", "welcome": "Welcome to the Family!",
        "giveaway": "Giveaway Time!", "ticket_panel": "Need Help? Open a Ticket", "custom": "Important Notice",
    }
    brief = (brief or "").strip()
    desc = (f"{lead} **{brief}**\n{brand.LINE}\n"
            "{bolt} Fast • {shield} Secure • {diamond} Premium\n"
            f"{brand.LINE}\n{{ticket}} Questions? Open a ticket and our team will help you!")
    return {"title": f"{lead} {titles.get(kind, titles['custom'])}", "description": desc[:3800],
            "fields": [], "footer": f"Powered by {brand.NAME}"}


def build_embed(bot, guild, data, kind, image=None):
    emo = bot.emo
    em = discord.Embed(title=emo.fmt(data["title"], guild)[:256],
                       description=emo.fmt(data["description"], guild)[:4000],
                       color=KIND_COLOR.get(kind, 0x8B5CF6))
    for f in data.get("fields", []):
        em.add_field(name=emo.fmt(f["name"], guild)[:256], value=emo.fmt(f["value"], guild)[:1024],
                     inline=f.get("inline", True))
    foot = data.get("footer") or "Premium Hosting"
    em.set_footer(text=f"{brand.NAME} • {foot}"[:2000])
    em.timestamp = discord.utils.utcnow()
    if guild and guild.icon:
        em.set_thumbnail(url=guild.icon.url)
    if image:
        em.set_image(url=image)
    return em


# ---------------------------------------------------------------------------
# preview UI shared by /ai announce and /ticket panel
# ---------------------------------------------------------------------------
class EditModal(discord.ui.Modal, title="Edit embed"):
    new_title = discord.ui.TextInput(label="Title", max_length=256)
    new_desc = discord.ui.TextInput(label="Description", style=discord.TextStyle.paragraph, max_length=3800)

    def __init__(self, view):
        super().__init__()
        self.pv = view
        self.new_title.default = view.data["title"]
        self.new_desc.default = view.data["description"]

    async def on_submit(self, interaction: discord.Interaction):
        self.pv.data["title"] = str(self.new_title)
        self.pv.data["description"] = str(self.new_desc)
        await interaction.response.edit_message(embed=self.pv.embed(), view=self.pv)


class PreviewView(discord.ui.View):
    def __init__(self, bot, author_id, guild, kind, brief, lang, data, note, post_cb, image=None):
        super().__init__(timeout=900)
        self.bot, self.author_id, self.guild = bot, author_id, guild
        self.kind, self.brief, self.lang = kind, brief, lang
        self.data, self.note, self.post_cb, self.image = data, note, post_cb, image
        self.message = None

    def embed(self):
        return build_embed(self.bot, self.guild, self.data, self.kind, self.image)

    def content(self):
        return f"⚠️ {self.note}" if self.note else "Preview — nothing is posted until you press **Post**."

    async def interaction_check(self, interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This preview belongs to someone else.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        if self.message:
            try:
                await self.message.edit(content="Preview expired.", view=None)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="Post", style=discord.ButtonStyle.success, emoji="✅")
    async def post(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        try:
            where = await self.post_cb(self.embed())
        except discord.HTTPException as exc:
            return await interaction.followup.send(f"Could not post: {exc.status} {exc.text}", ephemeral=True)
        self.stop()
        await interaction.edit_original_response(content=f"✅ Posted {where}", embed=None, view=None)

    @discord.ui.button(label="Regenerate", style=discord.ButtonStyle.primary, emoji="🔄")
    async def regen(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await interaction.edit_original_response(content="🤖 Writing a new version... (can take a little on CPU)", view=None)
        self.data, self.note = await self.bot.ai.make_embed(self.guild, self.kind, self.brief, self.lang)
        await interaction.edit_original_response(content=self.content(), embed=self.embed(), view=self)

    @discord.ui.button(label="Edit", style=discord.ButtonStyle.secondary, emoji="✏️")
    async def edit(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(EditModal(self))

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger, emoji="✖️")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(content="Cancelled.", embed=None, view=None)


async def start_preview(bot, interaction, kind, brief, lang, post_cb, image=None, data=None):
    """Generate + show the preview. interaction must already be deferred (ephemeral).
    Pass `data` to preview a ready-made embed instead of asking the AI."""
    guild = interaction.guild
    note = None
    if data is None:
        data, note = await bot.ai.make_embed(guild, kind, brief, lang)
    view = PreviewView(bot, interaction.user.id, guild, kind, brief, lang, data, note, post_cb, image)
    view.message = await interaction.followup.send(content=view.content(), embed=view.embed(), view=view,
                                                   ephemeral=True, wait=True)


# ---------------------------------------------------------------------------
class AICog(commands.Cog, name="AI"):
    ai = app_commands.Group(name="ai", description="Self-hosted AI tools",
                            default_permissions=discord.Permissions(manage_guild=True), guild_only=True)

    def __init__(self, bot):
        self.bot = bot
        self._ask_cd = {}

    async def cog_load(self):
        asyncio.create_task(self.bot.ai.warmup())

    async def cog_unload(self):
        await self.bot.ai.close()

    @ai.command(name="announce", description="AI writes a premium announcement embed — preview, edit, then post")
    @app_commands.describe(brief="What to announce, in your own words", kind="Type of post", channel="Where to post (default: here)",
                           ping="Role to ping", lang="Language of the text", image="Optional big image URL")
    @app_commands.choices(
        kind=[app_commands.Choice(name=k, value=k) for k in KINDS if k != "ticket_panel"],
        lang=[app_commands.Choice(name="Hinglish", value="hinglish"), app_commands.Choice(name="English", value="english")])
    async def announce(self, interaction: discord.Interaction, brief: str,
                       kind: app_commands.Choice[str] = None, channel: Optional[discord.TextChannel] = None,
                       ping: Optional[discord.Role] = None, lang: app_commands.Choice[str] = None,
                       image: Optional[str] = None):
        target = channel or interaction.channel
        perms = target.permissions_for(interaction.guild.me)
        if not (perms.send_messages and perms.embed_links):
            return await interaction.response.send_message(embed=brand.embed(
                self.bot, "err", "Missing permission", f"I need **Send Messages** and **Embed Links** in {target.mention}.",
                guild=interaction.guild), ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        k = kind.value if kind else "announcement"

        async def post(embed):
            msg = await target.send(content=ping.mention if ping else None, embed=embed,
                                    allowed_mentions=discord.AllowedMentions(roles=True))
            return msg.jump_url
        await start_preview(self.bot, interaction, k, brief, lang.value if lang else None, post, image)

    @ai.command(name="ask", description="Ask the AI a question (uses your knowledge base)")
    async def ask(self, interaction: discord.Interaction, question: str):
        now = time.monotonic()
        if now - self._ask_cd.get(interaction.user.id, 0) < 15:
            return await interaction.response.send_message("Slow down a little — try again in a few seconds.", ephemeral=True)
        self._ask_cd[interaction.user.id] = now
        await interaction.response.defer(ephemeral=True)
        try:
            text = await self.bot.ai.answer(interaction.guild, question[:1500])
        except AIError as exc:
            return await interaction.followup.send(embed=brand.embed(
                self.bot, "err", "AI error", str(exc), guild=interaction.guild), ephemeral=True)
        await interaction.followup.send(embed=brand.embed(self.bot, "ai", "Axion AI", text, guild=interaction.guild,
                                                          footer=AI_FOOTER), ephemeral=True)

    @ai.command(name="status", description="Check that the AI on your VPS is alive")
    async def status(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        h = await self.bot.ai.health()
        g = interaction.guild
        ok = h["reachable"] and h["model_ready"] and not h["error"]
        meta = self.bot.ai.provider == "meta"
        fields = [("Provider", "Meta AI (API)" if meta else "Ollama (your VPS)", False),
                  ("Server", f"`{self.bot.ai.meta_url if meta else self.bot.ai.url}`", False),
                  ("Model", f"`{self.bot.ai.meta_model if meta else self.bot.ai.model}`", True),
                  ("Reachable", "{online} yes" if h["reachable"] else "{offline} no", True),
                  ("Model ready", "{check} yes" if h["model_ready"] else "{cross} no", True)]
        if h["latency"] is not None:
            fields.append(("Reply time", f"{h['latency']:.1f}s", True))
        if h["models"]:
            fields.append(("Installed models", ", ".join(f"`{m}`" for m in h["models"][:10]), False))
        desc = ("Meta AI is connected." if meta else "Everything is running on your own VPS.") if ok else f"**Problem:** {h['error']}"
        await interaction.followup.send(embed=brand.embed(self.bot, "ok" if ok else "err", "AI status", desc,
                                                          guild=g, fields=fields), ephemeral=True)


async def setup(bot):
    bot.ai = AI(bot)
    await bot.add_cog(AICog(bot))
