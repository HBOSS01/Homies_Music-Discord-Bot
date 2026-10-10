import asyncio
import discord
import wavelink
from discord import app_commands
from discord.ext import commands

# ── Constants ──────────────────────────────────────────────────────────────────
INACTIVITY_TIMEOUT = 180

# ── Color palette ──────────────────────────────────────────────────────────────
C_PURPLE = 0x8B5CF6
C_BLUE   = 0x5865F2
C_GREEN  = 0x57F287
C_YELLOW = 0xFEE75C
C_RED    = 0xED4245
C_GREY   = 0x95A5A6
C_DARK   = 0x2B2D31


# ── EQ filter presets ──────────────────────────────────────────────────────────
def _build_filters(preset: str) -> wavelink.Filters:
    f = wavelink.Filters()
    if preset == "flat":
        f.reset()
    elif preset == "bass_boost":
        f.equalizer.set(bands=[
            {"band": 0, "gain": 0.25}, {"band": 1, "gain": 0.20},
            {"band": 2, "gain": 0.15}, {"band": 3, "gain": 0.10},
            {"band": 4, "gain": 0.05},
        ])
    elif preset == "treble_boost":
        f.equalizer.set(bands=[
            {"band": 10, "gain": 0.10}, {"band": 11, "gain": 0.15},
            {"band": 12, "gain": 0.20}, {"band": 13, "gain": 0.25},
            {"band": 14, "gain": 0.20},
        ])
    elif preset == "loud":
        f.equalizer.set(bands=[
            {"band": 0,  "gain": 0.12}, {"band": 1,  "gain": 0.08},
            {"band": 2,  "gain": 0.04}, {"band": 10, "gain": 0.03},
            {"band": 11, "gain": 0.06}, {"band": 12, "gain": 0.08},
            {"band": 13, "gain": 0.10}, {"band": 14, "gain": 0.08},
        ])
    return f


# ── Utility helpers ────────────────────────────────────────────────────────────
def duration_fmt(ms: int | None) -> str:
    if not ms:
        return "0:00"
    s = ms // 1000
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


async def _safe_edit(msg: discord.Message | None, **kwargs):
    if msg:
        try:
            await msg.edit(**kwargs)
        except Exception:
            pass


async def _delete_message(msg: discord.Message | None):
    if msg:
        try:
            await msg.delete()
        except Exception:
            pass


# ── Guild state ────────────────────────────────────────────────────────────────
class GuildState:
    def __init__(self):
        self.history:         list[wavelink.Playable]    = []
        self.requested_by:    dict[str, discord.Member]  = {}
        self.text_channel:    discord.TextChannel | None = None
        self.player_message:  discord.Message | None     = None
        self.inactivity_task: asyncio.Task | None        = None
        self.eq_preset:       str                        = "flat"
        self.autoplay:        bool                       = False
        self.search_fallbacks: set[str]                  = set()
        self.progress_task:   asyncio.Task | None        = None
        self.liked_tracks:    dict[str, set[int]]        = {}


# ── Embed builders ─────────────────────────────────────────────────────────────
def _progress_bar(position_ms: int, duration_ms: int | None, width: int = 14) -> str:
    if not duration_ms or duration_ms <= 0:
        return "🔴 LIVE"

    progress = min(max(position_ms / duration_ms, 0.0), 1.0)
    marker = min(round(progress * (width - 1)), width - 1)
    return f"{'━' * marker}🟣{'─' * (width - marker - 1)}"


