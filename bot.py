import asyncio
import json
import os
from typing import Dict, List, Optional
import re

# Load environment variables from a local .env file if present
try:
    from dotenv import load_dotenv  # type: ignore

    # Ensure local .env can override any inherited machine env for convenience
    load_dotenv(override=True)
except Exception:
    # If python-dotenv isn't installed, continue; BOT_TOKEN may still come from OS env or token file
    pass

import discord
from discord import app_commands
from discord.ext import commands

# ====== CONFIG / INTENTS ======
INTENTS = discord.Intents.default()
INTENTS.guilds = True
INTENTS.members = True  # /queue_kick @user
INTENTS.message_content = True
BOT_TOKEN = os.getenv("BOT_TOKEN")

STORAGE_CHANNEL_NAME = "queue-storage"  # channel used to store JSON data
INDEX_MESSAGE_PREFIX = "[QUEUE_INDEX]"  # map key->message_id (data)
DATA_MESSAGE_PREFIX = "[QUEUE_DATA]"  # message containing JSON array for a key
SETTINGS_MESSAGE_PREFIX = "[QUEUE_SETTINGS]"  # message storing settings (lang)

# ====== I18N ======
STRINGS: Dict[str, Dict[str, str]] = {}
for fn in os.listdir("lang"):
    if fn.endswith(".json"):
        code = fn[:-5]  # "en", "vi"
        # Use utf-8-sig to handle files saved with BOM on Windows
        with open(os.path.join("lang", fn), encoding="utf-8-sig") as f:
            STRINGS[code] = json.load(f)

DEFAULT_LANG = "en"


def t(lang: str, msg_key: str, **kwargs) -> str:
    lang = lang if lang in STRINGS else DEFAULT_LANG
    s = STRINGS[lang].get(msg_key, STRINGS[DEFAULT_LANG].get(msg_key, msg_key))
    try:
        return s.format(**kwargs)
    except Exception:
        return s


