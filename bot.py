import asyncio
import os
import discord
import wavelink
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

TOKEN    = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID"))

LAVALINK_HOST     = os.getenv("LAVALINK_HOST", "localhost")
LAVALINK_PORT     = int(os.getenv("LAVALINK_PORT", "2333"))
LAVALINK_PASSWORD = os.getenv("LAVALINK_PASSWORD", "homies_music_pass")

intents = discord.Intents.default()
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)


async def setup_hook():
    await bot.load_extension("cogs.music")

    node = wavelink.Node(
        uri=f"http://{LAVALINK_HOST}:{LAVALINK_PORT}",
        password=LAVALINK_PASSWORD,
    )
    await wavelink.Pool.connect(nodes=[node], client=bot, cache_capacity=100)

    guild = discord.Object(id=GUILD_ID)
    bot.tree.copy_global_to(guild=guild)
    await bot.tree.sync(guild=guild)
    print(f"Commands synced to guild {GUILD_ID}")


bot.setup_hook = setup_hook


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")


@bot.event
async def on_wavelink_node_ready(payload: wavelink.NodeReadyEventPayload):
    print(f"Lavalink node ready: {payload.node!r}  |  resumed={payload.resumed}")


if __name__ == "__main__":
    asyncio.run(bot.start(TOKEN))