def _player_embed(
    track: wavelink.Playable,
    state: GuildState,
    bot: commands.Bot,
    *,
    player: wavelink.Player | None = None,
    paused: bool = False,
) -> discord.Embed:
    requester = state.requested_by.get(track.identifier)
    status = "⏸  PAUSED" if paused else "♫  NOW PLAYING"
    duration = track.length or 0
    position = min(player.position, duration) if player and duration else 0
    repeat_mode = player.queue.mode if player else wavelink.QueueMode.normal
    repeat = (
        "Track" if repeat_mode == wavelink.QueueMode.loop
        else "Queue" if repeat_mode == wavelink.QueueMode.loop_all
        else "Off"
    )
    volume = player.volume if player else 100

    embed = discord.Embed(
        title=status,
        description=(
            f"**[{track.title}]({track.uri})**\n"
            f"{track.author or 'Unknown artist'}\n\n"
            f"`{duration_fmt(position)}`  {_progress_bar(position, duration)}  "
            f"`{duration_fmt(duration)}`\n"
            f"🔊 Volume **{volume}%**  ·  🔁 Repeat **{repeat}**"
        ),
        color=C_PURPLE,
    )

    art = getattr(track, "artwork", None) or getattr(track, "thumbnail", None)
    if art:
        embed.set_thumbnail(url=art)

    if requester:
        embed.set_footer(
            text=f"Requested by {requester.display_name}  ·  homies_music",
            icon_url=requester.display_avatar.url,
        )
    else:
        avatar = bot.user.display_avatar.url if bot.user else None
        embed.set_footer(text="🎶 homies_music", icon_url=avatar)

    return embed


def _cancel_progress_refresh(state: GuildState):
    task = state.progress_task
    state.progress_task = None
    if task and not task.done():
        task.cancel()


async def _refresh_progress(guild: discord.Guild, state: GuildState, bot: commands.Bot):
    current_task = asyncio.current_task()
    try:
        while state.player_message:
            await asyncio.sleep(15)
            player: wavelink.Player | None = guild.voice_client  # type: ignore
            if not player or not player.current or not state.player_message:
                return

            view = MusicPlayerView(state, guild, bot)
            embed = _player_embed(
                player.current,
                state,
                bot,
                player=player,
                paused=player.paused,
            )
            await state.player_message.edit(embed=embed, view=view)
    except asyncio.CancelledError:
        raise
    except discord.HTTPException as exc:
        print(f"[music] player dashboard refresh failed: {exc}")
    finally:
        if state.progress_task is current_task:
            state.progress_task = None


def _history_embed(track: wavelink.Playable, state: GuildState, bot: commands.Bot, icon: str = "✅") -> discord.Embed:
    requester = state.requested_by.get(track.identifier)
    req_str   = f"  •  👤 {requester.mention}" if requester else ""
    embed = discord.Embed(
        description=f"{icon}  **[{track.title}]({track.uri})**  •  `{duration_fmt(track.length)}`{req_str}",
        color=C_DARK,
    )
    avatar = bot.user.display_avatar.url if bot.user else None
    embed.set_footer(text="🎶 homies_music", icon_url=avatar)
    embed.timestamp = discord.utils.utcnow()
    return embed


def _queue_embed(player: wavelink.Player) -> discord.Embed:
    embed = discord.Embed(title="📋  Queue", color=C_BLUE)
    if player.current:
        embed.add_field(
            name="▶️  Now Playing",
            value=f"> **[{player.current.title}]({player.current.uri})**\n> `{duration_fmt(player.current.length)}`",
            inline=False,
        )
    queue_list = list(player.queue)
    if queue_list:
        lines = [
            f"`{i+1}.` **[{t.title}]({t.uri})** — `{duration_fmt(t.length)}`"
            for i, t in enumerate(queue_list[:10])
        ]
        if len(queue_list) > 10:
            lines.append(f"\n*…and {len(queue_list) - 10} more tracks*")
        embed.add_field(name="🎵  Up Next", value="\n".join(lines), inline=False)
    elif not player.current:
        embed.description = "😴  The queue is empty. Use `/play` to add some music!"
    return embed


