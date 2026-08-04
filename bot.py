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


DEFAULT_CONFIG = {
    "sweep_interval_minutes": 10,  # how long a tracked entry survives; also the cleanup sweep interval, both adjustable via slash commands
    "strike_limit": 12,
    "hash_threshold": 5,
    "mod_channel_id": 0,
    "bot_log_channel_id": 0,
    "ban_log_channel_id": 0,
}
# channel ids may also come from the environment on first run, before any slash command has set them in the config file
CHANNEL_ENV_VARS = {
    "mod_channel_id": "MOD_CHANNEL_ID",
    "bot_log_channel_id": "BOT_LOG_CHANNEL_ID",
    "ban_log_channel_id": "BAN_LOG_CHANNEL_ID",
}


def load_config():
    try:
        with open(CONFIG_PATH) as f:
            loaded = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        loaded = {}
    merged = {**DEFAULT_CONFIG, **loaded}
    for key, env_var in CHANNEL_ENV_VARS.items():
        merged[key] = merged[key] or int(os.environ.get(env_var) or "0")
    return merged


def save_config():
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f)


config = load_config()

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("image-watch")


class DiscordLogHandler(logging.Handler):
    """Mirrors log records to a Discord channel. emit() is sync, so sending is scheduled as a task."""

    def __init__(self, bot):
        super().__init__()
        self.bot = bot

    def emit(self, record):
        channel = self.bot.get_channel(config["bot_log_channel_id"]) if config["bot_log_channel_id"] else None
        if channel:
            self.bot.loop.create_task(self._send(channel, self.format(record)))

    async def _send(self, channel, msg):
        try:
            await channel.send(msg[:2000])
        except discord.HTTPException:
            pass

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)  # required by commands.Bot; unused, all commands are slash commands

tracked_users = {}  # user_id -> {username, hashes: [imagehash,...], strikes, created_at}


def is_match(hash1, hash2, threshold):
    return (hash1 - hash2) <= threshold


def track_hash(img_hash, username, user_id, tracked=None):
    """Record img_hash against user_id. Strikes only when this user has posted a fuzzy-matching image before."""
    tracked = tracked_users if tracked is None else tracked
    record = tracked.get(user_id)
    if record is None:
        record = {"username": username, "hashes": [], "strikes": 0, "created_at": datetime.now(timezone.utc), "alerted": False, "activity": []}
        tracked[user_id] = record
    record["username"] = username
    if any(is_match(img_hash, seen, config["hash_threshold"]) for seen in record["hashes"]):
        record["strikes"] += 1
    else:
        record["hashes"].append(img_hash)
    return record


def purge_stale(tracked, ttl_minutes, now):
    cutoff = now - timedelta(minutes=ttl_minutes)
    stale_keys = [key for key, record in tracked.items() if record["created_at"] < cutoff]
    removed = []
    for key in stale_keys:
        removed.append((key, tracked.pop(key)))
    return removed


def format_activity_report(record):
    """Return a user's activity as one 'channel : time' line per entry."""
    lines = [f"{entry['channel']} : {entry['time'].strftime('%Y-%m-%d %H:%M:%S')}" for entry in record["activity"]]
    return "\n".join([f"{record['username']}:"] + lines)


async def report_expired_activity(records):
    """Log each removed user's activity to the bot log channel when they met the strike limit."""
    channel = bot.get_channel(config["bot_log_channel_id"]) if config["bot_log_channel_id"] else None
    if not channel:
        return
    for _, record in records:
        if record["strikes"] < config["strike_limit"]:
            continue
        try:
            await channel.send(format_activity_report(record)[:2000])
        except discord.HTTPException:
            log.exception("Failed to send activity report to bot log channel")


@tasks.loop(minutes=config["sweep_interval_minutes"])
async def cleanup_tracked_hashes():
    removed = purge_stale(tracked_users, config["sweep_interval_minutes"], datetime.now(timezone.utc))
    if not removed:
        return
    log.info("Cleaned up %d stale tracked user(s)", len(removed))
    await report_expired_activity(removed)


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


@bot.tree.command(name="about", description="Get the link to this bot's source code")
@discord.app_commands.checks.cooldown(1, 60.0)
async def about_command(interaction: discord.Interaction):
    await interaction.response.send_message(GITHUB_URL)


@about_command.error
async def about_command_error(interaction: discord.Interaction, error: discord.app_commands.AppCommandError):
    if isinstance(error, discord.app_commands.CommandOnCooldown):
        await interaction.response.send_message(str(error), ephemeral=True)
    else:
        raise error


@bot.tree.command(name="purge", description="Manually purge all tracked users now")
@discord.app_commands.default_permissions(manage_guild=True)
async def purge_command(interaction: discord.Interaction):
    removed = list(tracked_users.items())
    tracked_users.clear()
    await interaction.response.send_message(f"Purged {len(removed)} tracked user(s).", ephemeral=True)
    if removed:
        await report_expired_activity(removed)


