import asyncio
import io
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord
import imagehash
from PIL import Image

import bot
from bot import UserRecord, find_known_match, format_activity_report, is_match, parse_message_link, purge_stale, track_hash


def test_is_match():
    a = imagehash.hex_to_hash("ffff0000ffff0000")
    b = imagehash.hex_to_hash("ffff0000ffff0001")  # 1 bit different -> fuzzy match
    c = imagehash.hex_to_hash("0000000000000000")  # far away -> no match

    assert is_match(a, b, threshold=5)
    assert not is_match(a, c, threshold=5)


@patch.object(bot, "tracked_users", {})
def test_track_hash():
    tracked = bot.tracked_users
    a = imagehash.hex_to_hash("ffff0000ffff0000")
    b = imagehash.hex_to_hash("ffff0000ffff0001")  # fuzzy-matches a
    c = imagehash.hex_to_hash("0000000000000000")  # far from a/b -> distinct image

    track_hash(a, "spammer", 123)
    assert tracked[123].strikes == 0
    assert len(tracked[123].hashes) == 1

    track_hash(b, "spammer", 123)  # same user reposts a fuzzy-matching image
    assert tracked[123].strikes == 1
    assert len(tracked[123].hashes) == 1  # repeat, not a new entry in their list

    track_hash(c, "spammer", 123)  # same user, but a genuinely new image
    assert tracked[123].strikes == 1  # no strike for a first sighting
    assert len(tracked[123].hashes) == 2

    track_hash(a, "someone_else", 456)  # different user, first time seeing this image
    assert tracked[456].strikes == 0


@patch.object(bot, "tracked_users", {})
def test_repeated_image_set_accumulates_strikes():
    # simulates the actual spam pattern: one user, same 4 images, sent over and over
    tracked = bot.tracked_users
    images = [imagehash.hex_to_hash(h) for h in ("ffff0000ffff0000", "0000ffff0000ffff", "ff00ff00ff00ff00", "00ff00ff00ff00ff")]

    def send_message(imgs):
        for img in imgs:
            track_hash(img, "spammer", 123)

    send_message(images)  # first time posting these 4 -> all new, no strikes
    assert tracked[123].strikes == 0

    send_message(images)  # same 4 again -> 4 strikes
    assert tracked[123].strikes == 4

    send_message(images)  # and again -> keeps accumulating
    assert tracked[123].strikes == 8


@patch.object(bot, "tracked_users", {})
def test_track_hash_does_not_record_activity():
    tracked = bot.tracked_users
    a = imagehash.hex_to_hash("ffff0000ffff0000")
    b = imagehash.hex_to_hash("0000ffff0000ffff")

    track_hash(a, "spammer", 123)
    track_hash(b, "spammer", 123)

    assert tracked[123].activity == []


def test_purge_stale():
    now = datetime.now(timezone.utc)
    stale = UserRecord("old", created_at=now - timedelta(minutes=11))
    tracked = {123: stale, 456: UserRecord("new", created_at=now - timedelta(minutes=1))}

    removed = purge_stale(tracked, ttl_minutes=10, now=now)

    assert removed == [stale]
    assert list(tracked) == [456]


def test_format_activity_report():
    record = UserRecord("spammer", activity=[
        ("memes", datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)),
        ("general", datetime(2024, 1, 1, 12, 5, tzinfo=timezone.utc)),
    ])

    report = format_activity_report(record)

    assert report == "spammer:\nmemes : 2024-01-01 12:00:00\ngeneral : 2024-01-01 12:05:00"


def test_parse_message_link():
    assert parse_message_link("https://discord.com/channels/111/222/333") == (222, 333)
    assert parse_message_link("https://discord.com/channels/@me/222/333") == (222, 333)
    assert parse_message_link("https://discord.com/channels/111/222/333/") == (222, 333)
    assert parse_message_link("12345") is None
    assert parse_message_link("not a link") is None


def test_find_known_match():
    watched = imagehash.hex_to_hash("ffff0000ffff0000")
    near = imagehash.hex_to_hash("ffff0000ffff0001")  # 1 bit different -> match
    far = imagehash.hex_to_hash("0000000000000000")  # far away -> no match

    with patch.object(bot, "known_hashes", [watched]), patch.dict(bot.config, hash_threshold=5):
        assert find_known_match(near) == watched
        assert find_known_match(far) is None


def _state_paths():
    folder = tempfile.mkdtemp()
    return os.path.join(folder, "config.json"), os.path.join(folder, "hashes.json")


def test_state_round_trip():
    config_path, hashes_path = _state_paths()
    h = imagehash.hex_to_hash("ffff0000ffff0000")
    bot.save_state(config_path, {**bot.DEFAULT_CONFIG, "strike_limit": 3}, [h])
    cfg, hashes = bot.load_state(config_path, hashes_path)
    assert cfg["strike_limit"] == 3
    assert hashes == [h]