# ====== STORAGE LAYER ======
class QueueStore:
    """
    Persist queues + settings inside a hidden Discord channel (#queue-storage).
    - One index message: { key: message_id }
    - One data message per key: "[QUEUE_DATA] key\n[JSON array user_ids]"
    - One settings message: "[QUEUE_SETTINGS]\n{"lang":"en"}"
    """

    def __init__(self):
        self._index: Dict[str, int] = {}
        self._queues: Dict[str, List[int]] = {}
        self._settings: Dict[str, str] = {"lang": DEFAULT_LANG}
        self.storage_channel: Optional[discord.TextChannel] = None
        self.index_message: Optional[discord.Message] = None
        self.settings_message: Optional[discord.Message] = None

    async def init_storage(self, guild: discord.Guild):
        # find/create channel
        ch = discord.utils.get(guild.text_channels, name=STORAGE_CHANNEL_NAME)
        if ch is None:
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                guild.me: discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True
                ),
            }
            ch = await guild.create_text_channel(
                STORAGE_CHANNEL_NAME,
                overwrites=overwrites,
                topic="Queue DB & settings - do not touch",
            )
        self.storage_channel = ch

        # find/create index & settings messages
        idx, stg = None, None
        async for m in ch.history(limit=100, oldest_first=True):
            if m.author == guild.me:
                if m.content.startswith(INDEX_MESSAGE_PREFIX):
                    idx = m
                elif m.content.startswith(SETTINGS_MESSAGE_PREFIX):
                    stg = m
        if idx is None:
            idx = await ch.send(f"{INDEX_MESSAGE_PREFIX}\n{{}}")
        if stg is None:
            stg = await ch.send(
                f'{SETTINGS_MESSAGE_PREFIX}\n{{"lang":"{DEFAULT_LANG}"}}'
            )

        self.index_message = idx
        self.settings_message = stg

        # load index
        try:
            raw = idx.content.split("\n", 1)[1]
            self._index = {k: int(v) for k, v in json.loads(raw).items()}
        except Exception:
            self._index = {}

        # load settings
        try:
            raw = stg.content.split("\n", 1)[1]
            self._settings = json.loads(raw)
            if "lang" not in self._settings:
                self._settings["lang"] = DEFAULT_LANG
        except Exception:
            self._settings = {"lang": DEFAULT_LANG}

        # load all queues
        self._queues = {}
        for key, msg_id in self._index.items():
            try:
                data_msg = await ch.fetch_message(msg_id)
                raw = data_msg.content.split("\n", 1)[1]
                arr = json.loads(raw)
                self._queues[key] = [int(x) for x in arr]
            except Exception:
                self._queues[key] = []

    # ----- settings (language) -----
    def get_lang(self) -> str:
        return self._settings.get("lang", DEFAULT_LANG)

    async def set_lang(self, lang: str):
        self._settings["lang"] = lang if lang in STRINGS else DEFAULT_LANG
        payload = json.dumps(self._settings, separators=(",", ":"))
        if self.settings_message:
            await self.settings_message.edit(content=f"{SETTINGS_MESSAGE_PREFIX}\n{payload}")

    # No per-key title stored; titles live on each panel message's embed

    # ----- queues -----
    async def ensure_key(self, key: str):
        if key in self._index:
            return
        if not self.storage_channel:
            raise RuntimeError("Storage channel not initialized")
        data_msg = await self.storage_channel.send(f"{DATA_MESSAGE_PREFIX} {key}\n[]")
        self._index[key] = data_msg.id
        self._queues[key] = []
        await self._save_index()

    async def _save_index(self):
        if not self.index_message:
            return
        payload = json.dumps(self._index, separators=(",", ":"))
        await self.index_message.edit(content=f"{INDEX_MESSAGE_PREFIX}\n{payload}")

    async def _save_queue(self, key: str):
        if not self.storage_channel:
            return
        msg_id = self._index[key]
        data_msg = await self.storage_channel.fetch_message(msg_id)
        payload = json.dumps(self._queues[key], separators=(",", ":"))
        await data_msg.edit(content=f"{DATA_MESSAGE_PREFIX} {key}\n{payload}")

    def get_list(self, key: str) -> List[int]:
        return list(self._queues.get(key, []))

    def count(self, key: str) -> int:
        return len(self._queues.get(key, []))

    def position_of(self, key: str, user_id: int) -> Optional[int]:
        q = self._queues.get(key, [])
        try:
            return q.index(user_id) + 1
        except ValueError:
            return None

    async def add(self, key: str, user_id: int) -> int:
        await self.ensure_key(key)
        q = self._queues[key]
        if user_id in q:
            return q.index(user_id) + 1
        q.append(user_id)
        await self._save_queue(key)
        return len(q)

    async def remove(self, key: str, user_id: int) -> bool:
        await self.ensure_key(key)
        q = self._queues[key]
        try:
            q.remove(user_id)
            await self._save_queue(key)
            return True
        except ValueError:
            return False

    async def reset(self, key: str):
        await self.ensure_key(key)
        self._queues[key] = []
        await self._save_queue(key)

    async def pop_n(self, key: str, n: int) -> List[int]:
        """Remove and return up to n users from the front of the queue."""
        await self.ensure_key(key)
        q = self._queues[key]
        n = max(0, min(n, len(q)))
        removed = q[:n]
        del q[:n]
        await self._save_queue(key)
        return removed


# ====== PANEL HELPERS ======
async def update_panel_message(msg: discord.Message, key: str, store: QueueStore):
    """Update a panel message's embed text/footers for the given key."""
    lang = store.get_lang()
    embeds = list(msg.embeds)
    if not embeds:
        emb = discord.Embed(
            title=t(lang, "panel_title", key=key),
            description=t(lang, "panel_desc", key=key),
        )
        embeds = [emb]
    emb = embeds[0]
    if not emb.title:
        emb.title = t(lang, "panel_title", key=key)
    emb.description = t(lang, "panel_desc", key=key)
    emb.set_footer(text=t(lang, "footer_count", count=store.count(key)))
    try:
        await msg.edit(embeds=embeds)
    except Exception as e:
        print("[UPDATE_PANEL_ERROR]", e)


