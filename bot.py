import asyncio
import io
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Literal

import discord
import imagehash
from discord.ext import tasks
from dotenv import load_dotenv
from PIL import Image

Image.MAX_IMAGE_PIXELS = 20_000_000  # reject huge decoded images; a small file can decode to a giant bitmap

load_dotenv()

TOKEN = os.environ.get("DISCORD_TOKEN", "")
CONFIG_PATH = os.environ.get("CONFIG_PATH", "config.json")  # persists settings changed via slash commands across restarts
HASHES_PATH = os.path.join(os.path.dirname(CONFIG_PATH), "hashes.json")  # watched image hashes; next to config so it shares the mounted volume


DEFAULT_CONFIG = {
    "sweep_interval_minutes": 10,  # how long a tracked entry survives; also the cleanup sweep interval, both adjustable via slash commands
    "strike_limit": 12,
    "hash_threshold": 5,
    "mod_channel_id": 0,
    "bot_log_channel_id": 0,
    "ban_log_channel_id": 0,
}


def load_json(path, default):
    try:
        with open(path) as f:
            data = json.load(f)
    except FileNotFoundError:
        return default
    except json.JSONDecodeError:
        log.warning("Could not parse %s; using the default value", path)
        return default
    if not isinstance(data, type(default)):
        log.warning("Ignoring %s: expected %s, got %s", path, type(default).__name__, type(data).__name__)
        return default
    return data


def save_json(path, data):
    """Write through a temp file. A crash mid-write must not truncate the saved data."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def parse_channel_id(env_var):
    raw = os.environ.get(env_var)
    if not raw:
        return 0
    try:
        return int(raw)
    except ValueError:
        startup_warnings.append(f"Ignoring {env_var}={raw!r}: not a channel id")
        return 0


def load_config():
    merged = {**DEFAULT_CONFIG, **load_json(CONFIG_PATH, {})}
    # channel ids may also come from the environment on first run, before any slash command has set them in the config file
    for key in ("mod_channel_id", "bot_log_channel_id", "ban_log_channel_id"):
        merged[key] = merged[key] or parse_channel_id(key.upper())
    for key, minimum in (("sweep_interval_minutes", 1), ("strike_limit", 1), ("hash_threshold", 0)):
        if not isinstance(merged[key], int) or merged[key] < minimum:
            startup_warnings.append(f"Ignoring {key}={merged[key]!r}: must be an integer of at least {minimum}")
            merged[key] = DEFAULT_CONFIG[key]
    return merged


def load_hashes():
    hashes = []
    for value in load_json(HASHES_PATH, []):
        if not isinstance(value, str) or len(value) != 16:  # phash is 64 bits -> 16 hex chars; other shapes crash the compare
            log.warning("Skipping malformed saved hash: %r", value)
            continue
        try:
            hashes.append(imagehash.hex_to_hash(value))
        except ValueError:
            log.warning("Skipping malformed saved hash: %r", value)
    return hashes


def save_hashes():
    save_json(HASHES_PATH, [str(h) for h in known_hashes])


logging.basicConfig(level=logging.INFO)
log = logging.getLogger("image-watch")

startup_warnings = []  # config problems found before the Discord log handler exists; flushed in on_ready
config = load_config()
known_hashes = load_hashes()  # images a mod marked with /hash-image; a repost of any of them alerts the mod channel


class DiscordLogHandler(logging.Handler):
    """Mirrors log records to a Discord channel. emit() is sync, so sending is scheduled as a task."""

    def emit(self, record):
        channel = bot.get_channel(config["bot_log_channel_id"])
        if channel:
            bot.loop.create_task(self._send(channel, self.format(record)))

    async def _send(self, channel, msg):
        try:
            await channel.send(msg[:2000])
        except discord.HTTPException:
            pass

intents = discord.Intents.default()
intents.message_content = True  # without this, Discord sends empty attachments and embeds; nothing gets watched
bot = discord.Client(intents=intents)
tree = discord.app_commands.CommandTree(bot)

tracked_users = {}  # user_id -> {username, hashes: [imagehash,...], strikes, created_at}


def is_match(hash1, hash2, threshold):
    return (hash1 - hash2) <= threshold


def parse_message_link(value):
    """Return (channel_id, message_id) from a Discord message link, or None if the text is not one."""
    parts = value.rstrip("/").split("/")
    try:
        return int(parts[-2]), int(parts[-1])
    except (IndexError, ValueError):
        return None


def find_known_match(img_hash):
    """Return the first watched hash that matches img_hash, or None."""
    return next((seen for seen in known_hashes if is_match(img_hash, seen, config["hash_threshold"])), None)


def hash_image_bytes(data):
    with Image.open(io.BytesIO(data)) as img:
        return imagehash.phash(img)


async def hash_attachment(attachment):
    """Return the image hash of an image attachment, or None if it is not an image or cannot be hashed."""
    if not (attachment.content_type or "").startswith("image/"):
        return None
    try:
        data = await attachment.read()
        return await asyncio.to_thread(hash_image_bytes, data)  # hashing is CPU-bound; keep it off the event loop
    except Exception as e:
        log.warning("Could not hash attachment %s: %s", attachment.filename, e)
        return None


def track_hash(img_hash, username, user_id):
    """Record img_hash against user_id. Strikes only when this user has posted a fuzzy-matching image before."""
    record = tracked_users.get(user_id)
    if record is None:
        record = {"username": username, "hashes": [], "strikes": 0, "created_at": datetime.now(timezone.utc), "alerted": False, "activity": [], "hash_alerts": []}
        tracked_users[user_id] = record
    record["username"] = username
    if any(is_match(img_hash, seen, config["hash_threshold"]) for seen in record["hashes"]):
        record["strikes"] += 1
    else:
        record["hashes"].append(img_hash)
    return record


def purge_stale(tracked, ttl_minutes, now):
    cutoff = now - timedelta(minutes=ttl_minutes)
    stale_keys = [key for key, record in tracked.items() if record["created_at"] < cutoff]
    return [tracked.pop(key) for key in stale_keys]


def format_activity_report(record):
    """Return a user's activity as one 'channel : time' line per entry."""
    lines = [f"{entry['channel']} : {entry['time'].strftime('%Y-%m-%d %H:%M:%S')}" for entry in record["activity"]]
    return "\n".join([f"{record['username']}:"] + lines)


