# Beast Coin Bot

A Discord bot that watches for image spam. When someone posts the same (or a
near-identical) picture over and over, the bot quietly keeps count, and once
they've done it too many times it forwards one of their messages to a mod
alert channel so a human can take a look.

It doesn't delete anything or ban anyone automatically, it just flags
repeat offenders for review.

## How it works

- Every image posted in a server is fingerprinted (a "perceptual hash") so
  visually similar images are recognized even if they've been resized or
  slightly edited.
- If the same person posts a matching image again, that counts as a strike.
- Once a user racks up enough strikes, their message gets forwarded to a
  designated alert channel.
- Strike history is temporary, a user's record resets automatically after a
  set amount of time (the "sweep interval").

## Commands

- `/about`: link to this repo.
- `/purge`: manually clear out stale tracked users right now.
- `/set-strike-threshold`: how many matching posts before an alert fires.
- `/set-hash-threshold`: how similar two images must be to count as a match.
- `/set-sweep-interval`: how long (in minutes) a user's history is kept.
- `/set-alert-channel`: where flagged messages get forwarded.
- `/set-log-channel`: where the bot's internal logs get mirrored.

The `set-*` and `purge` commands require "Manage Server" permission.

## Setup

1. Copy `.env.example` to `.env` and fill in `DISCORD_TOKEN` (and optionally
   `ALERT_CHANNEL_ID` / `LOG_CHANNEL_ID` as defaults).
2. `pip install -r requirements.txt`
3. `python bot.py`

Settings changed via slash commands are saved to `config.json` and survive
restarts.
