import os
import json
from datetime import datetime, time as dt_time
from typing import Optional, Any, Dict

import aiohttp
import discord
from discord.ext import commands, tasks
from discord import app_commands
from zoneinfo import ZoneInfo

API_BASE = "https://api.amapof.us/mous"
TOKEN = os.environ.get("DISCORD_BOT_TOKEN")

DAILY_CHANNEL_ID = 1466859419781435392
DAILY_TIMEZONE = "Europe/London"
DAILY_STATE_FILE = "daily_post.json"


class MousBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        intents.reactions = True
        intents.message_content = True  # Requires enabling Message Content Intent in Discord Dev Portal
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        # Sync slash commands
        await self.tree.sync()

        # Start daily task once
        if not daily_mous.is_running():
            daily_mous.start()


bot = MousBot()

# ----------------------------
# Reaction roles configuration
# ----------------------------
REACTION_ROLE_MAP: Dict[int, Dict[str, int]] = {}

# ----------------------------
# Auto reaction configuration
# ----------------------------
AUTO_REACT_CHANNELS: Dict[int, list[str]] = {}
AUTO_REACT_KEYWORDS: Dict[str, list[str]] = {}


async def fetch_payload(url: str) -> Any:
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"API {resp.status}: {text[:300]}")
            return await resp.json()


def unwrap_data(payload: Any) -> dict:
    cur = payload
    for _ in range(6):
        if isinstance(cur, dict) and "data" in cur:
            nxt = cur["data"]
            if isinstance(nxt, dict):
                cur = nxt
                continue
            if isinstance(nxt, list) and nxt and isinstance(nxt[0], dict):
                cur = nxt[0]
                continue
        break
    return cur if isinstance(cur, dict) else {}


def first_present(d: dict, *keys: str):
    for k in keys:
        v = d.get(k)
        if v is None:
            continue
        if isinstance(v, str) and not v.strip():
            continue
        return v
    return None


def pick_text(data: dict) -> str:
    t = first_present(data, "text", "message", "content", "body")
    if isinstance(t, str) and t.strip():
        return t.strip()

    translations = data.get("translations") or {}
    lang = (first_present(data, "language", "lang") or "").strip()

    if isinstance(translations, dict) and lang:
        translated = translations.get(lang)
        if isinstance(translated, str) and translated.strip():
            return translated.strip()
        if isinstance(translated, dict):
            tt = first_present(translated, "text", "value", "content")
            if isinstance(tt, str) and tt.strip():
                return tt.strip()

    return "*No text found.*"


def build_embed(payload: dict) -> discord.Embed:
    data = unwrap_data(payload)

    username = first_present(data, "username", "user", "author", "display_name", "name") or "Unknown"
    memory_date = first_present(data, "memory_date", "memoryDate", "date") or "Unknown date"
    category = first_present(data, "category", "type") or "mous"

    if memory_date == "Unknown date":
        created_at = first_present(data, "created_at", "createdAt")
        if isinstance(created_at, str) and created_at.strip():
            memory_date = created_at

    mous_id = first_present(data, "id", "ID") or "unknown-id"
    text = pick_text(data)

    embed = discord.Embed(
        title=f"{category} • {memory_date}",
        description=text[:4096],
        color=discord.Color.blurple(),
    )
    embed.set_author(name=str(username))
    embed.set_footer(text=f"ID: {mous_id}")
    return embed


def load_last_post_date() -> Optional[str]:
    try:
        with open(DAILY_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            value = data.get("last_post_date")
            return value if isinstance(value, str) else None
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError):
        return None
    return None


def save_last_post_date(date_str: str) -> None:
    try:
        with open(DAILY_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"last_post_date": date_str}, f)
    except OSError:
        pass


async def get_random_mous_full() -> Any:
    """
    /mous/random returns only {"id": "..."} in your case.
    So we fetch /mous/{id} afterwards.
    """
    rand_payload = await fetch_payload(f"{API_BASE}/random")

    rand_data = unwrap_data(rand_payload)
    maybe_username = first_present(rand_data, "username", "text", "memory_date", "category")
    if maybe_username is not None and (
        "text" in rand_data or "username" in rand_data or "memory_date" in rand_data
    ):
        return rand_payload

    rand_id = first_present(rand_payload, "id", "ID")
    if not isinstance(rand_id, str) or not rand_id.strip():
        raise RuntimeError(f"/mous/random did not return an id. Got: {rand_payload}")

    return await fetch_payload(f"{API_BASE}/{rand_id}")


# Slash command group: /mous ...
mous_group = app_commands.Group(name="mous", description="Request Mous")