async def update_all_panels_for_key(guild: discord.Guild, key: str, store: QueueStore):
    """Scan channels and update panels that reference the given key."""
    look_desc = f"`{key}`"
    look_title = f": {key}"
    for ch in guild.text_channels:
        try:
            async for m in ch.history(limit=200, oldest_first=False):
                if m.author != guild.me or not m.embeds:
                    continue
                emb = m.embeds[0]
                desc = (emb.description or "")
                ttl = (emb.title or "")
                if (look_desc in desc) or (look_title in ttl):
                    await update_panel_message(m, key, store)
        except Exception:
            # missing permissions or access; skip channel
            continue


# ====== UI VIEW ======
class SignupView(discord.ui.View):
    def __init__(self, key: Optional[str], store: QueueStore, *, timeout: Optional[float] = None):
        # timeout=None enables persistent views
        super().__init__(timeout=timeout)
        self.key = key
        self.store = store

        lang = self.store.get_lang()
        # set dynamic labels
        self.signup.label = t(lang, "btn_signup")
        self.cancel.label = t(lang, "btn_cancel")

    def _extract_key(self, message: discord.Message) -> Optional[str]:
        # Prefer explicit key
        if self.key:
            return self.key
        embeds = list(message.embeds)
        if not embeds:
            return None
        desc = embeds[0].description or ""
        # find first backtick section — panel_desc includes `{key}`
        m = re.search(r"`([^`]+)`", desc)
        if m:
            return m.group(1)
        # fallback: try title like "Signup: {key}"
        title = embeds[0].title or ""
        if ":" in title:
            return title.split(":", 1)[1].strip()
        return None

    async def _update_panel(self, interaction: discord.Interaction):
        lang = self.store.get_lang()
        msg = interaction.message
        key = self._extract_key(msg)
        if not key:
            return

        embeds = list(msg.embeds)
        if not embeds:
            emb = discord.Embed(
                title=t(lang, "panel_title", key=key),
                description=t(lang, "panel_desc", key=key),
            )
            embeds = [emb]

        emb = embeds[0]
        if not emb.title:
            emb.title = t(lang, "panel_title", key=key)
        emb.description = t(lang, "panel_desc", key=key)
        emb.set_footer(text=t(lang, "footer_count", count=self.store.count(key)))

        try:
            await msg.edit(embeds=embeds, view=self)
        except Exception as e:
            # Avoid crashing the interaction; log for debugging
            print("[UPDATE_PANEL_ERROR]", e)

    @discord.ui.button(style=discord.ButtonStyle.primary, custom_id="btn_signup")
    async def signup(self, interaction: discord.Interaction, button: discord.ui.Button):
        lang = self.store.get_lang()
        key = self._extract_key(interaction.message)
        if not key:
            await interaction.response.send_message("Missing key", ephemeral=True)
            return
        pos = await self.store.add(key, interaction.user.id)
        await self._update_panel(interaction)
        await interaction.response.send_message(
            t(
                lang,
                "signed_pos",
                name=interaction.user.display_name,
                pos=pos,
                key=key,
            ),
            ephemeral=True,
        )

    @discord.ui.button(style=discord.ButtonStyle.secondary, custom_id="btn_cancel")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        lang = self.store.get_lang()
        key = self._extract_key(interaction.message)
        if not key:
            await interaction.response.send_message("Missing key", ephemeral=True)
            return
        removed = await self.store.remove(key, interaction.user.id)
        await self._update_panel(interaction)
        if removed:
            await interaction.response.send_message(
                t(lang, "cancel_ok"), ephemeral=True
            )
        else:
            await interaction.response.send_message(
                t(lang, "cancel_none"), ephemeral=True
            )


