import asyncio
import discord
import wavelink
from discord import app_commands
from discord.ext import commands

# ── Constants ──────────────────────────────────────────────────────────────────
INACTIVITY_TIMEOUT = 180

# ── Color palette ──────────────────────────────────────────────────────────────
C_PURPLE = 0x9B59B6
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


# ── Embed builders ─────────────────────────────────────────────────────────────
def _player_embed(
    track: wavelink.Playable,
    state: GuildState,
    bot: commands.Bot,
    *,
    paused: bool = False,
) -> discord.Embed:
    requester = state.requested_by.get(track.identifier)
    status    = "⏸  Paused" if paused else "▶  Now Playing"

    embed = discord.Embed(
        title=status,
        description=f"**[{track.title}]({track.uri})**\nby {track.author or 'Unknown'}",
        color=C_YELLOW if paused else C_PURPLE,
    )

    art = getattr(track, "artwork", None) or getattr(track, "thumbnail", None)
    if art:
        embed.set_image(url=art)

    embed.add_field(name="⏳ Duration",     value=f"`{duration_fmt(track.length)}`", inline=True)
    embed.add_field(name="👤 Requested by", value=requester.display_name if requester else "Unknown", inline=True)
    embed.add_field(name="🔄 Autoplay",    value="ON" if state.autoplay else "OFF",                  inline=True)

    if requester:
        embed.set_footer(text=f"Requested by {requester.display_name}", icon_url=requester.display_avatar.url)
    else:
        avatar = bot.user.display_avatar.url if bot.user else None
        embed.set_footer(text="🎶 homies_music", icon_url=avatar)

    embed.timestamp = discord.utils.utcnow()
    return embed


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

        is_paused = player.paused if player else False
        for item in self.children:
            if isinstance(item, discord.ui.Button) and item.label:
                if "Pause" in item.label or "Resume" in item.label:
                    item.label = "▶ Resume" if is_paused else "⏸ Pause"
                    break

        loop_on = (
            player.queue.mode in (wavelink.QueueMode.loop, wavelink.QueueMode.loop_all)
            if player else False
        )
        for item in self.children:
            if isinstance(item, discord.ui.Button) and item.label and "Loop" in item.label:
                item.style = discord.ButtonStyle.success if loop_on else discord.ButtonStyle.secondary
                item.label = "🔁 Loop ON" if loop_on else "🔁 Loop"
                break

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
        embed = _player_embed(player.current, self.state, self.bot, paused=player.paused)
        await _safe_edit(self.state.player_message, embed=embed, view=view)

    # ── Row 0 ─────────────────────────────────────────────────────────────────
    @discord.ui.button(emoji="⏮️", style=discord.ButtonStyle.secondary, row=0)
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

    @discord.ui.button(label="⏸ Pause", style=discord.ButtonStyle.primary, row=0)
    async def btn_pause_resume(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        await interaction.response.defer()
        player = self.player
        if player:
            await player.pause(not player.paused)
        await self._update_player()

    @discord.ui.button(emoji="⏭️", style=discord.ButtonStyle.secondary, row=0)
    async def btn_skip(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        await interaction.response.defer()
        player = self.player
        if player and player.current:
            await player.skip(force=True)
        # on_wavelink_track_start will update the embed for the next track

    # ── Row 1 ─────────────────────────────────────────────────────────────────
    @discord.ui.button(label="🔀 Shuffle", style=discord.ButtonStyle.success, row=1)
    async def btn_shuffle(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        await interaction.response.defer()
        player = self.player
        if player and len(player.queue) > 1:
            player.queue.shuffle()
        await self._update_player()

    @discord.ui.button(label="🔁 Loop", style=discord.ButtonStyle.secondary, row=1)
    async def btn_loop(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        await interaction.response.defer()
        player = self.player
        if player:
            if player.queue.mode == wavelink.QueueMode.loop:
                player.queue.mode = wavelink.QueueMode.normal
            else:
                player.queue.mode = wavelink.QueueMode.loop
        await self._update_player()

    @discord.ui.button(label="⏹ Stop", style=discord.ButtonStyle.danger, row=1)
    async def btn_stop(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        await interaction.response.defer()
        player = self.player
        source = player.current if player else None
        if source:
            self.state.history.append(source)

        old = self.state.player_message
        self.state.player_message = None

        if player:
            player.queue.clear()
            await player.stop()
            await player.disconnect()

        # Edit existing message to disabled/stopped state
        frozen = (
            _player_embed(source, self.state, self.bot)
            if source
            else discord.Embed(description="😴  Stopped.", color=C_GREY)
        )
        disabled_view = MusicPlayerView(self.state, self.guild, self.bot, disabled=True)
        await _safe_edit(old, embed=frozen, view=disabled_view)

        channel = self.state.text_channel or interaction.channel
        if channel and source:
            await channel.send(embed=_history_embed(source, self.state, self.bot, "⏹️"))


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
        embed = _player_embed(player.current, state, self.bot)
        msg   = await state.text_channel.send(embed=embed, view=view)
        state.player_message = msg

    @commands.Cog.listener()
    async def on_wavelink_track_end(self, payload: wavelink.TrackEndEventPayload):
        player = payload.player
        if not player or not player.guild:
            return
        guild = player.guild
        state = self.get_state(guild.id)
        track = payload.track

        # If next track already started, track_start handles the embed
        if player.current:
            return

        if track:
            state.history.append(track)

        if state.text_channel and track:
            try:
                await state.text_channel.send(embed=_history_embed(track, state, self.bot, "✅"))
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

        try:
            tracks = await wavelink.Playable.search(query)
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
        await interaction.followup.send(embed=_player_embed(player.current, state, self.bot, paused=player.paused))

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
