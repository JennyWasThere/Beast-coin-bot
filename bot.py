import asyncio
import io
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
ALERT_CHANNEL_ID = int(os.environ.get("ALERT_CHANNEL_ID") or "0")
LOG_CHANNEL_ID = int(os.environ.get("LOG_CHANNEL_ID") or "0")
HASH_THRESHOLD = int(os.environ.get("HASH_THRESHOLD", "5"))
TRACK_TTL_MINUTES = 10  # how long a tracked entry survives; also the cleanup sweep interval
STRIKE_LIMIT = 12

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("image-watch")


class DiscordLogHandler(logging.Handler):
    """Mirrors log records to a Discord channel. emit() is sync, so sending is scheduled as a task."""

    def __init__(self, bot, channel_id):
        super().__init__()
        self.bot = bot
        self.channel_id = channel_id

    def emit(self, record):
        channel = self.bot.get_channel(self.channel_id)
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

tracked_users = {}  # user_id -> {username, hashes: [imagehash,...], strikes, last_seen}


def is_match(hash1, hash2, threshold=HASH_THRESHOLD):
    return (hash1 - hash2) <= threshold


def track_hash(img_hash, username, user_id, tracked=None):
    """Record img_hash against user_id. Strikes only when this user has posted a fuzzy-matching image before."""
    tracked = tracked_users if tracked is None else tracked
    record = tracked.get(user_id)
    if record is None:
        record = {"username": username, "hashes": [], "strikes": 0, "last_seen": None, "alerted": False}
        tracked[user_id] = record
    record["username"] = username
    record["last_seen"] = datetime.now(timezone.utc)
    if any(is_match(img_hash, seen) for seen in record["hashes"]):
        record["strikes"] += 1
    else:
        record["hashes"].append(img_hash)
    return record


def purge_stale(tracked, ttl_minutes, now):
    cutoff = now - timedelta(minutes=ttl_minutes)
    stale_keys = [key for key, record in tracked.items() if record["last_seen"] < cutoff]
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
    if LOG_CHANNEL_ID and not any(isinstance(h, DiscordLogHandler) for h in log.handlers):
        handler = DiscordLogHandler(bot, LOG_CHANNEL_ID)
        handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        log.addHandler(handler)
    if not cleanup_tracked_hashes.is_running():
        cleanup_tracked_hashes.start()
    log.info("Logged in as %s", bot.user)


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