# ====== DM RUN VIEW ======
class DMRunView(discord.ui.View):
    def __init__(self, store: QueueStore, url: Optional[str] = None, *, timeout: Optional[float] = None):
        super().__init__(timeout=timeout)
        self.store = store
        # We want: Go (left) | Leave (right). Add Go first, then Leave.
        if url:
            self.add_item(discord.ui.Button(style=discord.ButtonStyle.link, label="Go", url=url))

        # Dynamic Leave button so we can control order; keep a stable custom_id for persistence.
        leave_button = discord.ui.Button(label="Leave", style=discord.ButtonStyle.danger, custom_id="dm_leave")

        async def on_leave(interaction: discord.Interaction):
            await self.dm_leave(interaction, leave_button)

        leave_button.callback = on_leave  # type: ignore[assignment]
        self.add_item(leave_button)

    async def dm_leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Acknowledge immediately to avoid Unknown interaction (10062)
        try:
            await interaction.response.defer()
        except Exception:
            pass

        # Extract key and guild id from message
        msg = interaction.message
        emb = msg.embeds[0] if msg.embeds else None
        desc = (emb.description if emb else msg.content) or ""
        footer = emb.footer.text if emb and emb.footer else ""

        # key is inside backticks in description; gid is in footer as gid:<id>
        key = None
        gid = None
        m = re.search(r"`([^`]+)`", desc) or re.search(r"key:([^\s]+)", footer)
        if m:
            key = m.group(1)
        mg = re.search(r"gid:(\d+)", footer or desc)
        if mg:
            gid = int(mg.group(1))

        if not key or not gid:
            await interaction.followup.send("Context missing.")
            return

        guild = interaction.client.get_guild(gid)
        if not guild:
            await interaction.followup.send("Guild not found.")
            return
        try:
            await self.store.init_storage(guild)
        except Exception:
            pass
        # Remove user from queue
        await self.store.remove(key, interaction.user.id)
        lang = self.store.get_lang()
        # Prefer the DM embed title (queue title) over fallback "Signup: {key}"
        title = (emb.title if emb and emb.title else None) or t(lang, "panel_title", key=key)
        await interaction.followup.send(
            f"Now you are no longer get any DM from the {title}, but you can always signup again."
        )

        # Refresh panels after responding
        try:
            await update_all_panels_for_key(guild, key, self.store)
        except Exception:
            pass

# ====== BOT ======
class QueueBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=INTENTS)
        self.store = QueueStore()

    async def setup_hook(self):
        # Global sync (may take a few seconds). Use /queue_sync for instant guild sync.
        await self.tree.sync()
        # Re-register a persistent view so old panels keep working after restarts
        # This view infers the key from the message embed when buttons are clicked
        self.add_view(SignupView(key=None, store=self.store, timeout=None))
        # Persistent DM view for DM leave buttons
        self.add_view(DMRunView(store=self.store, timeout=None))
        # Start heartbeat logger/pinger (optional)
        if os.getenv("HEARTBEAT", "1") != "0":
            try:
                asyncio.create_task(heartbeat_task())
                print("[INFO] Heartbeat task started")
            except Exception as e:
                print(f"[WARN] Heartbeat not started: {e}")

    async def on_guild_join(self, guild: discord.Guild):
        await self.tree.sync(guild=guild)


bot = QueueBot()


# ====== HELPERS ======
def admin_only():
    def predicate(interaction: discord.Interaction):
        return (
            interaction.user.guild_permissions.manage_guild
            or interaction.user.guild_permissions.administrator
        )

    return app_commands.check(predicate)


async def ensure_store_ready(interaction: discord.Interaction):
    await bot.store.init_storage(interaction.guild)


# ====== HEARTBEAT / SUPERVISOR ======
async def heartbeat_task():
    try:
        interval = int(os.getenv("HEARTBEAT_INTERVAL", "300"))
    except Exception:
        interval = 300
    port = os.getenv("PORT", "8080")
    url = os.getenv("HEARTBEAT_URL") or f"http://127.0.0.1:{port}/healthz"
    print(f"[HEARTBEAT] Using interval={interval}s url={url}")
    import urllib.request

    while True:
        # Log before sleeping, so you always see when next ping will happen
        print(f"[HEARTBEAT] Next ping in {interval}s -> {url}")
        try:
            await asyncio.sleep(interval)
        except Exception:
            # loop shutdown
            return
        print(f"[HEARTBEAT] Pinging {url} ...")
        try:
            # Synchronous HTTP in thread to avoid extra deps
            def _ping(u: str):
                with urllib.request.urlopen(u, timeout=10) as resp:
                    return resp.status

            status = await asyncio.to_thread(_ping, url)
            print(f"[HEARTBEAT] Pong {status}")
        except Exception as e:
            print(f"[HEARTBEAT_WARN] Ping failed: {e}")


