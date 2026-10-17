import asyncio
import io
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import aiohttp
import discord
import imagehash
from discord.ext import tasks
from dotenv import load_dotenv
from PIL import Image

Image.MAX_IMAGE_PIXELS = 20_000_000  # reject huge decoded images; a small file can decode to a giant bitmap

load_dotenv()

TOKEN = os.environ.get("DISCORD_TOKEN", "")
GUILD_ID = int(os.environ.get("GUILD_ID") or 0)  # the one server this bot serves
CONFIG_PATH = os.environ.get("CONFIG_PATH", "config.json")  # settings and watched hashes; persists across restarts
LEGACY_HASHES_PATH = os.path.join(os.path.dirname(CONFIG_PATH), "hashes.json")  # old separate hash file; read once to migrate
BAN_GRACE_SECONDS = 10  # the moderation bot gets this long to ban a user before the strike alert goes out
THUMBNAIL_SIZE = 128  # phash shrinks every image to 32x32, so a small copy gives the same hash

CHANNEL_KEYS = ("mod_channel_id", "bot_log_channel_id", "ban_log_channel_id")

DEFAULT_CONFIG = {
    "sweep_interval_minutes": 10,  # how long a tracked entry survives; also the cleanup sweep interval, both adjustable via slash commands
    "strike_limit": 12,
    "hash_threshold": 5,
    "mod_channel_id": 0,
    "bot_log_channel_id": 0,
    "ban_log_channel_id": 0,
}

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("image-watch")


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
        log.warning("Ignoring %s=%r: not a channel id", env_var, raw)
        return 0


def parse_hashes(values):
    hashes = []
    for value in values:
        if not isinstance(value, str) or len(value) != 16:  # phash is 64 bits -> 16 hex chars; other shapes crash the compare
            log.warning("Skipping malformed saved hash: %r", value)
            continue
        try:
            hashes.append(imagehash.hex_to_hash(value))
        except ValueError:
            log.warning("Skipping malformed saved hash: %r", value)
    return hashes


def load_state(config_path, legacy_hashes_path):
    """Return (config, watched hashes) from config_path.
    The first run of this version migrates the old layout: hashes in a separate file, channel ids in the environment."""
    data = load_json(config_path, {})
    saved_hashes = data.pop("watched_hashes", None)
    migrating = saved_hashes is None
    if migrating:
        saved_hashes = load_json(legacy_hashes_path, [])
        for key in CHANNEL_KEYS:
            data[key] = data.get(key) or parse_channel_id(key.upper())
    config = {**DEFAULT_CONFIG, **data}
    for key, minimum in (("sweep_interval_minutes", 1), ("strike_limit", 1), ("hash_threshold", 0)):
        if not isinstance(config[key], int) or config[key] < minimum:
            log.warning("Ignoring %s=%r: must be an integer of at least %d", key, config[key], minimum)
            config[key] = DEFAULT_CONFIG[key]
    hashes = parse_hashes(saved_hashes if isinstance(saved_hashes, list) else [])
    if migrating:
        save_state(config_path, config, hashes)
        log.info("Saved settings and %d watched hash(es) to %s", len(hashes), config_path)
    return config, hashes


def save_state(path, config, hashes):
    save_json(path, {**config, "watched_hashes": [str(h) for h in hashes]})


config = dict(DEFAULT_CONFIG)  # main() fills this from disk
known_hashes = []  # images a mod marked with /hash-image; a repost of any of them alerts the mod channel


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


@dataclass
class UserRecord:
    """One user's image history inside the sweep window."""

    username: str
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    hashes: list = field(default_factory=list)
    strikes: int = 0
    alerted: bool = False
    activity: list = field(default_factory=list)  # one (channel name, time) pair per message
    hash_alerts: set = field(default_factory=set)  # watched hashes already reported for this user


tracked_users = {}  # user_id -> UserRecord


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


def thumbnail_url(attachment):
    """Return a media proxy URL that asks Discord for a small copy of the image. The aspect ratio stays the same."""
    scale = min(1, THUMBNAIL_SIZE / max(attachment.width, attachment.height))
    size = [("width", max(1, round(attachment.width * scale))), ("height", max(1, round(attachment.height * scale)))]
    parts = urlsplit(attachment.proxy_url)
    return urlunsplit(parts._replace(query=urlencode(parse_qsl(parts.query) + size)))


async def fetch_thumbnail(attachment):
    # ponytail: one HTTP session per image; share one session if image volume gets high
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
        async with session.get(thumbnail_url(attachment)) as response:
            response.raise_for_status()
            return await response.read()


async def hash_attachment(attachment):
    """Return the image hash of an image attachment, or None if it is not an image or cannot be hashed."""
    if not (attachment.content_type or "").startswith("image/") or not attachment.width or not attachment.height:
        return None
    try:
        data = await fetch_thumbnail(attachment)
        return await asyncio.to_thread(hash_image_bytes, data)  # hashing is CPU-bound; keep it off the event loop
    except Exception as e:
        log.warning("Could not hash attachment %s: %s", attachment.filename, e)
        return None


def track_hash(img_hash, username, user_id):
    """Record img_hash against user_id. Strikes only when this user has posted a fuzzy-matching image before."""
    record = tracked_users.setdefault(user_id, UserRecord(username))
    record.username = username
    if any(is_match(img_hash, seen, config["hash_threshold"]) for seen in record.hashes):
        record.strikes += 1
    else:
        record.hashes.append(img_hash)
    return record


