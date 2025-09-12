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
        with open(os.path.join("lang", fn), encoding="utf-8") as f:
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
            await self.settings_message.edit(
                content=f"{SETTINGS_MESSAGE_PREFIX}\n{payload}"
            )

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


@bot.tree.command(name="queue_kick", description="Remove a user from the queue.")
@admin_only()
@app_commands.describe(key="Queue key", user="Member to remove")
async def queue_kick(interaction: discord.Interaction, key: str, user: discord.Member):
    await ensure_store_ready(interaction)
    lang = bot.store.get_lang()
    ok = await bot.store.remove(key, user.id)
    if ok:
        await interaction.response.send_message(
            t(lang, "kick_ok", user=user.display_name, key=key), ephemeral=True
        )
    else:
        await interaction.response.send_message(
            t(lang, "kick_none", key=key), ephemeral=True
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
    name="language", description="Set or show bot language for this server."
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

    try:
        bot.run(BOT_TOKEN)
    except discord.LoginFailure:
        raise SystemExit(
            "Login failed: invalid BOT_TOKEN. Regenerate the Bot Token (Developer Portal > Bot > Reset Token) and update your .env."
        )