@bot.tree.command(name="set-strike-threshold", description="Set how many strikes trigger an alert")
@discord.app_commands.default_permissions(manage_guild=True)
async def set_strike_threshold_command(interaction: discord.Interaction, threshold: int):
    if threshold < 1:
        await interaction.response.send_message("Threshold must be at least 1.", ephemeral=True)
        return
    config["strike_limit"] = threshold
    save_config()
    await interaction.response.send_message(f"Strike threshold set to {threshold}.", ephemeral=True)


@bot.tree.command(name="set-hash-threshold", description="Set how close two images' hashes must be to count as a match")
@discord.app_commands.default_permissions(manage_guild=True)
async def set_hash_threshold_command(interaction: discord.Interaction, threshold: int):
    if threshold < 0:
        await interaction.response.send_message("Threshold must be at least 0.", ephemeral=True)
        return
    config["hash_threshold"] = threshold
    save_config()
    await interaction.response.send_message(f"Hash threshold set to {threshold}.", ephemeral=True)


@bot.tree.command(name="set-sweep-interval", description="Set how often (in minutes) stale tracked users are purged")
@discord.app_commands.default_permissions(manage_guild=True)
async def set_sweep_interval_command(interaction: discord.Interaction, minutes: int):
    if minutes < 1:
        await interaction.response.send_message("Interval must be at least 1 minute.", ephemeral=True)
        return
    config["sweep_interval_minutes"] = minutes
    save_config()
    cleanup_tracked_hashes.change_interval(minutes=minutes)
    await interaction.response.send_message(f"Sweep interval set to {minutes} minute(s).", ephemeral=True)


@bot.tree.command(name="set-mod-channel", description="Set the channel where strike alerts are forwarded")
@discord.app_commands.default_permissions(manage_guild=True)
async def set_mod_channel_command(interaction: discord.Interaction, channel: discord.TextChannel):
    config["mod_channel_id"] = channel.id
    save_config()
    await interaction.response.send_message(f"Mod channel set to {channel.mention}.", ephemeral=True)


@bot.tree.command(name="set-bot-log-channel", description="Set the channel where bot logs are mirrored")
@discord.app_commands.default_permissions(manage_guild=True)
async def set_bot_log_channel_command(interaction: discord.Interaction, channel: discord.TextChannel):
    config["bot_log_channel_id"] = channel.id
    save_config()
    await interaction.response.send_message(f"Bot log channel set to {channel.mention}.", ephemeral=True)


@bot.tree.command(name="set-ban-log-channel", description="Set the channel where honeypot ban notices are forwarded")
@discord.app_commands.default_permissions(manage_guild=True)
async def set_ban_log_channel_command(interaction: discord.Interaction, channel: discord.TextChannel):
    config["ban_log_channel_id"] = channel.id
    save_config()
    await interaction.response.send_message(f"Ban log channel set to {channel.mention}.", ephemeral=True)


HONEYPOT_PHRASE = "triggered honeypot channel"


def is_honeypot_ban_notice(message):
    parts = []
    for embed in message.embeds:
        parts += [embed.title, embed.description]
        parts += [f"{f.name} {f.value}" for f in embed.fields]
    return any(part and HONEYPOT_PHRASE in part.lower() for part in parts)


async def forward_to(message, channel_id, label):
    """Forward message to channel_id. Returns True on success; logs a warning/exception and returns False otherwise."""
    channel = bot.get_channel(channel_id) if channel_id else None
    if not channel:
        if channel_id:
            log.warning("%s channel %s not found", label, channel_id)
        return False
    try:
        await message.forward(channel)
        return True
    except discord.HTTPException:
        log.exception("Failed to forward message to %s channel", label)
        return False


async def handle_strike_limit(message, record):
    if record["alerted"]:
        log.debug("Skipping alert for %s: already alerted", message.author)
        return
    record["alerted"] = True  # claim it now, before the sleep, so a concurrent duplicate can't slip through

    try:
        await asyncio.sleep(10)

        try:
            await message.guild.fetch_member(message.author.id)
        except discord.NotFound:
            log.debug("Skipping alert for %s: no longer in the server", message.author)
            record["alerted"] = False  # if they rejoin and spam again, we should still catch it
            return

        if not await forward_to(message, config["mod_channel_id"], "Mod"):
            record["alerted"] = False
    except Exception:
        log.exception("Alert flow failed for %s; will retry on next qualifying message", message.author)
        record["alerted"] = False


@bot.event
async def on_message(message):
    if message.guild is None:
        return

    if message.author.bot:
        if message.channel.id == config["bot_log_channel_id"] and message.embeds and is_honeypot_ban_notice(message):
            await forward_to(message, config["ban_log_channel_id"], "Ban log")
        return

    if not message.attachments:
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

    if record:
        record["activity"].append({"time": message.created_at, "channel": message.channel.name})

    if record and record["strikes"] >= config["strike_limit"]:
        await handle_strike_limit(message, record)


if __name__ == "__main__":
    bot.run(TOKEN)
