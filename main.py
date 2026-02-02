import os
import aiohttp
from aiohttp import web
import discord
from discord.ext import commands
from discord import app_commands
from discord.ext import tasks
from datetime import datetime, time as dt_time
from zoneinfo import ZoneInfo
import json
import sys
from typing import Optional  # ← ADDED (needed for Railway-safe typing)

API_BASE = "https://api.amapof.us/mous"
TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
DAILY_CHANNEL_ID = 1466859419781435392
DAILY_TIMEZONE = "Europe/London"
DAILY_STATE_FILE = "daily_post.json"
GUILD_ID = int(os.environ.get("DISCORD_GUILD_ID", "0"))
HEALTH_RUNNER: Optional[web.AppRunner] = None

# ============================
# STATUS CONFIG (ADDED)
# ============================
STATUS_TEXT = "Exploring The Map 🌎"
STATUS_TYPE = discord.ActivityType.watching


class MousBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        intents.reactions = True
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        try:
            if GUILD_ID:
                guild = discord.Object(id=GUILD_ID)
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
            else:
                await self.tree.sync()
        except Exception as exc:
            print(f"[ERROR] Slash command sync failed: {exc}")

        await start_health_server()
        if not daily_mous.is_running():
            daily_mous.start()


bot = MousBot()

REACTION_ROLE_MAP: dict[int, dict[str, int]] = {}
AUTO_REACT_CHANNELS: dict[int, list[str]] = {}
AUTO_REACT_KEYWORDS: dict[str, list[str]] = {}


async def fetch_payload(url: str) -> dict:
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"API {resp.status}: {text[:300]}")
            return await resp.json()


def unwrap_data(payload: object) -> dict:
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
    return "*No text found.*"


def build_embed(payload: dict) -> discord.Embed:
    data = unwrap_data(payload)

    username = first_present(data, "username", "user", "author", "display_name", "name") or "Unknown"
    memory_date = first_present(data, "memory_date", "memoryDate", "date") or "Unknown date"
    category = first_present(data, "category", "type") or "mous"
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
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return None


def save_last_post_date(date_str: str) -> None:
    try:
        with open(DAILY_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"last_post_date": date_str}, f)
    except OSError:
        pass


def is_admin(interaction: discord.Interaction) -> bool:
    if interaction.guild is None:
        return False
    perms = interaction.user.guild_permissions
    return perms.administrator or perms.manage_guild


async def start_health_server() -> None:
    global HEALTH_RUNNER
    port_str = os.environ.get("PORT")
    if not port_str:
        return
    if HEALTH_RUNNER is not None:
        return

    port = int(port_str)

    async def _health(_: web.Request) -> web.Response:
        return web.Response(text="OK")

    app = web.Application()
    app.router.add_get("/", _health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()
    HEALTH_RUNNER = runner


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

    try:
        payload = await fetch_payload(f"{API_BASE}/random")
        embed = build_embed(payload)
        await channel.send(embed=embed)
        save_last_post_date(today)
    except Exception:
        pass


@bot.event
async def on_ready():
    # ============================
    # STATUS SET HERE (ADDED)
    # ============================
    activity = discord.Activity(type=STATUS_TYPE, name=STATUS_TEXT)
    await bot.change_presence(
        status=discord.Status.online,
        activity=activity
    )
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")


@bot.tree.command(name="mous_random", description="Get a random memory from A Map of Us.")
async def mous_random(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    try:
        payload = await fetch_payload(f"{API_BASE}/random")
        embed = build_embed(payload)
        await interaction.followup.send(embed=embed)
    except Exception as exc:
        await interaction.followup.send(f"Failed to fetch memory: {exc}")


@bot.tree.command(name="reload_commands", description="Reload slash commands (sync).")
@app_commands.check(is_admin)
async def reload_commands(interaction: discord.Interaction):
    if interaction.guild is not None:
        commands_synced = await bot.tree.sync(guild=interaction.guild)
    else:
        commands_synced = await bot.tree.sync()
    await interaction.response.send_message(
        f"Reloaded {len(commands_synced)} slash commands.",
        ephemeral=True,
    )


@bot.tree.command(name="restart", description="Restart the bot process.")
@app_commands.check(is_admin)
async def restart(interaction: discord.Interaction):
    await interaction.response.send_message("Restarting...", ephemeral=True)

    async def _restart():
        await bot.close()
        os.execv(sys.executable, [sys.executable] + sys.argv)

    bot.loop.create_task(_restart())


@reload_commands.error
@restart.error
async def admin_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        await interaction.response.send_message("You do not have permission to use this.", ephemeral=True)
        return
    await interaction.response.send_message(f"Command failed: {error}", ephemeral=True)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set DISCORD_BOT_TOKEN environment variable.")
    bot.run(TOKEN)