# ====== COMMANDS ======
@bot.tree.command(
    name="queue_setup_storage",
    description="Create hidden storage channel for queues/settings.",
)
@admin_only()
async def queue_setup_storage(interaction: discord.Interaction):
    await ensure_store_ready(interaction)
    lang = bot.store.get_lang()
    await interaction.response.send_message(t(lang, "setup_ok"), ephemeral=True)


@bot.tree.command(
    name="queue_create", description="Create a signup panel with buttons."
)
@admin_only()
@app_commands.describe(key="Key to separate queues (e.g., boss-a)", title="Panel title")
async def queue_create(interaction: discord.Interaction, key: str, title: str):
    await ensure_store_ready(interaction)
    await bot.store.ensure_key(key)
    lang = bot.store.get_lang()

    emb = discord.Embed(title=title, description=t(lang, "panel_desc", key=key))
    emb.set_footer(text=t(lang, "footer_count", count=bot.store.count(key)))
    view = SignupView(key, bot.store)
    await interaction.response.send_message(embed=emb, view=view)


@bot.tree.command(name="queue_set_title", description="Edit the title on existing panels for a queue key.")
@admin_only()
@app_commands.describe(key="Queue key", title="New panel title")
async def queue_set_title(interaction: discord.Interaction, key: str, title: str):
    await ensure_store_ready(interaction)
    # Acknowledge early to avoid interaction timeout while scanning/updating
    try:
        await interaction.response.defer(ephemeral=True, thinking=True)
    except Exception:
        pass
    # Update all panel messages' embed title that reference this key
    lang = bot.store.get_lang()
    updated = 0
    look_desc = f"`{key}`"
    look_title = f": {key}"
    for ch in interaction.guild.text_channels:
        try:
            async for m in ch.history(limit=200, oldest_first=False):
                if m.author != interaction.guild.me or not m.embeds:
                    continue
                emb = m.embeds[0]
                desc = (emb.description or "")
                ttl = (emb.title or "")
                if (look_desc in desc) or (look_title in ttl):
                    new_emb = discord.Embed(title=title, description=t(lang, "panel_desc", key=key))
                    new_emb.set_footer(text=t(lang, "footer_count", count=bot.store.count(key)))
                    await m.edit(embed=new_emb, view=SignupView(key, bot.store))
                    updated += 1
        except Exception:
            continue
    await interaction.followup.send(f"Updated {updated} panels for `{key}`.")


@bot.tree.command(name="queue_list", description="Show queue from oldest to newest.")
@admin_only()
@app_commands.describe(key="Queue key (e.g., boss-a)")
async def queue_list(interaction: discord.Interaction, key: str):
    await ensure_store_ready(interaction)
    lang = bot.store.get_lang()
    users = bot.store.get_list(key)
    if not users:
        await interaction.response.send_message(
            t(lang, "list_empty", key=key), ephemeral=True
        )
        return
    lines = []
    for i, uid in enumerate(users, start=1):
        member = interaction.guild.get_member(uid)
        name = member.display_name if member else f"<@{uid}>"
        lines.append(f"**#{i}** {name} (<@{uid}>)")
    text = "\n".join(lines)
    await interaction.response.send_message(
        t(lang, "list_header", key=key, count=len(users), lines=text), ephemeral=True
    )


@bot.tree.command(name="queue_remove", description="Remove one or multiple users from the queue.")
@admin_only()
@app_commands.describe(
    key="Queue key",
    users="Mentions or IDs separated by space/comma",
)
async def queue_remove(interaction: discord.Interaction, key: str, users: str):
    await ensure_store_ready(interaction)
    lang = bot.store.get_lang()

    # Parse user IDs from mentions/IDs
    ids: List[int] = []
    for token in re.split(r"[\s,]+", users.strip()):
        if not token:
            continue
        m = re.match(r"<@!?([0-9]+)>", token)
        if m:
            ids.append(int(m.group(1)))
        elif token.isdigit():
            ids.append(int(token))
    ids = list(dict.fromkeys(ids))  # unique preserve order

    removed, missing = [], []
    for uid in ids:
        ok = await bot.store.remove(key, uid)
        if ok:
            removed.append(uid)
        else:
            missing.append(uid)

    # Update panels
    try:
        await update_all_panels_for_key(interaction.guild, key, bot.store)
    except Exception:
        pass

    def name(uid: int):
        m = interaction.guild.get_member(uid)
        return m.display_name if m else f"<@{uid}>"

    removed_names = ", ".join(name(u) for u in removed) if removed else "-"
    missing_names = ", ".join(name(u) for u in missing) if missing else "-"
    await interaction.response.send_message(
        t(
            lang,
            "remove_multi_result",
            key=key,
            removed=len(removed),
            missing=len(missing),
            removed_list=removed_names,
            missing_list=missing_names,
        ),
        ephemeral=True,
    )