async def report_expired_activity(records):
    """Log each removed user's activity to the bot log channel when they met the strike limit."""
    channel = bot.get_channel(config["bot_log_channel_id"])
    if not channel:
        return
    for record in records:
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
        handler = DiscordLogHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        log.addHandler(handler)
    for warning in startup_warnings:
        log.warning(warning)
    startup_warnings.clear()
    if not cleanup_tracked_hashes.is_running():
        cleanup_tracked_hashes.start()
    await tree.sync()
    log.info("Logged in as %s", bot.user)


@tree.command(name="about", description="Get the link to this bot's source code")
async def about_command(interaction: discord.Interaction):
    await interaction.response.send_message("https://github.com/JennyWasThere/Beast-coin-bot", ephemeral=True)


@tree.command(name="purge", description="Manually purge all tracked users now")
@discord.app_commands.default_permissions(manage_guild=True)
async def purge_command(interaction: discord.Interaction):
    removed = list(tracked_users.values())
    tracked_users.clear()
    await interaction.response.send_message(f"Purged {len(removed)} tracked user(s).", ephemeral=True)
    await report_expired_activity(removed)


@tree.command(name="set-threshold", description="Set how many strikes trigger an alert, or how close two images must be to match")
@discord.app_commands.default_permissions(manage_guild=True)
async def set_threshold_command(
    interaction: discord.Interaction, kind: Literal["strike", "hash"], threshold: discord.app_commands.Range[int, 0]
):
    if kind == "strike" and threshold < 1:  # a strike limit of 0 alerts on every image
        await interaction.response.send_message("Strike threshold must be at least 1.", ephemeral=True)
        return
    config["strike_limit" if kind == "strike" else "hash_threshold"] = threshold
    save_json(CONFIG_PATH, config)
    await interaction.response.send_message(f"{kind.capitalize()} threshold set to {threshold}.", ephemeral=True)


@tree.command(name="set-sweep-interval", description="Set how often (in minutes) stale tracked users are purged")
@discord.app_commands.default_permissions(manage_guild=True)
async def set_sweep_interval_command(interaction: discord.Interaction, minutes: discord.app_commands.Range[int, 1]):
    config["sweep_interval_minutes"] = minutes
    save_json(CONFIG_PATH, config)
    cleanup_tracked_hashes.change_interval(minutes=minutes)
    await interaction.response.send_message(f"Sweep interval set to {minutes} minute(s).", ephemeral=True)


@tree.command(name="set-channel", description="Set the mod, bot log, or ban log channel")
@discord.app_commands.default_permissions(manage_guild=True)
async def set_channel_command(
    interaction: discord.Interaction, kind: Literal["mod", "bot_log", "ban_log"], channel: discord.TextChannel
):
    config[f"{kind}_channel_id"] = channel.id
    save_json(CONFIG_PATH, config)
    await interaction.response.send_message(f"{kind} channel set to {channel.mention}.", ephemeral=True)


