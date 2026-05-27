# 🎵 Homies Music Bot

A Discord music bot built with discord.py, wavelink, and Lavalink. Plays music from YouTube directly in your voice channels with a clean player UI, queue management, and EQ presets.

---

## Features

- Play music from YouTube by name or URL
- Queue system with shuffle and loop
- Previous / Pause / Skip / Stop buttons on the player embed
- EQ presets — Flat, Bass Boost, Treble Boost, Loud
- Auto-disconnect after inactivity
- Slash commands only

---

## Requirements

- Python 3.10+
- Java 17+ (for Lavalink)
- A Discord bot token
- A Discord server (guild)

---

## Setup

### 1. Clone the repo

```bash
git clone https://github.com/yourusername/homies_music.git
cd homies_music
```

### 2. Install Python dependencies

```bash
pip install -r requirements.txt
```

### 3. Download Lavalink

Download the latest `Lavalink.jar` from the [Lavalink releases page](https://github.com/lavalink-devs/Lavalink/releases/latest) and place it inside the `lavalink/` folder.

### 4. Configure environment variables

Copy the example env file and fill in your values:

```bash
cp .env.example .env
```

Open `.env` and set:

```env
DISCORD_TOKEN=your_discord_bot_token
GUILD_ID=your_discord_server_id
LAVALINK_HOST=localhost
LAVALINK_PORT=2333
LAVALINK_PASSWORD=your_lavalink_password
```

> Make sure the `LAVALINK_PASSWORD` in `.env` matches the `password` field in `lavalink/application.yml`.

### 5. Set up YouTube OAuth (required for playback)

Lavalink needs a YouTube OAuth token to play music. On first run, it will print a code in the Lavalink console window:

```
Go to https://www.google.com/device and enter code: XXXX-XXXX
```

Open that link, sign in with a **burner Google account** (not your main), and enter the code. Once authorized, copy the refresh token printed in the console and paste it into `lavalink/application.yml` under:

```yaml
plugins:
  youtube:
    oauth:
      refreshToken: "paste your token here"
      skipInitialization: true
```

You only need to do this once.

### 6. Run the bot

Just double-click `start.bat` or run it from the terminal:

```bash
start.bat
```

This starts Lavalink first, waits for it to be ready, then starts the bot.

---

## Slash Commands

| Command | Description |
|---|---|
| `/play <query>` | Play a song by name or YouTube URL |
| `/skip` | Skip the current song |
| `/queue` | Show the current queue |
| `/pause` | Pause playback |
| `/resume` | Resume playback |
| `/stop` | Stop and clear the queue |
| `/leave` | Disconnect the bot from voice |
| `/nowplaying` | Show what's currently playing |
| `/eq` | Apply an EQ preset |

---

## Project Structure

```
homies_music/
├── bot.py                  # Bot entry point
├── cogs/
│   └── music.py            # All music logic and commands
├── lavalink/
│   ├── Lavalink.jar        # Download separately (see setup)
│   └── application.yml     # Lavalink config
├── .env                    # Your secrets (not committed)
├── .env.example            # Template for .env
├── requirements.txt
└── start.bat               # Starts everything
```

---

## Notes

- The Lavalink jar is not included in the repo. Download it from the link above.
- Never commit your `.env` file — it contains your bot token.
- Use a throwaway Google account for the YouTube OAuth token, not your personal one.
- The bot uses `ANDROID_VR`, `WEBEMBEDDED`, and `TV` clients for YouTube playback to avoid YouTube's bot detection.