@bot.tree.command(name="queue_reset", description="Reset a queue.")
@admin_only()
@app_commands.describe(key="Queue key")
async def queue_reset(interaction: discord.Interaction, key: str):
    await ensure_store_ready(interaction)
    lang = bot.store.get_lang()
    await bot.store.reset(key)
    await interaction.response.send_message(
        t(lang, "reset_ok", key=key), ephemeral=True
    )
    # Update all panels with this key across channels
    try:
        await update_all_panels_for_key(interaction.guild, key, bot.store)
    except Exception as e:
        print("[RESET_UPDATE_ERROR]", e)


@bot.tree.command(name="queue_list_n", description="Show the first N users in a queue.")
@admin_only()
@app_commands.describe(
    key="Queue key (e.g., boss-a)",
    count="How many users to show (default 8)",
)
async def queue_list_n(interaction: discord.Interaction, key: str, count: int = 8):
    await ensure_store_ready(interaction)
    lang = bot.store.get_lang()
    users = bot.store.get_list(key)
    if not users:
        await interaction.response.send_message(
            t(lang, "list_empty", key=key), ephemeral=True
        )
        return

    # clamp count
    count = max(1, min(int(count), 50))
    head = users[:count]
    lines = []
    for i, uid in enumerate(head, start=1):
        member = interaction.guild.get_member(uid)
        name = member.display_name if member else f"<@{uid}>"
        lines.append(f"**#{i}** {name} (<@{uid}>)")
    text = "\n".join(lines)
    await interaction.response.send_message(
        t(
            lang,
            "head_header",
            key=key,
            shown=len(head),
            total=len(users),
            lines=text,
        ),
        ephemeral=True,
    )


@bot.tree.command(name="queue_remove_n", description="Remove first N users from a queue.")
@admin_only()
@app_commands.describe(key="Queue key", count="How many to remove from the front")
async def queue_remove_n(interaction: discord.Interaction, key: str, count: int):
    await ensure_store_ready(interaction)
    lang = bot.store.get_lang()
    count = max(1, min(int(count), 50))
    removed_ids = await bot.store.pop_n(key, count)
    try:
        await update_all_panels_for_key(interaction.guild, key, bot.store)
    except Exception:
        pass
    names = []
    for uid in removed_ids:
        m = interaction.guild.get_member(uid)
        names.append(m.display_name if m else f"<@{uid}>")
    removed_text = ", ".join(names) if names else "-"
    await interaction.response.send_message(
        t(lang, "remove_n_result", key=key, n=len(removed_ids), users=removed_text),
        ephemeral=True,
    )


