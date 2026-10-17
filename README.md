# Beast Coin Bot

The bot watches for image spam. A user can post the same picture, or a
near-identical one, many times. The bot counts these posts. After too many
repeats, the bot forwards one of the user's messages to a mod channel. A
human then reviews it.

The bot also watches for honeypot bans. When the moderation bot posts a ban
notice for a honeypot trigger, this bot forwards that notice to a ban log
channel.

The bot does not delete messages. The bot does not ban users. The bot only
flags repeat offenders and forwards ban notices for review.

## How it works

### Image spam detection

- The bot fingerprints every image posted in the server. It downloads a
  small copy of each image from Discord, not the full file. This fingerprint is
  a "perceptual hash." The hash matches visually similar images, even after
  a resize or a small edit.
- A repost of a matching image counts as a strike for that user.
- After a user reaches the strike limit, the bot forwards their message to
  the mod channel.
- Strike history is temporary. A user's record resets after a set time (the
  "sweep interval").

### Watched images

- A mod can point the bot at one known-bad post with `/hash-image` and a
  message link. The bot fingerprints each image in that post. It saves these
  fingerprints to disk.
- Any later repost of a watched image, in any channel, forwards that message
  to the mod channel. The bot alerts once per user per watched image, within
  the sweep window.
- Watched images survive restarts. `/clear-hashes` wipes them all.

### Honeypot ban forwarding

- The bot watches the bot log channel for moderation-bot messages.
- When a message embed contains the phrase "Triggered honeypot channel,"
  the bot forwards it to the ban log channel.

## Commands

- `/about`: link to this repo.
- `/purge`: clear all tracked users now.
- `/set-threshold kind:strike`: set how many matching posts trigger an alert.
- `/set-threshold kind:hash`: set how similar two images must be to count as a
  match.
- `/set-sweep-interval`: set how long (in minutes) the bot keeps a user's
  strike history.
- `/set-channel kind:mod`: set where flagged messages get forwarded.
- `/set-channel kind:bot_log`: set where the bot's internal logs get
  mirrored, and where the bot watches for honeypot ban notices.
- `/set-channel kind:ban_log`: set where honeypot ban notices get forwarded.
- `/hash-image`: add the images in a message from this server to the watch
  list.
- `/clear-hashes`: wipe every watched image hash.

The `set-*`, `hash-image`, `clear-hashes`, and `purge` commands require the
"Manage Server" permission.

## Setup

1. Copy `.env.example` to `.env`. Fill in `DISCORD_TOKEN` and `GUILD_ID`.
2. Run `pip install -r requirements.txt`.
3. Run `python bot.py`.
4. Run `/set-channel` for the mod, bot log, and ban log channels.

The bot serves one server: the server in `GUILD_ID`. The bot leaves any other
server that adds it. Slash commands register in that server only.

Settings and watched image hashes save to `config.json`. This file survives
restarts when it sits on a mounted volume.
