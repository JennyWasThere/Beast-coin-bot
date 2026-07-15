import asyncio
import io
import json
import logging
import os
from datetime import datetime, timedelta, timezone

import discord
import imagehash
from discord.ext import commands, tasks
from dotenv import load_dotenv
from PIL import Image

load_dotenv()

TOKEN = os.environ.get("DISCORD_TOKEN", "")
CONFIG_PATH = os.environ.get("CONFIG_PATH", "config.json")  # persists settings changed via slash commands across restarts


def load_config():
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_config():
    with open(CONFIG_PATH, "w") as f:
        json.dump({
            "sweep_interval_minutes": TRACK_TTL_MINUTES,
            "strike_limit": STRIKE_LIMIT,
            "hash_threshold": HASH_THRESHOLD,
            "alert_channel_id": ALERT_CHANNEL_ID,
            "log_channel_id": LOG_CHANNEL_ID,
        }, f)


_config = load_config()
TRACK_TTL_MINUTES = _config.get("sweep_interval_minutes", 10)  # how long a tracked entry survives; also the cleanup sweep interval, both adjustable via slash commands
STRIKE_LIMIT = _config.get("strike_limit", 12)
HASH_THRESHOLD = _config.get("hash_threshold", 5)
ALERT_CHANNEL_ID = _config.get("alert_channel_id") or int(os.environ.get("ALERT_CHANNEL_ID") or "0")
LOG_CHANNEL_ID = _config.get("log_channel_id") or int(os.environ.get("LOG_CHANNEL_ID") or "0")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("image-watch")


class DiscordLogHandler(logging.Handler):
    """Mirrors log records to a Discord channel. emit() is sync, so sending is scheduled as a task."""

    def __init__(self, bot):
        super().__init__()
        self.bot = bot

    def emit(self, record):
        channel = self.bot.get_channel(LOG_CHANNEL_ID) if LOG_CHANNEL_ID else None
        if channel:
            self.bot.loop.create_task(self._send(channel, self.format(record)))

    async def _send(self, channel, msg):
        try:
            await channel.send(msg[:2000])
        except discord.HTTPException:
            pass

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

tracked_users = {}  # user_id -> {username, hashes: [imagehash,...], strikes, created_at}


def is_match(hash1, hash2, threshold):
    return (hash1 - hash2) <= threshold


def track_hash(img_hash, username, user_id, tracked=None):
    """Record img_hash against user_id. Strikes only when this user has posted a fuzzy-matching image before."""
    tracked = tracked_users if tracked is None else tracked
    record = tracked.get(user_id)
    if record is None:
        record = {"username": username, "hashes": [], "strikes": 0, "created_at": datetime.now(timezone.utc), "alerted": False}
        tracked[user_id] = record
    record["username"] = username
    if any(is_match(img_hash, seen, HASH_THRESHOLD) for seen in record["hashes"]):
        record["strikes"] += 1
    else:
        record["hashes"].append(img_hash)
    return record


def purge_stale(tracked, ttl_minutes, now):
    cutoff = now - timedelta(minutes=ttl_minutes)
    stale_keys = [key for key, record in tracked.items() if record["created_at"] < cutoff]
    for key in stale_keys:
        del tracked[key]
    return len(stale_keys)


@tasks.loop(minutes=TRACK_TTL_MINUTES)
async def cleanup_tracked_hashes():
    removed = purge_stale(tracked_users, TRACK_TTL_MINUTES, datetime.now(timezone.utc))
    if removed:
        log.info("Cleaned up %d stale tracked user(s)", removed)


@bot.event
async def on_ready():
    if not any(isinstance(h, DiscordLogHandler) for h in log.handlers):
        handler = DiscordLogHandler(bot)
        handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        log.addHandler(handler)
    if not cleanup_tracked_hashes.is_running():
        cleanup_tracked_hashes.start()
    await bot.tree.sync()
    log.info("Logged in as %s", bot.user)


GITHUB_URL = "https://github.com/JennyWasThere/Beast-coin-bot"


@bot.tree.command(name="github", description="Get the link to this bot's source code")
@discord.app_commands.checks.cooldown(1, 60.0)
async def github_command(interaction: discord.Interaction):
    await interaction.response.send_message(GITHUB_URL)


@github_command.error
async def github_command_error(interaction: discord.Interaction, error: discord.app_commands.AppCommandError):
    if isinstance(error, discord.app_commands.CommandOnCooldown):
        await interaction.response.send_message(str(error), ephemeral=True)
    else:
        raise error


