from datetime import datetime, timedelta, timezone

import imagehash

from bot import format_activity_report, is_match, purge_stale, track_hash


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


if __name__ == "__main__":
    test_is_match()
    test_track_hash()
    test_repeated_image_set_accumulates_strikes()
    test_track_hash_does_not_record_activity()
    test_purge_stale()
    test_format_activity_report()
    print("ok")