@tree.command(name="hash-image", description="Add the images in a linked message to the watch list")
@discord.app_commands.default_permissions(manage_guild=True)
async def hash_image_command(interaction: discord.Interaction, message_link: str):
    parsed = parse_message_link(message_link)
    if parsed is None:
        await interaction.response.send_message("Provide a message link (Discord's Copy Message Link).", ephemeral=True)
        return
    channel_id, message_id = parsed
    await interaction.response.defer(ephemeral=True)
    try:
        channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
        message = await channel.fetch_message(message_id)
    except discord.HTTPException:
        await interaction.followup.send("I could not read that message. Check the link and my channel permissions.", ephemeral=True)
        return
    if message.guild is None or message.guild.id != interaction.guild_id:
        await interaction.followup.send("That link is from another server.", ephemeral=True)
        return
    added = await add_message_hashes(message)
    save_hashes()
    if added == 0:
        await interaction.followup.send("No new images to add.", ephemeral=True)
        return
    await interaction.followup.send(f"Added {added} new image hash(es).", ephemeral=True)


@tree.command(name="clear-hashes", description="Wipe every image hash from the watch list")
@discord.app_commands.default_permissions(manage_guild=True)
async def clear_hashes_command(interaction: discord.Interaction):
    count = len(known_hashes)
    known_hashes.clear()
    for record in tracked_users.values():
        record["hash_alerts"].clear()
    save_hashes()
    await interaction.response.send_message(f"Cleared {count} watched image hash(es).", ephemeral=True)


HONEYPOT_PHRASE = "triggered honeypot channel"


def is_honeypot_ban_notice(message):
    parts = []
    for embed in message.embeds:
        parts += [embed.title, embed.description]
        parts += [f"{f.name} {f.value}" for f in embed.fields]
    return any(part and HONEYPOT_PHRASE in part.lower() for part in parts)


async def forward_to(message, channel_id, label):
    """Forward message to channel_id.
    Returns True on success, False on a failure that can retry, and None if the source message is gone."""
    channel = bot.get_channel(channel_id)
    if not channel:
        if channel_id:
            log.warning("%s channel %s not found", label, channel_id)
        return False
    try:
        await message.forward(channel)
        return True
    except discord.NotFound:
        return None  # the source message was deleted; a retry cannot help
    except discord.Forbidden:
        log.warning("Missing permissions to forward to %s channel %s", label, channel_id)
        return False
    except discord.HTTPException:
        log.exception("Failed to forward message to %s channel", label)
        return False


async def send_fallback_alert(message):
    """Send a text alert when the offending message is deleted and cannot be forwarded."""
    channel = bot.get_channel(config["mod_channel_id"])
    if not channel:
        return False
    try:
        await channel.send(
            f"{message.author} (id {message.author.id}) hit the strike limit in {message.channel.mention}, "
            "but their message was deleted before I could forward it."
        )
        return True
    except discord.HTTPException:
        log.exception("Failed to send fallback strike alert to Mod channel")
        return False


async def add_message_hashes(message):
    """Add every new image hash in message to the watch list. Returns how many hashes were added."""
    added = 0
    for attachment in message.attachments:
        img_hash = await hash_attachment(attachment)
        if img_hash is None or find_known_match(img_hash) is not None:
            continue
        known_hashes.append(img_hash)
        added += 1
    return added


async def handle_watch_hit(message, record, known_hash):
    """Alert the mod channel once per user per watched image."""
    key = str(known_hash)
    if key in record["hash_alerts"]:
        return
    record["hash_alerts"].append(key)
    if not await forward_to(message, config["mod_channel_id"], "Mod"):
        record["hash_alerts"].remove(key)  # forward failed; let the next repost retry


async def handle_strike_limit(message, record):
    if record["alerted"]:
        return
    record["alerted"] = True  # claim it now, before the sleep, so a concurrent duplicate can't slip through

    try:
        await asyncio.sleep(10)

        try:
            await message.guild.fetch_member(message.author.id)
        except discord.NotFound:
            record["alerted"] = False  # if they rejoin and spam again, we should still catch it
            return

        result = await forward_to(message, config["mod_channel_id"], "Mod")
        if result is None:
            result = await send_fallback_alert(message)  # message is gone; alert by user instead
        if not result:
            record["alerted"] = False
    except Exception:
        log.exception("Alert flow failed for %s; will retry on next qualifying message", message.author)
        record["alerted"] = False


@bot.event
async def on_message(message):
    if message.guild is None:
        return

    if message.author.bot:
        if message.channel.id == config["bot_log_channel_id"] and is_honeypot_ban_notice(message):
            await forward_to(message, config["ban_log_channel_id"], "Ban log")
        return

    record = None
    watch_hit = None
    for attachment in message.attachments:
        img_hash = await hash_attachment(attachment)
        if img_hash is None:
            continue

        record = track_hash(img_hash, str(message.author), message.author.id)
        if watch_hit is None:
            watch_hit = find_known_match(img_hash)

    if record:
        record["activity"].append({"time": message.created_at, "channel": message.channel.name})

    if watch_hit is not None:
        await handle_watch_hit(message, record, watch_hit)

    if record and record["strikes"] >= config["strike_limit"]:
        await handle_strike_limit(message, record)


if __name__ == "__main__":
    bot.run(TOKEN)