@bot.tree.command(name="queue_notify_boss_run", description="DM the first N users with Accept/Leave buttons.")
@admin_only()
@app_commands.describe(key="Queue key", count="How many users to DM", channel="Target channel to open when Accept")
async def queue_notify_boss_run(interaction: discord.Interaction, key: str, count: int, channel: discord.TextChannel):
    await ensure_store_ready(interaction)
    lang = bot.store.get_lang()
    # Defer early to avoid interaction timeout while sending DMs
    try:
        await interaction.response.defer(ephemeral=True, thinking=True)
    except Exception:
        pass
    users = bot.store.get_list(key)
    if not users:
        await interaction.followup.send(
            t(lang, "list_empty", key=key), ephemeral=True
        )
        return
    count = max(1, min(int(count), 25))
    targets = users[:count]

    sem = asyncio.Semaphore(5)

    async def send_dm(uid: int):
        member = interaction.guild.get_member(uid)
        if not member:
            return uid, False, "not_member"
        try:
            async with sem:
                # Guess a panel title for this key by scanning; fallback to default
                async def guess_title() -> str:
                    look_desc = f"`{key}`"
                    look_title = f": {key}"
                    for ch in interaction.guild.text_channels:
                        try:
                            async for m in ch.history(limit=100, oldest_first=False):
                                if m.author != interaction.guild.me or not m.embeds:
                                    continue
                                emb = m.embeds[0]
                                desc = (emb.description or "")
                                ttl = (emb.title or "")
                                if (look_desc in desc) or (look_title in ttl):
                                    return ttl or t(lang, "panel_title", key=key)
                        except Exception:
                            continue
                    return t(lang, "panel_title", key=key)

                title = await guess_title()
                desc = t(lang, "notify_dm_boss_run", title=title)
                emb = discord.Embed(title=title, description=desc)
                emb.set_footer(text=f"gid:{interaction.guild.id} key:{key}")
                url = f"https://discord.com/channels/{interaction.guild.id}/{channel.id}"
                view = DMRunView(store=bot.store, url=url, timeout=None)
                await member.send(embed=emb, view=view)
            return uid, True, None
        except discord.Forbidden:
            return uid, False, "forbidden"
        except discord.HTTPException as e:
            return uid, False, f"http_{getattr(e, 'status', 'err')}"
        except Exception:
            return uid, False, "error"

    results = await asyncio.gather(*(send_dm(uid) for uid in targets))
    ok = [uid for uid, success, _ in results if success]
    fail = [(uid, reason) for uid, success, reason in results if not success]

    def name(uid: int):
        m = interaction.guild.get_member(uid)
        return m.display_name if m else f"<@{uid}>"

    ok_text = ", ".join(name(u) for u in ok) if ok else "-"
    fail_text = ", ".join(f"{name(u)}({r})" for u, r in fail) if fail else "-"
    await interaction.followup.send(
        t(lang, "notify_result", key=key, sent=len(ok), failed=len(fail), sent_list=ok_text, failed_list=fail_text),
        ephemeral=True,
    )


@bot.tree.command(
    name="queue_sync", description="(admin) Sync slash commands for this server."
)
@admin_only()
async def queue_sync(interaction: discord.Interaction):
    await bot.tree.sync(guild=interaction.guild)
    lang = bot.store.get_lang() if bot.store.storage_channel else DEFAULT_LANG
    await interaction.response.send_message(t(lang, "sync_ok"), ephemeral=True)


# ---- LANGUAGE COMMAND ----
@bot.tree.command(
    name="queue_language", description="Set or show bot language for this server."
)
@admin_only()
@app_commands.describe(lang="Choose language")
@app_commands.choices(
    lang=[
        app_commands.Choice(name="English", value="en"),
        app_commands.Choice(name="Tiếng Việt", value="vi"),
    ]
)
async def language_cmd(
    interaction: discord.Interaction, lang: Optional[app_commands.Choice[str]]
):
    await ensure_store_ready(interaction)
    # show current if not provided
    current = bot.store.get_lang()
    if lang is None:
        lang_name = (
            STRINGS[current]["lang_name_en"]
            if current == "en"
            else STRINGS[current]["lang_name_vi"]
        )
        await interaction.response.send_message(
            t(current, "lang_current", lang_name=lang_name), ephemeral=True
        )
        return
    # set new
    await bot.store.set_lang(lang.value)
    new_lang = bot.store.get_lang()
    lang_name = (
        STRINGS[new_lang]["lang_name_en"]
        if new_lang == "en"
        else STRINGS[new_lang]["lang_name_vi"]
    )
    await interaction.response.send_message(
        t(new_lang, "lang_set_ok", lang_name=lang_name), ephemeral=True
    )


# ====== STARTUP ======
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (id: {bot.user.id})")
    await asyncio.sleep(1)
    # init storage for joined guilds (so /language works right away)
    for guild in bot.guilds:
        try:
            await bot.store.init_storage(guild)
        except Exception as e:
            print(f"init_storage failed for {guild.name}: {e}")