def purge_stale(tracked, ttl_minutes, now):
    cutoff = now - timedelta(minutes=ttl_minutes)
    stale_keys = [key for key, record in tracked.items() if record.created_at < cutoff]
    return [tracked.pop(key) for key in stale_keys]


def format_activity_report(record):
    """Return a user's activity as one 'channel : time' line per entry."""
    lines = [f"{channel} : {time.strftime('%Y-%m-%d %H:%M:%S')}" for channel, time in record.activity]
    return "\n".join([f"{record.username}:"] + lines)


async def report_expired_activity(records):
    """Log each removed user's activity to the bot log channel when they met the strike limit."""
    channel = bot.get_channel(config["bot_log_channel_id"])
    if not channel:
        return
    for record in records:
        if record.strikes < config["strike_limit"]:
            continue
        try:
            await channel.send(format_activity_report(record)[:2000])
        except discord.HTTPException:
            log.exception("Failed to send activity report to bot log channel")


@tasks.loop(minutes=DEFAULT_CONFIG["sweep_interval_minutes"])  # main() sets the saved interval
async def cleanup_tracked_hashes():
    removed = purge_stale(tracked_users, config["sweep_interval_minutes"], datetime.now(timezone.utc))
    if not removed:
        return
    log.info("Cleaned up %d stale tracked user(s)", len(removed))
    await report_expired_activity(removed)


@bot.event
async def setup_hook():
    guild = discord.Object(GUILD_ID)
    tree.copy_global_to(guild=guild)
    tree.clear_commands(guild=None)
    await tree.sync()  # removes the global commands that older versions registered
    await tree.sync(guild=guild)  # server commands update at once; global ones can take an hour


@bot.event
async def on_ready():
    if not any(isinstance(h, DiscordLogHandler) for h in log.handlers):
        handler = DiscordLogHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        log.addHandler(handler)
    if not cleanup_tracked_hashes.is_running():
        cleanup_tracked_hashes.start()
    if bot.get_guild(GUILD_ID) is None:
        log.error("I am not in server %s. Check GUILD_ID. I stay in all servers until it is correct.", GUILD_ID)
    else:
        for guild in bot.guilds:
            await leave_if_foreign(guild)
    log.info("Logged in as %s", bot.user)


async def leave_if_foreign(guild):
    if guild.id == GUILD_ID:
        return
    log.warning("Leaving server %s (%s): this bot serves one server only", guild.name, guild.id)
    await guild.leave()


@bot.event
async def on_guild_join(guild):
    await leave_if_foreign(guild)


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
    save_state(CONFIG_PATH, config, known_hashes)
    await interaction.response.send_message(f"{kind.capitalize()} threshold set to {threshold}.", ephemeral=True)


@tree.command(name="set-sweep-interval", description="Set how often (in minutes) stale tracked users are purged")
@discord.app_commands.default_permissions(manage_guild=True)
async def set_sweep_interval_command(interaction: discord.Interaction, minutes: discord.app_commands.Range[int, 1]):
    config["sweep_interval_minutes"] = minutes
    save_state(CONFIG_PATH, config, known_hashes)
    cleanup_tracked_hashes.change_interval(minutes=minutes)
    await interaction.response.send_message(f"Sweep interval set to {minutes} minute(s).", ephemeral=True)


@tree.command(name="set-channel", description="Set the mod, bot log, or ban log channel")
@discord.app_commands.default_permissions(manage_guild=True)
async def set_channel_command(
    interaction: discord.Interaction, kind: Literal["mod", "bot_log", "ban_log"], channel: discord.TextChannel
):
    config[f"{kind}_channel_id"] = channel.id
    save_state(CONFIG_PATH, config, known_hashes)
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
    save_state(CONFIG_PATH, config, known_hashes)
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
        record.hash_alerts.clear()
    save_state(CONFIG_PATH, config, known_hashes)
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
    if key in record.hash_alerts:
        return
    record.hash_alerts.add(key)
    if not await forward_to(message, config["mod_channel_id"], "Mod"):
        record.hash_alerts.discard(key)  # forward failed; let the next repost retry


async def handle_strike_limit(message, record):
    if record.alerted:
        return
    record.alerted = True  # claim it now, before the sleep, so a concurrent duplicate can't slip through

    try:
        await asyncio.sleep(BAN_GRACE_SECONDS)

        try:
            await message.guild.fetch_member(message.author.id)
        except discord.NotFound:
            record.alerted = False  # if they rejoin and spam again, we should still catch it
            return

        result = await forward_to(message, config["mod_channel_id"], "Mod")
        if result is None:
            result = await send_fallback_alert(message)  # message is gone; alert by user instead
        if not result:
            record.alerted = False
    except Exception:
        log.exception("Alert flow failed for %s; will retry on next qualifying message", message.author)
        record.alerted = False


@bot.event
async def on_message(message):
    if message.guild is None or message.guild.id != GUILD_ID:
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
        record.activity.append((message.channel.name, message.created_at))

    if watch_hit is not None:
        await handle_watch_hit(message, record, watch_hit)

    if record and record.strikes >= config["strike_limit"]:
        await handle_strike_limit(message, record)


def main():
    if not GUILD_ID:
        raise SystemExit("Set GUILD_ID to the id of the server this bot serves.")
    loaded_config, loaded_hashes = load_state(CONFIG_PATH, LEGACY_HASHES_PATH)
    config.update(loaded_config)
    known_hashes[:] = loaded_hashes
    cleanup_tracked_hashes.change_interval(minutes=config["sweep_interval_minutes"])
    bot.run(TOKEN)


if __name__ == "__main__":
    main()
