import asyncio
import io
import os
import tempfile
import types
from datetime import datetime, timedelta, timezone

import imagehash
from PIL import Image

import bot
from bot import find_known_match, format_activity_report, is_match, parse_message_link, purge_stale, track_hash


def test_is_match():
    a = imagehash.hex_to_hash("ffff0000ffff0000")
    b = imagehash.hex_to_hash("ffff0000ffff0001")  # 1 bit different -> fuzzy match
    c = imagehash.hex_to_hash("0000000000000000")  # far away -> no match

    assert is_match(a, b, threshold=5)
    assert not is_match(a, c, threshold=5)


def test_track_hash():
    tracked = {}
    a = imagehash.hex_to_hash("ffff0000ffff0000")
    b = imagehash.hex_to_hash("ffff0000ffff0001")  # fuzzy-matches a
    c = imagehash.hex_to_hash("0000000000000000")  # far from a/b -> distinct image

    track_hash(a, "spammer", 123, tracked)
    assert tracked[123]["strikes"] == 0
    assert len(tracked[123]["hashes"]) == 1

    track_hash(b, "spammer", 123, tracked)  # same user reposts a fuzzy-matching image
    assert tracked[123]["strikes"] == 1
    assert len(tracked[123]["hashes"]) == 1  # repeat, not a new entry in their list

    track_hash(c, "spammer", 123, tracked)  # same user, but a genuinely new image
    assert tracked[123]["strikes"] == 1  # no strike for a first sighting
    assert len(tracked[123]["hashes"]) == 2

    track_hash(a, "someone_else", 456, tracked)  # different user, first time seeing this image
    assert tracked[456]["strikes"] == 0


def test_repeated_image_set_accumulates_strikes():
    # simulates the actual spam pattern: one user, same 4 images, sent over and over
    tracked = {}
    images = [imagehash.hex_to_hash(h) for h in ("ffff0000ffff0000", "0000ffff0000ffff", "ff00ff00ff00ff00", "00ff00ff00ff00ff")]

    def send_message(imgs):
        for img in imgs:
            track_hash(img, "spammer", 123, tracked)

    send_message(images)  # first time posting these 4 -> all new, no strikes
    assert tracked[123]["strikes"] == 0

    send_message(images)  # same 4 again -> 4 strikes
    assert tracked[123]["strikes"] == 4

    send_message(images)  # and again -> keeps accumulating
    assert tracked[123]["strikes"] == 8


def test_track_hash_does_not_record_activity():
    tracked = {}
    a = imagehash.hex_to_hash("ffff0000ffff0000")
    b = imagehash.hex_to_hash("0000ffff0000ffff")

    track_hash(a, "spammer", 123, tracked)
    track_hash(b, "spammer", 123, tracked)

    assert tracked[123]["activity"] == []


def test_purge_stale():
    now = datetime.now(timezone.utc)
    tracked = {
        123: {"created_at": now - timedelta(minutes=11)},
        456: {"created_at": now - timedelta(minutes=1)},
    }

    removed = purge_stale(tracked, ttl_minutes=10, now=now)

    assert len(removed) == 1
    assert removed[0][0] == 123
    assert list(tracked) == [456]


def test_format_activity_report():
    record = {
        "username": "spammer",
        "activity": [
            {"time": datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc), "channel": "memes"},
            {"time": datetime(2024, 1, 1, 12, 5, tzinfo=timezone.utc), "channel": "general"},
        ],
    }

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

    old_hashes = list(bot.known_hashes)
    old_threshold = bot.config["hash_threshold"]
    bot.known_hashes.clear()
    bot.known_hashes.append(watched)
    bot.config["hash_threshold"] = 5
    try:
        assert find_known_match(near) == watched
        assert find_known_match(far) is None
    finally:
        bot.config["hash_threshold"] = old_threshold
        bot.known_hashes.clear()
        bot.known_hashes.extend(old_hashes)