if __name__ == "__main__":
    # Start keep-alive web server (used by Replit free). Disable with KEEP_ALIVE=0
    if os.getenv("KEEP_ALIVE", "1") != "0":
        try:
            from keep_alive import keep_alive

            keep_alive()
            print("[INFO] keep_alive web server started (/: 200)")
        except Exception as e:
            print(f"[WARN] keep_alive not started: {e}")

    # Prefer BOT_TOKEN from environment (via .env). If missing, try fallbacks.
    token_source = "env" if BOT_TOKEN else None
    if not BOT_TOKEN:
        # Fallback 1: parse .env manually in case python-dotenv isn't installed
        try:
            if os.path.exists(".env"):
                with open(".env", "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#"):
                            continue
                        if line.startswith("BOT_TOKEN="):
                            BOT_TOKEN = line.split("=", 1)[1].strip()
                            token_source = token_source or ".env"
                            break
        except Exception:
            pass
    else:
        # If token is present in env but looks like a placeholder, try .env to override
        looks_placeholder = (BOT_TOKEN.count(".") < 2) or BOT_TOKEN.upper().startswith("YOUR")
        if looks_placeholder and os.path.exists(".env"):
            try:
                with open(".env", "r", encoding="utf-8") as f:
                    for line in f:
                        s = line.strip()
                        if not s or s.startswith("#"):
                            continue
                        if s.startswith("BOT_TOKEN="):
                            candidate = s.split("=", 1)[1].strip()
                            if candidate and candidate.count(".") >= 2:
                                BOT_TOKEN = candidate
                                token_source = ".env_override"
                            break
            except Exception:
                pass

    if not BOT_TOKEN:
        # Fallback 2: legacy token file, only if it exists
        try:
            if os.path.exists("token"):
                with open("token", "r", encoding="utf-8") as f:
                    BOT_TOKEN = f.read().strip()
                    token_source = token_source or "token_file"
        except Exception:
            pass

    if not BOT_TOKEN:
        # Final: no token anywhere
        raise SystemExit(STRINGS[DEFAULT_LANG].get("token_missing", "Missing BOT_TOKEN"))

    # Normalize token in case users paste with quotes or prefix
    BOT_TOKEN = BOT_TOKEN.strip()
    if (BOT_TOKEN.startswith('"') and BOT_TOKEN.endswith('"')) or (
        BOT_TOKEN.startswith("'") and BOT_TOKEN.endswith("'")
    ):
        BOT_TOKEN = BOT_TOKEN[1:-1]
    if BOT_TOKEN.lower().startswith("bot "):
        BOT_TOKEN = BOT_TOKEN[4:].strip()

    # Debug: show where token was loaded and a masked preview
    parts = BOT_TOKEN.split('.')
    if os.getenv("DEBUG_SHOW_TOKEN") == "1":
        preview = BOT_TOKEN
    else:
        preview = BOT_TOKEN if len(BOT_TOKEN) <= 10 else (BOT_TOKEN[:4] + "..." + BOT_TOKEN[-4:])
    print(f"[DEBUG] BOT_TOKEN source={token_source or 'unknown'}, length={len(BOT_TOKEN)}, dots={len(parts)-1}, preview={preview}")

    # Quick sanity hint if token doesn't look like a bot token
    if BOT_TOKEN.count(".") < 2:
        print("[WARN] BOT_TOKEN format looks unusual. Ensure it's the Bot Token from Developer Portal > Bot tab, not Client Secret or OAuth token.")

    def _run_once():
        bot.run(BOT_TOKEN)

    supervise = os.getenv("SUPERVISE", "1") != "0"
    if supervise:
        import time
        delay = 5
        while True:
            try:
                print("[SUPERVISOR] Starting bot run loop")
                _run_once()
                print("[SUPERVISOR] Bot exited normally; restarting in 5s")
                time.sleep(5)
            except discord.LoginFailure:
                raise SystemExit(
                    "Login failed: invalid BOT_TOKEN. Regenerate the Bot Token (Developer Portal > Bot > Reset Token) and update your .env."
                )
            except Exception as e:
                print(f"[SUPERVISOR] Bot crashed: {e}; restarting in {delay}s")
                time.sleep(delay)
                delay = min(delay * 2, 300)
    else:
        try:
            _run_once()
        except discord.LoginFailure:
            raise SystemExit(
                "Login failed: invalid BOT_TOKEN. Regenerate the Bot Token (Developer Portal > Bot > Reset Token) and update your .env."
            )