def test_load_state_migrates_old_layout_once():
    config_path, hashes_path = _state_paths()
    with open(config_path, "w") as f:
        json.dump({"strike_limit": 4, "mod_channel_id": 0}, f)
    with open(hashes_path, "w") as f:
        json.dump(["ffff0000ffff0000", None, 123, "ffff"], f)  # malformed entries are skipped

    with patch.dict(os.environ, MOD_CHANNEL_ID="111"):
        cfg, hashes = bot.load_state(config_path, hashes_path)
    assert cfg["strike_limit"] == 4
    assert cfg["mod_channel_id"] == 111  # env seeds a channel the old config file lacked
    assert hashes == [imagehash.hex_to_hash("ffff0000ffff0000")]
    with open(config_path) as f:
        assert json.load(f)["watched_hashes"] == ["ffff0000ffff0000"]

    os.remove(hashes_path)
    with patch.dict(os.environ, MOD_CHANNEL_ID="222"):
        cfg, hashes = bot.load_state(config_path, hashes_path)
    assert cfg["mod_channel_id"] == 111  # after migration the environment is ignored
    assert len(hashes) == 1  # the old hash file is no longer needed


def test_load_state_survives_bad_values():
    config_path, hashes_path = _state_paths()
    with open(config_path, "w") as f:
        json.dump({"sweep_interval_minutes": -5}, f)
    with patch.dict(os.environ, MOD_CHANNEL_ID="#general"):
        cfg, _ = bot.load_state(config_path, hashes_path)
    assert cfg["mod_channel_id"] == 0
    assert cfg["sweep_interval_minutes"] == bot.DEFAULT_CONFIG["sweep_interval_minutes"]


def test_handle_watch_hit_alerts_once():
    forward = AsyncMock(return_value=True)
    record = UserRecord("spammer")
    watched = imagehash.hex_to_hash("ffff0000ffff0000")
    with patch.object(bot, "forward_to", forward):
        asyncio.run(bot.handle_watch_hit("post", record, watched))
        asyncio.run(bot.handle_watch_hit("post", record, watched))  # same user, same image
    forward.assert_awaited_once()
    assert record.hash_alerts == {str(watched)}


def test_handle_watch_hit_retries_after_forward_failure():
    record = UserRecord("spammer")
    with patch.object(bot, "forward_to", AsyncMock(return_value=False)):
        asyncio.run(bot.handle_watch_hit("post", record, imagehash.hex_to_hash("ffff0000ffff0000")))
    assert record.hash_alerts == set()  # not claimed, so the next repost tries again


def _attachment(data):
    return SimpleNamespace(content_type="image/png", filename="image.png", width=16, height=16, data=data)


def test_thumbnail_url_keeps_signature_and_aspect_ratio():
    attachment = SimpleNamespace(width=1000, height=500, proxy_url="https://media.discordapp.net/a/b.png?ex=1&hm=2")
    assert bot.thumbnail_url(attachment) == "https://media.discordapp.net/a/b.png?ex=1&hm=2&width=128&height=64"

    small = SimpleNamespace(width=40, height=20, proxy_url="https://media.discordapp.net/a/b.png")
    assert bot.thumbnail_url(small) == "https://media.discordapp.net/a/b.png?width=40&height=20"  # never upscale


def test_add_message_hashes():
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), "red").save(buf, format="PNG")
    png = buf.getvalue()

    with patch.object(bot, "known_hashes", []), \
            patch.object(bot, "fetch_thumbnail", AsyncMock(side_effect=lambda a: a.data)):
        message = SimpleNamespace(attachments=[_attachment(png), _attachment(b"not an image")])
        assert asyncio.run(bot.add_message_hashes(message)) == 1
        assert len(bot.known_hashes) == 1

        same = SimpleNamespace(attachments=[_attachment(png)])
        assert asyncio.run(bot.add_message_hashes(same)) == 0  # already watched


def test_forward_to_returns_none_when_message_deleted():
    deleted = discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown Message")
    message = SimpleNamespace(forward=AsyncMock(side_effect=deleted))
    with patch.object(bot.bot, "get_channel", return_value=object()):
        assert asyncio.run(bot.forward_to(message, 999, "Mod")) is None


def test_handle_strike_limit_alerts_by_user_when_message_gone():
    message = SimpleNamespace(author=SimpleNamespace(id=123), guild=SimpleNamespace(fetch_member=AsyncMock()))
    fallback = AsyncMock(return_value=True)
    record = UserRecord("spammer")
    with patch.object(bot, "forward_to", AsyncMock(return_value=None)), \
            patch.object(bot, "send_fallback_alert", fallback), patch.object(bot.asyncio, "sleep", AsyncMock()):
        asyncio.run(bot.handle_strike_limit(message, record))

    fallback.assert_awaited_once_with(message)
    assert record.alerted is True


def test_hash_image_rejects_other_guild():
    link = "https://discord.com/channels/111/222/333"
    message = SimpleNamespace(attachments=[], guild=SimpleNamespace(id=999))
    channel = SimpleNamespace(fetch_message=AsyncMock(return_value=message))
    interaction = SimpleNamespace(guild_id=111, response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))
    before = list(bot.known_hashes)

    with patch.object(bot.bot, "get_channel", return_value=channel):
        asyncio.run(bot.hash_image_command.callback(interaction, link))

    interaction.followup.send.assert_awaited_once_with("That link is from another server.", ephemeral=True)
    assert bot.known_hashes == before


def test_leaves_only_other_servers():
    home = SimpleNamespace(id=1, name="home", leave=AsyncMock())
    other = SimpleNamespace(id=2, name="other", leave=AsyncMock())
    with patch.object(bot, "GUILD_ID", 1):
        asyncio.run(bot.on_guild_join(home))
        asyncio.run(bot.on_guild_join(other))
    home.leave.assert_not_awaited()
    other.leave.assert_awaited_once()


if __name__ == "__main__":
    for name, test in list(globals().items()):
        if name.startswith("test_"):
            test()
    print("ok")
