import asyncio
import hashlib
import json
import os
from pathlib import Path
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

PLAYER_EMOJI_FILES = {
    "play": "play.png",
    "previous": "previous.png",
    "pause": "pause.png",
    "next": "next.png",
    "like": "like.png",
}
PLAYER_EMOJI_CACHE = Path.home() / ".cache" / "homies_music" / "player_emoji_hashes.json"


async def _load_player_emojis():
    """Create the transparent purple emojis or replace them when assets change."""
    if getattr(bot, "_player_emojis_loaded", False):
        return

    guild = bot.get_guild(GUILD_ID)
    if guild is None:
        print(f"[music] Cannot load player icons: guild {GUILD_ID} is not cached.")
        return

    existing = {emoji.name: emoji for emoji in guild.emojis}
    player_emojis = {}
    try:
        synced_hashes = json.loads(PLAYER_EMOJI_CACHE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        synced_hashes = {}
    if not isinstance(synced_hashes, dict):
        synced_hashes = {}

    can_manage = bool(
        guild.me and guild.me.guild_permissions.manage_emojis_and_stickers
    )
    icon_dir = Path(__file__).resolve().parent / "assets" / "player_icons"
    cache_prefix = str(guild.id)
    warned_permission = False

    for key, filename in PLAYER_EMOJI_FILES.items():
        emoji_name = f"homies_{key}"
        emoji = existing.get(emoji_name)
        try:
            image = (icon_dir / filename).read_bytes()
        except OSError as exc:
            print(f"[music] Could not read player icon {filename}: {exc}")
            continue

        if len(image) >= 256 * 1024:
            print(f"[music] Player icon {filename} must be smaller than 256 KB.")
            continue

        content_hash = hashlib.sha256(image).hexdigest()
        cache_key = f"{cache_prefix}:{key}"
        image_changed = synced_hashes.get(cache_key) != content_hash
        if emoji is not None and image_changed:
            if not can_manage:
                if not warned_permission:
                    print(
                        "[music] Updated icon files cannot replace the server emojis. "
                        "Grant the bot Manage Expressions permission."
                    )
                    warned_permission = True
                player_emojis[key] = emoji
                continue
            try:
                await emoji.delete(reason="Replace outdated music player icon")
                emoji = None
            except discord.Forbidden:
                print(
                    "[music] Discord denied replacing a player emoji. Grant the bot "
                    "Manage Expressions permission."
                )
                player_emojis[key] = emoji
                continue
            except discord.HTTPException as exc:
                print(f"[music] Could not remove outdated player emoji {emoji_name}: {exc}")
                player_emojis[key] = emoji
                continue

        if emoji is None:
            if not can_manage:
                if not warned_permission:
                    print(
                        "[music] Missing custom player icons. Grant the bot "
                        "Manage Expressions permission to create them."
                    )
                    warned_permission = True
                continue
            try:
                emoji = await guild.create_custom_emoji(
                    name=emoji_name,
                    image=image,
                    reason="Create or refresh purple icons for the music player",
                )
            except discord.Forbidden:
                print(
                    "[music] Discord denied custom emoji creation. Grant the bot "
                    "Manage Expressions permission."
                )
                continue
            except discord.HTTPException as exc:
                print(f"[music] Could not create player emoji {emoji_name}: {exc}")
                continue
        player_emojis[key] = emoji
        synced_hashes[cache_key] = content_hash

    bot.player_emojis = player_emojis
    bot._player_emojis_loaded = True
    try:
        PLAYER_EMOJI_CACHE.parent.mkdir(parents=True, exist_ok=True)
        PLAYER_EMOJI_CACHE.write_text(
            json.dumps(synced_hashes, indent=2), encoding="utf-8"
        )
    except OSError as exc:
        print(f"[music] Could not save player icon sync state: {exc}")
    if len(player_emojis) == len(PLAYER_EMOJI_FILES):
        print("[music] Purple player icons are synced and ready.")


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
    await _load_player_emojis()
    print(f"Logged in as {bot.user}")


@bot.event
async def on_wavelink_node_ready(payload: wavelink.NodeReadyEventPayload):
    print(f"Lavalink node ready: {payload.node!r}  |  resumed={payload.resumed}")


if __name__ == "__main__":
    asyncio.run(bot.start(TOKEN))
