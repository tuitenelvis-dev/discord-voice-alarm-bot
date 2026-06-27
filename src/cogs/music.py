from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

import discord
import yt_dlp
from discord import app_commands
from discord.ext import commands, tasks

if TYPE_CHECKING:
    from src.bot import VoiceAlarmBot


LOGGER = logging.getLogger(__name__)

YTDLP_OPTIONS = {
    "format": "bestaudio/best",
    "quiet": True,
    "default_search": "ytsearch",
    "extract_flat": False,
    "noplaylist": True,
    "source_address": "0.0.0.0",
}

FFMPEG_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}


@dataclass(slots=True)
class Track:
    title: str
    webpage_url: str
    stream_url: str
    requester_id: int
    duration: int | None = None

    @property
    def duration_text(self) -> str:
        if not self.duration:
            return "live/unknown"
        minutes, seconds = divmod(self.duration, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours}:{minutes:02d}:{seconds:02d}"
        return f"{minutes}:{seconds:02d}"


class GuildMusicState:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[Track] = asyncio.Queue()
        self.current: Track | None = None
        self.audio_task: asyncio.Task[None] | None = None
        self.next_event = asyncio.Event()
        self.lock = asyncio.Lock()


class MusicCog(commands.Cog):
    def __init__(self, bot: VoiceAlarmBot) -> None:
        self.bot = bot
        self.states: dict[int, GuildMusicState] = {}
        self.ytdlp = yt_dlp.YoutubeDL(YTDLP_OPTIONS)
        self.ensure_stay_channels.start()

    def cog_unload(self) -> None:
        self.ensure_stay_channels.cancel()
        for state in self.states.values():
            if state.audio_task:
                state.audio_task.cancel()

    def get_state(self, guild_id: int) -> GuildMusicState:
        state = self.states.get(guild_id)
        if state is None:
            state = GuildMusicState()
            self.states[guild_id] = state
        return state

    async def extract_track(self, query: str, requester_id: int) -> Track:
        query = query.strip()
        search_query = query

        if "open.spotify.com/" in query:
            spotify_info = await asyncio.to_thread(self._extract_info, query, False)
            title = spotify_info.get("title") or query
            artists = spotify_info.get("artists") or spotify_info.get("artist") or ""
            if isinstance(artists, list):
                artists = " ".join(str(artist) for artist in artists)
            search_query = f"ytsearch1:{title} {artists}".strip()
        elif not query.startswith(("http://", "https://")):
            search_query = f"ytsearch1:{query}"

        info = await asyncio.to_thread(self._extract_info, search_query, False)
        if "entries" in info:
            entries = [entry for entry in info["entries"] if entry]
            if not entries:
                raise ValueError("Không tìm thấy bài nhạc phù hợp.")
            info = entries[0]

        stream_url = info.get("url")
        if not stream_url:
            raise ValueError("Không lấy được audio stream. Thử link/từ khóa khác.")

        return Track(
            title=info.get("title") or "Unknown title",
            webpage_url=info.get("webpage_url") or info.get("original_url") or query,
            stream_url=stream_url,
            requester_id=requester_id,
            duration=info.get("duration"),
        )

    def _extract_info(self, query: str, download: bool) -> dict:
        return self.ytdlp.extract_info(query, download=download)

    async def connect_to_channel(self, channel: discord.VoiceChannel) -> discord.VoiceClient:
        current = channel.guild.voice_client
        if current and current.is_connected():
            if current.channel.id != channel.id:
                await current.move_to(channel)
            return current

        return await channel.connect(self_deaf=True, reconnect=True)

    async def start_audio_task(self, guild: discord.Guild) -> None:
        state = self.get_state(guild.id)
        if state.audio_task and not state.audio_task.done():
            return
        state.audio_task = asyncio.create_task(self.audio_player_loop(guild))

    async def audio_player_loop(self, guild: discord.Guild) -> None:
        state = self.get_state(guild.id)

        while True:
            state.next_event.clear()
            track = await state.queue.get()
            state.current = track

            voice = guild.voice_client
            if not voice or not voice.is_connected():
                state.current = None
                continue

            source = discord.FFmpegPCMAudio(
                track.stream_url,
                executable=self.bot.settings.ffmpeg_path,
                **FFMPEG_OPTIONS,
            )

            def after_play(error: Exception | None) -> None:
                if error:
                    LOGGER.warning("Playback error in guild %s: %s", guild.id, error)
                self.bot.loop.call_soon_threadsafe(state.next_event.set)

            voice.play(source, after=after_play)
            await state.next_event.wait()
            state.current = None

    @tasks.loop(minutes=1)
    async def ensure_stay_channels(self) -> None:
        snapshot = await self.bot.store.snapshot()
        stay_channels: dict[str, int] = snapshot.get("stay_channels", {})

        for guild_id_raw, channel_id in stay_channels.items():
            guild = self.bot.get_guild(int(guild_id_raw))
            if not guild:
                continue

            channel = guild.get_channel(int(channel_id))
            if not isinstance(channel, discord.VoiceChannel):
                continue

            try:
                await self.connect_to_channel(channel)
                await self.start_audio_task(guild)
            except discord.DiscordException as exc:
                LOGGER.warning("Could not reconnect stay channel %s: %s", channel_id, exc)

    @ensure_stay_channels.before_loop
    async def before_ensure_stay_channels(self) -> None:
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        await self.ensure_stay_channels()

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if not self.bot.user or member.id != self.bot.user.id:
            return

        snapshot = await self.bot.store.snapshot()
        stay_channel_id = snapshot.get("stay_channels", {}).get(str(member.guild.id))
        if not stay_channel_id:
            return

        if after.channel is None:
            await asyncio.sleep(5)
            channel = member.guild.get_channel(int(stay_channel_id))
            if isinstance(channel, discord.VoiceChannel):
                await self.connect_to_channel(channel)

    @app_commands.command(name="play", description="Phát nhạc từ YouTube URL, từ khóa, Spotify URL hoặc direct audio URL.")
    @app_commands.describe(query="URL hoặc từ khóa cần phát")
    async def play(self, interaction: discord.Interaction, query: str) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Lệnh này chỉ dùng được trong server.", ephemeral=True)
            return

        voice_channel = interaction.user.voice.channel if interaction.user.voice else None
        if not isinstance(voice_channel, discord.VoiceChannel):
            await interaction.response.send_message("Bạn cần vào voice room trước.", ephemeral=True)
            return

        await interaction.response.defer(thinking=True)

        try:
            await self.connect_to_channel(voice_channel)
            track = await self.extract_track(query, interaction.user.id)
        except Exception as exc:
            await interaction.followup.send(f"Không thể phát bài này: `{exc}`")
            return

        state = self.get_state(interaction.guild.id)
        await state.queue.put(track)
        await self.start_audio_task(interaction.guild)

        await interaction.followup.send(
            f"Đã thêm vào queue: **{track.title}** (`{track.duration_text}`)\n{track.webpage_url}"
        )

    @app_commands.command(name="join247", description="Cho bot treo 24/7 trong voice room hiện tại.")
    async def join247(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Lệnh này chỉ dùng được trong server.", ephemeral=True)
            return

        channel = interaction.user.voice.channel if interaction.user.voice else None
        if not isinstance(channel, discord.VoiceChannel):
            await interaction.response.send_message("Bạn cần vào voice room muốn treo bot.", ephemeral=True)
            return

        await self.bot.store.set_stay_channel(interaction.guild.id, channel.id)
        await self.connect_to_channel(channel)
        await self.start_audio_task(interaction.guild)

        await interaction.response.send_message(f"Đã bật treo 24/7 tại **{channel.name}**.")

    @app_commands.command(name="leave247", description="Tắt treo room 24/7 và cho bot rời voice.")
    async def leave247(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Lệnh này chỉ dùng được trong server.", ephemeral=True)
            return

        await self.bot.store.remove_stay_channel(interaction.guild.id)
        voice = interaction.guild.voice_client
        if voice and voice.is_connected():
            await voice.disconnect(force=True)

        await interaction.response.send_message("Đã tắt treo 24/7.")

    @app_commands.command(name="queue", description="Xem hàng chờ nhạc hiện tại.")
    async def queue(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Lệnh này chỉ dùng được trong server.", ephemeral=True)
            return

        state = self.get_state(interaction.guild.id)
        lines: list[str] = []
        if state.current:
            lines.append(f"Đang phát: **{state.current.title}**")

        queued = list(state.queue._queue)
        if queued:
            lines.extend(
                f"{index}. {track.title} (`{track.duration_text}`)"
                for index, track in enumerate(queued[:10], start=1)
            )

        await interaction.response.send_message("\n".join(lines) if lines else "Queue đang trống.")

    @app_commands.command(name="skip", description="Bỏ qua bài đang phát.")
    async def skip(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not interaction.guild.voice_client:
            await interaction.response.send_message("Bot chưa phát nhạc.", ephemeral=True)
            return

        voice = interaction.guild.voice_client
        if voice.is_playing() or voice.is_paused():
            voice.stop()
            await interaction.response.send_message("Đã skip bài hiện tại.")
        else:
            await interaction.response.send_message("Không có bài nào đang phát.", ephemeral=True)

    @app_commands.command(name="pause", description="Tạm dừng nhạc.")
    async def pause(self, interaction: discord.Interaction) -> None:
        voice = interaction.guild.voice_client if interaction.guild else None
        if voice and voice.is_playing():
            voice.pause()
            await interaction.response.send_message("Đã pause.")
        else:
            await interaction.response.send_message("Không có bài nào đang phát.", ephemeral=True)

    @app_commands.command(name="resume", description="Tiếp tục phát nhạc.")
    async def resume(self, interaction: discord.Interaction) -> None:
        voice = interaction.guild.voice_client if interaction.guild else None
        if voice and voice.is_paused():
            voice.resume()
            await interaction.response.send_message("Đã resume.")
        else:
            await interaction.response.send_message("Bot không ở trạng thái pause.", ephemeral=True)

    @app_commands.command(name="stop", description="Dừng nhạc và xóa queue nhưng bot vẫn ở voice room.")
    async def stop(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Lệnh này chỉ dùng được trong server.", ephemeral=True)
            return

        state = self.get_state(interaction.guild.id)
        while not state.queue.empty():
            state.queue.get_nowait()
            state.queue.task_done()

        voice = interaction.guild.voice_client
        if voice and (voice.is_playing() or voice.is_paused()):
            voice.stop()

        await interaction.response.send_message("Đã stop nhạc và xóa queue. Bot vẫn ở voice room.")


async def setup(bot: VoiceAlarmBot) -> None:
    await bot.add_cog(MusicCog(bot))