# ── Button view ────────────────────────────────────────────────────────────────
class MusicPlayerView(discord.ui.View):
    def __init__(self, state: GuildState, guild: discord.Guild, bot: commands.Bot, *, disabled: bool = False):
        super().__init__(timeout=None)
        self.state = state
        self.guild = guild
        self.bot   = bot

        player: wavelink.Player | None = guild.voice_client  # type: ignore

        custom_emojis = getattr(bot, "player_emojis", {})
        emoji_keys = {
            "music:resume": "play",
            "music:previous": "previous",
            "music:pause": "pause",
            "music:skip": "next",
            "music:like": "like",
        }
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                custom_emoji = custom_emojis.get(emoji_keys.get(item.custom_id))
                if custom_emoji:
                    item.emoji = custom_emoji

        is_paused = player.paused if player else False
        current_track = player.current if player else None
        for item in self.children:
            if not isinstance(item, discord.ui.Button):
                continue
            if item.custom_id == "music:resume":
                item.disabled = not current_track or not is_paused
            elif item.custom_id == "music:previous":
                item.disabled = not state.history
            elif item.custom_id == "music:pause":
                item.disabled = not current_track or is_paused
            elif item.custom_id in {"music:skip", "music:like"}:
                item.disabled = not current_track

        if disabled:
            for child in self.children:
                child.disabled = True

    @property
    def player(self) -> wavelink.Player | None:
        return self.guild.voice_client  # type: ignore

    async def _update_player(self):
        """Edit the existing player message in-place — no new messages."""
        player = self.player
        if not player or not player.current or not self.state.player_message:
            return
        view  = MusicPlayerView(self.state, self.guild, self.bot)
        embed = _player_embed(
            player.current,
            self.state,
            self.bot,
            player=player,
            paused=player.paused,
        )
        await _safe_edit(self.state.player_message, embed=embed, view=view)

    # Keep the five player actions together to match the compact dashboard.
    @discord.ui.button(
        emoji="▶️", style=discord.ButtonStyle.secondary, row=0, custom_id="music:resume"
    )
    async def btn_resume(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        await interaction.response.defer()
        player = self.player
        if player and player.current and player.paused:
            await player.pause(False)
            await self._update_player()

    @discord.ui.button(
        emoji="⏮️", style=discord.ButtonStyle.secondary, row=0, custom_id="music:previous"
    )
    async def btn_previous(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        await interaction.response.defer()
        if not self.state.history:
            return
        player = self.player
        if not player:
            return
        prev_track = self.state.history.pop()
        if player.current:
            await player.queue.put_wait(player.current)
            player.queue.put_at(0, player.queue[-1])
            player.queue._queue.pop()
        await player.queue.put_wait(prev_track)
        player.queue.put_at(0, player.queue[-1])
        player.queue._queue.pop()
        await player.skip(force=True)

    @discord.ui.button(
        emoji="⏸️", style=discord.ButtonStyle.secondary, row=0, custom_id="music:pause"
    )
    async def btn_pause(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        await interaction.response.defer()
        player = self.player
        if player and player.current and not player.paused:
            await player.pause(True)
            await self._update_player()

    @discord.ui.button(
        emoji="⏭️", style=discord.ButtonStyle.secondary, row=0, custom_id="music:skip"
    )
    async def btn_skip(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        await interaction.response.defer()
        player = self.player
        if player and player.current:
            await player.skip(force=True)

    @discord.ui.button(
        emoji="💜", style=discord.ButtonStyle.secondary, row=0, custom_id="music:like"
    )
    async def btn_like(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        player = self.player
        track = player.current if player else None
        if not track:
            return await interaction.response.send_message(
                "Nothing is playing right now.", ephemeral=True
            )

        likers = self.state.liked_tracks.setdefault(track.identifier, set())
        if interaction.user.id in likers:
            likers.remove(interaction.user.id)
            message = f"Removed ❤️ from **{track.title}**."
        else:
            likers.add(interaction.user.id)
            message = f"Added ❤️ to **{track.title}**."
        if not likers:
            self.state.liked_tracks.pop(track.identifier, None)

        await interaction.response.send_message(message, ephemeral=True)
        await self._update_player()


# ── Cog ────────────────────────────────────────────────────────────────────────
class Music(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot    = bot
        self.states: dict[int, GuildState] = {}

    def get_state(self, guild_id: int) -> GuildState:
        if guild_id not in self.states:
            self.states[guild_id] = GuildState()
        return self.states[guild_id]

    def _footer(self, embed: discord.Embed, hint: str = ""):
        avatar = self.bot.user.display_avatar.url if self.bot.user else None
        text   = f"🎶 homies_music  •  {hint}" if hint else "🎶 homies_music"
        embed.set_footer(text=text, icon_url=avatar)
        embed.timestamp = discord.utils.utcnow()

    def _simple_embed(self, description: str, color: int) -> discord.Embed:
        embed = discord.Embed(description=description, color=color)
        self._footer(embed)
        return embed

    # ── Inactivity ─────────────────────────────────────────────────────────────
    def _start_inactivity(self, guild: discord.Guild, state: GuildState):
        self._cancel_inactivity(state)
        state.inactivity_task = self.bot.loop.create_task(
            self._inactivity_disconnect(guild, state)
        )

    def _cancel_inactivity(self, state: GuildState):
        if state.inactivity_task and not state.inactivity_task.done():
            state.inactivity_task.cancel()
        state.inactivity_task = None

    async def _inactivity_disconnect(self, guild: discord.Guild, state: GuildState):
        await asyncio.sleep(INACTIVITY_TIMEOUT)
        player: wavelink.Player | None = guild.voice_client  # type: ignore
        if player and not player.playing:
            old = state.player_message
            state.player_message = None
            _cancel_progress_refresh(state)
            await _delete_message(old)
            if state.text_channel:
                try:
                    embed = discord.Embed(description="😴  Disconnected due to inactivity.", color=C_GREY)
                    self._footer(embed)
                    await state.text_channel.send(embed=embed)
                except Exception:
                    pass
            await player.disconnect()

    # ── wavelink events ────────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_wavelink_track_start(self, payload: wavelink.TrackStartEventPayload):
        player = payload.player
        if not player or not player.guild:
            return
        guild = player.guild
        state = self.get_state(guild.id)
        self._cancel_inactivity(state)

        if not state.text_channel:
            return

        # Delete old player message, send fresh one for the new track
        old = state.player_message
        state.player_message = None
        await _delete_message(old)

        view  = MusicPlayerView(state, guild, self.bot)
        embed = _player_embed(player.current, state, self.bot, player=player)
        msg   = await state.text_channel.send(embed=embed, view=view)
        state.player_message = msg
        _cancel_progress_refresh(state)
        state.progress_task = self.bot.loop.create_task(_refresh_progress(guild, state, self.bot))

    @commands.Cog.listener()
    async def on_wavelink_track_end(self, payload: wavelink.TrackEndEventPayload):
        player = payload.player
        if not player or not player.guild:
            return
        guild = player.guild
        state = self.get_state(guild.id)
        track = payload.track
        reason = payload.reason.upper()

        # Lavalink also emits track-end events for failed loads and explicit
        # stops. Logging the reason makes source/stream failures distinguishable
        # from a track that actually finished playing.
        print(f"[music] track ended: title={track.title!r}, reason={reason}")

        load_failed = reason in {"LOAD_FAILED", "LOADFAILED"}
        if track and not load_failed:
            state.search_fallbacks.discard(track.identifier)

        # If next track already started, track_start handles the embed
        if player.current:
            return

        _cancel_progress_refresh(state)

        # Handle fallback from track-end (rather than track-exception) so it
        # cannot race the separate Wavelink exception event or queue advance.
        if (
            track
            and load_failed
            and track.source.lower() == "youtube"
            and track.identifier in state.search_fallbacks
        ):
            state.search_fallbacks.discard(track.identifier)
            search_query = f"{track.author} {track.title}".strip()
            try:
                alternatives = await wavelink.Playable.search(
                    search_query,
                    source=wavelink.TrackSource.SoundCloud,
                )
                if alternatives:
                    fallback_track = alternatives[0]
                    requester = state.requested_by.get(track.identifier)
                    if requester:
                        state.requested_by[fallback_track.identifier] = requester
                    await player.play(fallback_track)
                    print(f"[music] using SoundCloud fallback: title={fallback_track.title!r}")
                    if state.text_channel:
                        await state.text_channel.send(
                            embed=self._simple_embed(
                                f"⚠️ YouTube could not stream **{track.title}**. Trying SoundCloud: "
                                f"**{fallback_track.title}**",
                                C_YELLOW,
                            )
                        )
                    return
                print(f"[music] SoundCloud fallback found no match for {search_query!r}")
            except Exception as fallback_error:
                print(f"[music] SoundCloud fallback failed: {fallback_error}")

        if track and reason == "FINISHED":
            state.history.append(track)

        if state.text_channel and track and reason == "FINISHED":
            try:
                await state.text_channel.send(embed=_history_embed(track, state, self.bot, "✅"))
            except Exception:
                pass

        if load_failed and state.text_channel:
            failed_embed = discord.Embed(
                description=(
                    f"⚠️  Could not load **{track.title}**. Lavalink reported `{payload.reason}`; "
                    "check the Lavalink console for the source error."
                ),
                color=C_RED,
            )
            self._footer(failed_embed)
            try:
                await state.text_channel.send(embed=failed_embed)
            except Exception:
                pass

        # Autoplay ON: wavelink will fetch a YouTube recommendation and fire track_start
        if state.autoplay:
            return

        # Manually advance to the next track in queue (don't rely on AutoPlayMode)
        if len(player.queue) > 0:
            next_track = player.queue.get()
            await player.play(next_track)
            return

        # Queue truly empty — show disabled card and start inactivity timer
        disabled_view = MusicPlayerView(state, guild, self.bot, disabled=True)
        ended_embed   = discord.Embed(description="😴  Queue ended.", color=C_GREY)
        self._footer(ended_embed)
        await _safe_edit(state.player_message, embed=ended_embed, view=disabled_view)

        self._start_inactivity(guild, state)

    @commands.Cog.listener()
    async def on_wavelink_track_exception(self, payload: wavelink.TrackExceptionEventPayload):
        player = payload.player
        track = payload.track
        exception = payload.exception
        message = exception.get("message", "No message provided")
        severity = exception.get("severity", "unknown")
        cause = exception.get("cause", "unknown")

        print(
            f"[music] track exception: title={track.title!r}, severity={severity}, "
            f"cause={cause}, message={message}"
        )

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ):
        if member.id != self.bot.user.id:
            return
        if before.channel is None or after.channel is not None:
            return
        guild = member.guild
        state = self.get_state(guild.id)
        self._cancel_inactivity(state)
        old = state.player_message
        state.player_message = None
        _cancel_progress_refresh(state)
        await _delete_message(old)
        if state.text_channel:
            try:
                embed = discord.Embed(description="👋  Disconnected from voice channel.", color=C_GREY)
                self._footer(embed)
                await state.text_channel.send(embed=embed)
            except Exception:
                pass

    # ── /play ──────────────────────────────────────────────────────────────────
    @app_commands.command(name="play", description="Play a song from YouTube (URL or search query)")
    @app_commands.describe(query="YouTube URL or search terms")
    async def play(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer()

        if not interaction.user.voice or not interaction.user.voice.channel:
            return await interaction.followup.send(
                embed=self._simple_embed("❌  You need to be in a voice channel.", C_RED), ephemeral=True
            )

        vc_channel = interaction.user.voice.channel
        state      = self.get_state(interaction.guild_id)
        state.text_channel = interaction.channel  # type: ignore

        player: wavelink.Player = interaction.guild.voice_client  # type: ignore

        if player is None:
            player = await vc_channel.connect(cls=wavelink.Player)
            player.autoplay = wavelink.AutoPlayMode.enabled if state.autoplay else wavelink.AutoPlayMode.partial
        elif player.channel != vc_channel:
            await player.move_to(vc_channel)

        self._cancel_inactivity(state)

        allow_search_fallback = not query.strip().lower().startswith(
            ("http://", "https://", "www.")
        )

        try:
            # Use regular YouTube video search for text queries; direct URLs
            # continue to be resolved as URLs by Lavalink.
            tracks = await wavelink.Playable.search(query, source=wavelink.TrackSource.YouTube)
        except Exception as exc:
            return await interaction.followup.send(
                embed=self._simple_embed(f"❌  Search failed: `{exc}`", C_RED)
            )

        print(f"[music] search result: type={type(tracks).__name__}, "
              f"count={len(tracks.tracks) if isinstance(tracks, wavelink.Playlist) else len(tracks)}")

        if not tracks:
            return await interaction.followup.send(
                embed=self._simple_embed("❌  No results found.", C_RED)
            )

        # ── Playlist ───────────────────────────────────────────────────────────
        if isinstance(tracks, wavelink.Playlist):
            playlist    = tracks
            pl_tracks   = playlist.tracks
            print(f"[music] playlist '{playlist.name}': {len(pl_tracks)} tracks loaded")

            if not pl_tracks:
                return await interaction.followup.send(
                    embed=self._simple_embed("❌  That playlist appears to be empty.", C_RED)
                )

            for t in pl_tracks:
                state.requested_by[t.identifier] = interaction.user
                if allow_search_fallback:
                    state.search_fallbacks.add(t.identifier)

            total_ms = sum(t.length or 0 for t in pl_tracks)

            if player.current:
                # Already playing — add whole playlist to end of queue in one call
                q_before = len(player.queue)
                added    = await player.queue.put_wait(playlist)
                print(f"[music] queued {added} tracks (queue was {q_before}, now {len(player.queue)})")
                embed = discord.Embed(
                    description=(
                        f"📋  **[{playlist.name}]({query})**\n"
                        f"`{added}` tracks  •  ⏱ `{duration_fmt(total_ms)}`"
                        f"  •  📍 starts at `#{q_before + 1}`"
                        f"  •  👤 {interaction.user.mention}"
                    ),
                    color=C_BLUE,
                )
                art = getattr(playlist, "artwork", None)
                if art:
                    embed.set_thumbnail(url=art)
                self._footer(embed, "Use /queue to see the full queue")
                await interaction.followup.send(embed=embed)
            else:
                # Nothing playing — queue all tracks then pop first to play
                added = await player.queue.put_wait(playlist)
                print(f"[music] queued {added} tracks, starting playback")
                first = player.queue.get()   # removes track 1 from queue; tracks 2-N stay
                await player.set_filters(_build_filters(state.eq_preset))
                await player.play(first)
                await interaction.followup.send(
                    embed=self._simple_embed(
                        f"▶️  Playing playlist **{playlist.name}** — `{added}` tracks queued.",
                        C_PURPLE,
                    ),
                    ephemeral=True,
                )
            return

        # ── Single track ───────────────────────────────────────────────────────
        track = tracks[0]
        state.requested_by[track.identifier] = interaction.user
        if allow_search_fallback:
            state.search_fallbacks.add(track.identifier)

        if player.current:
            await player.queue.put_wait(track)
            embed = discord.Embed(
                description=(
                    f"📋  **[{track.title}]({track.uri})**\n"
                    f"by {track.author or 'Unknown'}"
                    f"  •  ⏱ `{duration_fmt(track.length)}`"
                    f"  •  📍 `#{len(player.queue)}`"
                    f"  •  👤 {interaction.user.mention}"
                ),
                color=C_BLUE,
            )
            art = getattr(track, "artwork", None) or getattr(track, "thumbnail", None)
            if art:
                embed.set_thumbnail(url=art)
            self._footer(embed, "Use /queue to see the full queue")
            await interaction.followup.send(embed=embed)
        else:
            await player.set_filters(_build_filters(state.eq_preset))
            await player.play(track)
            await interaction.followup.send(
                embed=self._simple_embed(f"▶️  Loading **{track.title}**…", C_PURPLE),
                ephemeral=True,
            )

    # ── /skip ──────────────────────────────────────────────────────────────────
    @app_commands.command(name="skip", description="Skip the current song")
    async def skip(self, interaction: discord.Interaction):
        await interaction.response.defer()
        player: wavelink.Player | None = interaction.guild.voice_client  # type: ignore
        if not player or not player.current:
            return await interaction.followup.send(
                embed=self._simple_embed("❌  Nothing is playing.", C_RED), ephemeral=True
            )
        state   = self.get_state(interaction.guild_id)
        current = player.current
        if current:
            state.history.append(current)
        up_next = list(player.queue)
        label   = f"**[{up_next[0].title}]({up_next[0].uri})**" if up_next else "nothing — queue is empty"
        await player.skip(force=True)
        await interaction.followup.send(
            embed=self._simple_embed(f"⏭️  Skipped  —  Up next: {label}", C_YELLOW)
        )

    @app_commands.command(name="shuffle", description="Shuffle the songs waiting in the queue")
    async def shuffle(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        player: wavelink.Player | None = interaction.guild.voice_client  # type: ignore
        if not player or len(player.queue) < 2:
            return await interaction.followup.send(
                embed=self._simple_embed("Add at least two songs to shuffle.", C_GREY),
                ephemeral=True,
            )
        player.queue.shuffle()
        await interaction.followup.send(
            embed=self._simple_embed("🔀 Queue shuffled.", C_PURPLE),
            ephemeral=True,
        )

    @app_commands.command(name="loop", description="Toggle repeating the current song")
    async def loop(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        player: wavelink.Player | None = interaction.guild.voice_client  # type: ignore
        if not player:
            return await interaction.followup.send(
                embed=self._simple_embed("The player is not connected.", C_GREY),
                ephemeral=True,
            )

        if player.queue.mode == wavelink.QueueMode.loop:
            player.queue.mode = wavelink.QueueMode.normal
            status = "off"
        else:
            player.queue.mode = wavelink.QueueMode.loop
            status = "on"

        if player.current and self.get_state(interaction.guild_id).player_message:
            state = self.get_state(interaction.guild_id)
            embed = _player_embed(
                player.current,
                state,
                self.bot,
                player=player,
                paused=player.paused,
            )
            await _safe_edit(state.player_message, embed=embed, view=MusicPlayerView(state, player.guild, self.bot))

        await interaction.followup.send(
            embed=self._simple_embed(f"🔁 Repeat is **{status}**.", C_PURPLE),
            ephemeral=True,
        )

    # ── /queue ──────────────────────────────────────────────────────────────────
    @app_commands.command(name="queue", description="Show the current song queue")
    async def queue(self, interaction: discord.Interaction):
        await interaction.response.defer()
        player: wavelink.Player | None = interaction.guild.voice_client  # type: ignore
        if not player:
            embed = discord.Embed(description="😴  The queue is empty. Use `/play` to add some music!", color=C_BLUE)
            self._footer(embed)
            return await interaction.followup.send(embed=embed)
        embed = _queue_embed(player)
        self._footer(embed)
        await interaction.followup.send(embed=embed)

    # ── /pause ──────────────────────────────────────────────────────────────────
    @app_commands.command(name="pause", description="Pause playback")
    async def pause(self, interaction: discord.Interaction):
        await interaction.response.defer()
        player: wavelink.Player | None = interaction.guild.voice_client  # type: ignore
        if not player or not player.playing:
            return await interaction.followup.send(
                embed=self._simple_embed("❌  Nothing is playing.", C_RED), ephemeral=True
            )
        await player.pause(True)
        await interaction.followup.send(embed=self._simple_embed("⏸️  Paused.", C_YELLOW))

    # ── /resume ──────────────────────────────────────────────────────────────────
    @app_commands.command(name="resume", description="Resume playback")
    async def resume(self, interaction: discord.Interaction):
        await interaction.response.defer()
        player: wavelink.Player | None = interaction.guild.voice_client  # type: ignore
        if not player or not player.paused:
            return await interaction.followup.send(
                embed=self._simple_embed("❌  Nothing is paused.", C_RED), ephemeral=True
            )
        await player.pause(False)
        await interaction.followup.send(embed=self._simple_embed("▶️  Resumed.", C_GREEN))

    # ── /stop ──────────────────────────────────────────────────────────────────
    @app_commands.command(name="stop", description="Stop playback and clear the queue")
    async def stop(self, interaction: discord.Interaction):
        await interaction.response.defer()
        player: wavelink.Player | None = interaction.guild.voice_client  # type: ignore
        state  = self.get_state(interaction.guild_id)
        source = player.current if player else None

        if source:
            state.history.append(source)

        self._cancel_inactivity(state)
        old = state.player_message
        state.player_message = None
        _cancel_progress_refresh(state)

        if player:
            player.queue.clear()
            await player.stop()

        if source and state.text_channel:
            await state.text_channel.send(embed=_history_embed(source, state, self.bot, "⏹️"))  # type: ignore

        await _delete_message(old)
        await interaction.followup.send(embed=self._simple_embed("⏹️  Stopped and queue cleared.", C_RED))

    # ── /leave ──────────────────────────────────────────────────────────────────
    @app_commands.command(name="leave", description="Disconnect from the voice channel")
    async def leave(self, interaction: discord.Interaction):
        await interaction.response.defer()
        player: wavelink.Player | None = interaction.guild.voice_client  # type: ignore
        state  = self.get_state(interaction.guild_id)
        source = player.current if player else None

        if source:
            state.history.append(source)

        self._cancel_inactivity(state)
        old = state.player_message
        state.player_message = None
        _cancel_progress_refresh(state)
        await _delete_message(old)

        if source and state.text_channel:
            await state.text_channel.send(embed=_history_embed(source, state, self.bot, "👋"))  # type: ignore

        if player:
            player.queue.clear()
            await player.disconnect()

        await interaction.followup.send(embed=self._simple_embed("👋  Disconnected. See you next time!", C_GREY))

    # ── /nowplaying ──────────────────────────────────────────────────────────────
    @app_commands.command(name="nowplaying", description="Show the currently playing song")
    async def nowplaying(self, interaction: discord.Interaction):
        await interaction.response.defer()
        player: wavelink.Player | None = interaction.guild.voice_client  # type: ignore
        if not player or not player.current:
            return await interaction.followup.send(
                embed=self._simple_embed("❌  Nothing is playing right now.", C_RED), ephemeral=True
            )
        state = self.get_state(interaction.guild_id)
        await interaction.followup.send(
            embed=_player_embed(
                player.current,
                state,
                self.bot,
                player=player,
                paused=player.paused,
            )
        )

    # ── /eq ──────────────────────────────────────────────────────────────────────
    _EQ_LABELS: dict[str, str] = {
        "flat": "🎚️  Flat", "bass_boost": "🔊  Bass Boost",
        "treble_boost": "✨  Treble Boost", "loud": "📢  Loud",
    }

    @app_commands.command(name="eq", description="Apply an EQ preset")
    @app_commands.describe(preset="Audio EQ preset to apply")
    @app_commands.choices(preset=[
        app_commands.Choice(name="🎚️  Flat (no EQ)",   value="flat"),
        app_commands.Choice(name="🔊  Bass Boost",      value="bass_boost"),
        app_commands.Choice(name="✨  Treble Boost",    value="treble_boost"),
        app_commands.Choice(name="📢  Loud (default)",  value="loud"),
    ])
    async def eq(self, interaction: discord.Interaction, preset: app_commands.Choice[str]):
        await interaction.response.defer(ephemeral=True)
        player: wavelink.Player | None = interaction.guild.voice_client  # type: ignore
        state  = self.get_state(interaction.guild_id)

        if not player or not player.current:
            return await interaction.followup.send(
                embed=self._simple_embed("❌  Nothing is playing right now.", C_RED), ephemeral=True
            )

        state.eq_preset = preset.value
        await player.set_filters(_build_filters(preset.value))
        label = self._EQ_LABELS.get(preset.value, preset.value)
        await interaction.followup.send(embed=self._simple_embed(f"🎛️  EQ → **{label}**", C_GREEN), ephemeral=True)


    # ── /autoplay ────────────────────────────────────────────────────────────────
    @app_commands.command(name="autoplay", description="Toggle autoplay — plays similar songs when the queue ends")
    async def autoplay_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer()
        state = self.get_state(interaction.guild_id)
        state.autoplay = not state.autoplay

        player: wavelink.Player | None = interaction.guild.voice_client  # type: ignore
        if player:
            player.autoplay = (
                wavelink.AutoPlayMode.enabled if state.autoplay else wavelink.AutoPlayMode.partial
            )

        if state.autoplay:
            await interaction.followup.send(
                embed=self._simple_embed(
                    "🔄  Autoplay **enabled** — similar songs will automatically play when the queue ends.",
                    C_GREEN,
                )
            )
        else:
            await interaction.followup.send(
                embed=self._simple_embed(
                    "🔄  Autoplay **disabled** — the player will stop when the queue is empty.",
                    C_GREY,
                )
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(Music(bot))