@mous_group.command(name="random", description="Get a random Mous (via the API).")
async def mous_random(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    try:
        payload = await get_random_mous_full()
        embed = build_embed(payload)
        data = unwrap_data(payload)
        mous_id = first_present(data, "id", "ID")
        if isinstance(mous_id, str) and mous_id.strip():
            embed.add_field(
                name="View",
                value=f"https://amapof.us/map?mou={mous_id}",
                inline=False,
            )
        await interaction.followup.send(embed=embed)
    except Exception as e:
        await interaction.followup.send(f"❌ Failed to fetch random Mous: `{e}`", ephemeral=True)


@mous_group.command(name="id", description="Get a Mous by ID (via the API).")
@app_commands.describe(mous_id="The Mous ID to fetch")
async def mous_by_id(interaction: discord.Interaction, mous_id: str):
    await interaction.response.defer(thinking=True)
    try:
        payload = await fetch_payload(f"{API_BASE}/{mous_id}")
        await interaction.followup.send(embed=build_embed(payload))
    except Exception as e:
        await interaction.followup.send(f"❌ Failed to fetch Mous {mous_id}: `{e}`", ephemeral=True)


@mous_group.command(name="debug", description="Show raw random Mous payload (truncated).")
async def mous_debug(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True, ephemeral=True)
    try:
        payload = await fetch_payload(f"{API_BASE}/random")
        s = str(payload)
        if len(s) > 1800:
            s = s[:1800] + "…"
        await interaction.followup.send(f"```{s}```", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Debug failed: `{e}`", ephemeral=True)


bot.tree.add_command(mous_group)


@tasks.loop(time=dt_time(hour=0, minute=0, tzinfo=ZoneInfo(DAILY_TIMEZONE)))
async def daily_mous():
    today = datetime.now(tz=ZoneInfo(DAILY_TIMEZONE)).date().isoformat()
    if load_last_post_date() == today:
        return

    channel = bot.get_channel(DAILY_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(DAILY_CHANNEL_ID)
        except discord.HTTPException:
            return

    # If the fetched channel isn't messageable, stop
    if not isinstance(channel, (discord.TextChannel, discord.Thread, discord.DMChannel)):
        return

    try:
        payload = await get_random_mous_full()
        embed = build_embed(payload)
        data = unwrap_data(payload)
        mous_id = first_present(data, "id", "ID")
        if isinstance(mous_id, str) and mous_id.strip():
            embed.add_field(
                name="View",
                value=f"https://amapof.us/map?mou={mous_id}",
                inline=False,
            )
        await channel.send(embed=embed)
        save_last_post_date(today)
    except Exception:
        pass


@daily_mous.before_loop
async def before_daily_mous():
    # Ensure bot is logged in and ready before task runs
    await bot.wait_until_ready()


@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    channel_emojis = AUTO_REACT_CHANNELS.get(message.channel.id)
    if channel_emojis:
        for emoji in channel_emojis:
            try:
                await message.add_reaction(emoji)
            except discord.HTTPException:
                pass

    if AUTO_REACT_KEYWORDS and message.content:
        content_lower = message.content.lower()
        for keyword, emojis in AUTO_REACT_KEYWORDS.items():
            if keyword.lower() in content_lower:
                for emoji in emojis:
                    try:
                        await message.add_reaction(emoji)
                    except discord.HTTPException:
                        pass

    await bot.process_commands(message)


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if bot.user and payload.user_id == bot.user.id:
        return
    role_map = REACTION_ROLE_MAP.get(payload.message_id)
    if not role_map:
        return
    emoji = str(payload.emoji)
    role_id = role_map.get(emoji)
    if not role_id:
        return

    guild = bot.get_guild(payload.guild_id) if payload.guild_id else None
    if not guild:
        return
    member = guild.get_member(payload.user_id)
    if not member:
        try:
            member = await guild.fetch_member(payload.user_id)
        except discord.HTTPException:
            return
    role = guild.get_role(role_id)
    if not role:
        return
    try:
        await member.add_roles(role, reason="Reaction role")
    except discord.HTTPException:
        pass


@bot.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
    role_map = REACTION_ROLE_MAP.get(payload.message_id)
    if not role_map:
        return
    emoji = str(payload.emoji)
    role_id = role_map.get(emoji)
    if not role_id:
        return

    guild = bot.get_guild(payload.guild_id) if payload.guild_id else None
    if not guild:
        return
    member = guild.get_member(payload.user_id)
    if not member:
        try:
            member = await guild.fetch_member(payload.user_id)
        except discord.HTTPException:
            return
    role = guild.get_role(role_id)
    if not role:
        return
    try:
        await member.remove_roles(role, reason="Reaction role")
    except discord.HTTPException:
        pass


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set DISCORD_BOT_TOKEN environment variable.")
    bot.run(TOKEN)
