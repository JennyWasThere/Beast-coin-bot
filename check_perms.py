import os

import discord
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.environ.get("DISCORD_TOKEN", "")
ALERT_CHANNEL_ID = int(os.environ.get("ALERT_CHANNEL_ID", "0"))

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)


@client.event
async def on_ready():
    print(f"Logged in as {client.user}")
    channel = client.get_channel(ALERT_CHANNEL_ID)
    if channel is None:
        print(f"Could not find channel {ALERT_CHANNEL_ID} in this bot's cache.")
        print("Guilds this bot is in:")
        for g in client.guilds:
            print(f"  - {g.name} ({g.id})")
    else:
        print(f"Found channel: #{channel.name} (id={channel.id}, type={channel.type}) in guild '{channel.guild.name}'")
        me = channel.guild.me
        perms = channel.permissions_for(me)
        print(f"Bot's top role: {me.top_role.name} (position {me.top_role.position})")
        print("Roles the bot has:", [r.name for r in me.roles])
        print("--- Effective permissions in this channel ---")
        print(f"view_channel:       {perms.view_channel}")
        print(f"send_messages:      {perms.send_messages}")
        print(f"send_messages_in_threads: {perms.send_messages_in_threads}")
        print(f"read_message_history: {perms.read_message_history}")
        print(f"embed_links:        {perms.embed_links}")
        print(f"attach_files:       {perms.attach_files}")
        if channel.category:
            print(f"Parent category: {channel.category.name} ({channel.category.id})")

    await client.close()


client.run(TOKEN)