@bot.tree.command(name="purge", description="Manually purge stale tracked users now")
@discord.app_commands.default_permissions(manage_guild=True)
async def purge_command(interaction: discord.Interaction):
    removed = purge_stale(tracked_users, TRACK_TTL_MINUTES, datetime.now(timezone.utc))
    await interaction.response.send_message(f"Purged {removed} stale tracked user(s).", ephemeral=True)


@bot.tree.command(name="set-strike-threshold", description="Set how many strikes trigger an alert")
@discord.app_commands.default_permissions(manage_guild=True)
async def set_strike_threshold_command(interaction: discord.Interaction, threshold: int):
    global STRIKE_LIMIT
    if threshold < 1:
        await interaction.response.send_message("Threshold must be at least 1.", ephemeral=True)
        return
    STRIKE_LIMIT = threshold
    save_config()
    await interaction.response.send_message(f"Strike threshold set to {threshold}.", ephemeral=True)


@bot.tree.command(name="set-hash-threshold", description="Set how close two images' hashes must be to count as a match")
@discord.app_commands.default_permissions(manage_guild=True)
async def set_hash_threshold_command(interaction: discord.Interaction, threshold: int):
    global HASH_THRESHOLD
    if threshold < 0:
        await interaction.response.send_message("Threshold must be at least 0.", ephemeral=True)
        return
    HASH_THRESHOLD = threshold
    save_config()
    await interaction.response.send_message(f"Hash threshold set to {threshold}.", ephemeral=True)


@bot.tree.command(name="set-sweep-interval", description="Set how often (in minutes) stale tracked users are purged")
@discord.app_commands.default_permissions(manage_guild=True)
async def set_sweep_interval_command(interaction: discord.Interaction, minutes: int):
    global TRACK_TTL_MINUTES
    if minutes < 1:
        await interaction.response.send_message("Interval must be at least 1 minute.", ephemeral=True)
        return
    TRACK_TTL_MINUTES = minutes
    save_config()
    cleanup_tracked_hashes.change_interval(minutes=minutes)
    await interaction.response.send_message(f"Sweep interval set to {minutes} minute(s).", ephemeral=True)


@bot.tree.command(name="set-alert-channel", description="Set the channel where strike alerts are forwarded")
@discord.app_commands.default_permissions(manage_guild=True)
async def set_alert_channel_command(interaction: discord.Interaction, channel: discord.TextChannel):
    global ALERT_CHANNEL_ID
    ALERT_CHANNEL_ID = channel.id
    save_config()
    await interaction.response.send_message(f"Alert channel set to {channel.mention}.", ephemeral=True)


@bot.tree.command(name="set-log-channel", description="Set the channel where bot logs are mirrored")
@discord.app_commands.default_permissions(manage_guild=True)
async def set_log_channel_command(interaction: discord.Interaction, channel: discord.TextChannel):
    global LOG_CHANNEL_ID
    LOG_CHANNEL_ID = channel.id
    save_config()
    await interaction.response.send_message(f"Log channel set to {channel.mention}.", ephemeral=True)


async def handle_strike_limit(message, record):
    if record["alerted"]:
        log.info("Skipping alert for %s: already alerted", message.author)
        return
    record["alerted"] = True  # claim it now, before the sleep, so a concurrent duplicate can't slip through

    try:
        await asyncio.sleep(10)

        try:
            await message.guild.fetch_member(message.author.id)
        except discord.NotFound:
            log.info("Skipping alert for %s: no longer in the server", message.author)
            record["alerted"] = False  # if they rejoin and spam again, we should still catch it
            return

        channel = bot.get_channel(ALERT_CHANNEL_ID)
        if channel:
            await message.forward(channel)
        else:
            log.warning("Alert channel %s not found; check ALERT_CHANNEL_ID", ALERT_CHANNEL_ID)
            record["alerted"] = False
    except Exception:
        log.exception("Alert flow failed for %s; will retry on next qualifying message", message.author)
        record["alerted"] = False


@bot.event
async def on_message(message):
    if message.author.bot or not message.attachments or message.guild is None:
        return

    record = None
    for attachment in message.attachments:
        if not (attachment.content_type or "").startswith("image/"):
            continue
        try:
            data = await attachment.read()
            with Image.open(io.BytesIO(data)) as img:
                img_hash = imagehash.phash(img)
        except Exception as e:
            log.warning("Could not hash attachment %s: %s", attachment.filename, e)
            continue

        record = track_hash(img_hash, str(message.author), message.author.id)

    if record and record["strikes"] >= STRIKE_LIMIT:
        await handle_strike_limit(message, record)

    await bot.process_commands(message)


if __name__ == "__main__":
    bot.run(TOKEN)