def test_hash_round_trip():
    old_path = bot.HASHES_PATH
    old_hashes = list(bot.known_hashes)
    bot.HASHES_PATH = os.path.join(tempfile.mkdtemp(), "hashes.json")
    try:
        bot.known_hashes.clear()
        bot.known_hashes.append(imagehash.hex_to_hash("ffff0000ffff0000"))
        bot.save_hashes()

        bot.known_hashes.clear()
        assert bot.load_hashes() == [imagehash.hex_to_hash("ffff0000ffff0000")]
    finally:
        bot.HASHES_PATH = old_path
        bot.known_hashes.clear()
        bot.known_hashes.extend(old_hashes)


def test_handle_watch_hit_alerts_once():
    calls = []

    async def fake_forward(message, channel_id, label):
        calls.append(message)
        return True

    old_forward = bot.forward_to
    bot.forward_to = fake_forward
    try:
        record = {"hash_alerts": []}
        watched = imagehash.hex_to_hash("ffff0000ffff0000")
        asyncio.run(bot.handle_watch_hit("post", record, watched))
        asyncio.run(bot.handle_watch_hit("post", record, watched))  # same user, same image
        assert calls == ["post"]
        assert record["hash_alerts"] == [str(watched)]
    finally:
        bot.forward_to = old_forward


def test_handle_watch_hit_retries_after_forward_failure():
    async def failing_forward(message, channel_id, label):
        return False

    old_forward = bot.forward_to
    bot.forward_to = failing_forward
    try:
        record = {"hash_alerts": []}
        asyncio.run(bot.handle_watch_hit("post", record, imagehash.hex_to_hash("ffff0000ffff0000")))
        assert record["hash_alerts"] == []  # not claimed, so the next repost tries again
    finally:
        bot.forward_to = old_forward


class _FakeAttachment:
    def __init__(self, data, content_type="image/png"):
        self._data = data
        self.content_type = content_type
        self.filename = "image.png"

    async def read(self):
        return self._data


class _FakeMessage:
    def __init__(self, attachments, guild_id=None):
        self.attachments = attachments
        self.guild = types.SimpleNamespace(id=guild_id) if guild_id is not None else None


def test_add_message_hashes():
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), "red").save(buf, format="PNG")
    png = buf.getvalue()

    old_hashes = list(bot.known_hashes)
    bot.known_hashes.clear()
    try:
        message = _FakeMessage([_FakeAttachment(png), _FakeAttachment(b"not an image")])
        assert asyncio.run(bot.add_message_hashes(message)) == 1
        assert len(bot.known_hashes) == 1

        same = _FakeMessage([_FakeAttachment(png)])
        assert asyncio.run(bot.add_message_hashes(same)) == 0  # already watched
    finally:
        bot.known_hashes.clear()
        bot.known_hashes.extend(old_hashes)


class _FakeResponse:
    async def defer(self, ephemeral=False):
        pass


class _FakeFollowup:
    def __init__(self):
        self.messages = []

    async def send(self, content, ephemeral=False):
        self.messages.append(content)


class _FakeInteraction:
    def __init__(self, guild_id):
        self.guild_id = guild_id
        self.response = _FakeResponse()
        self.followup = _FakeFollowup()


class _FakeChannel:
    def __init__(self, message):
        self._message = message

    async def fetch_message(self, message_id):
        return self._message


def test_hash_image_rejects_other_guild():
    link = "https://discord.com/channels/111/222/333"
    channel = _FakeChannel(_FakeMessage([], guild_id=999))
    interaction = _FakeInteraction(guild_id=111)
    before = list(bot.known_hashes)

    old_get_channel = bot.bot.get_channel
    bot.bot.get_channel = lambda channel_id: channel
    try:
        asyncio.run(bot.hash_image_command.callback(interaction, link))
    finally:
        bot.bot.get_channel = old_get_channel

    assert interaction.followup.messages == ["That link is from another server."]
    assert bot.known_hashes == before


if __name__ == "__main__":
    test_is_match()
    test_track_hash()
    test_repeated_image_set_accumulates_strikes()
    test_track_hash_does_not_record_activity()
    test_purge_stale()
    test_format_activity_report()
    test_parse_message_link()
    test_find_known_match()
    test_hash_round_trip()
    test_hash_image_rejects_other_guild()
    test_handle_watch_hit_alerts_once()
    test_handle_watch_hit_retries_after_forward_failure()
    test_add_message_hashes()
    print("ok")
