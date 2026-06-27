from __future__ import annotations

import asyncio
import logging
import math
import wave
from dataclasses import dataclass
from pathlib import Path
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
    "source_address": "0.0.0.0",
}

MAX_PLAYLIST_TRACKS = 50

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
        self.alarm_active = False
        self.lock = asyncio.Lock()


class MusicCog(commands.Cog):
    def __init__(self, bot: VoiceAlarmBot) -> None:
        self.bot = bot
        self.states: dict[int, GuildMusicState] = {}
        self.alarm_sound_path = Path(__file__).resolve().parents[2] / "data" / "alarm_messenger_style.wav"
        self.ensure_alarm_sound()
        self.ensure_stay_channels.start()

    def ensure_alarm_sound(self) -> None:
        if self.alarm_sound_path.exists():
            return

        self.alarm_sound_path.parent.mkdir(parents=True, exist_ok=True)
        sample_rate = 44_100
        total_frames = int(sample_rate * 7.5)
        pattern = (
            (880, 0.18),
            (1175, 0.18),
            (0, 0.12),
            (880, 0.18),
            (1175, 0.18),
            (0, 0.45),
        )
        pattern_samples = [(frequency, int(length * sample_rate)) for frequency, length in pattern]
        pattern_total = sum(length for _, length in pattern_samples)
        frames = bytearray()

        for index in range(total_frames):
            cursor = index % pattern_total
            frequency = 0
            offset = 0
            for candidate_frequency, length in pattern_samples:
                if cursor < offset + length:
                    frequency = candidate_frequency
                    cursor -= offset
                    break
                offset += length

            if frequency == 0:
                sample = 0
            else:
                envelope = min(1.0, cursor / (sample_rate * 0.025))
                value = math.sin(2 * math.pi * frequency * index / sample_rate)
                sample = int(0.42 * envelope * value * 32767)
            frames.extend(sample.to_bytes(2, "little", signed=True))

        with wave.open(str(self.alarm_sound_path), "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(sample_rate)
            wav_file.writeframes(bytes(frames))

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

    async def extract_tracks(self, query: str, requester_id: int) -> list[Track]:
        query = query.strip()
        search_query = query
        wants_playlist = any(
            marker in query
            for marker in (
                "list=",
                "/playlist",
                "open.spotify.com/playlist",
                "open.spotify.com/album",
            )
        )

        if "open.spotify.com/" in query:
            spotify_info = await asyncio.to_thread(self._extract_info, query, False)
            title = spotify_info.get("title") or query
            artists = spotify_info.get("artists") or spotify_info.get("artist") or ""
            if isinstance(artists, list):
                artists = " ".join(str(artist) for artist in artists)
            search_query = f"ytsearch1:{title} {artists}".strip()
        elif not query.startswith(("http://", "https://")):
            search_query = f"ytsearch1:{query}"

        info = await asyncio.to_thread(self._extract_info, search_query, False, wants_playlist)
        if "entries" in info:
            entries = [entry for entry in info["entries"] if entry]
            if not entries:
                raise ValueError("Không tìm thấy bài nhạc phù hợp.")
            if not wants_playlist:
                entries = entries[:1]
            else:
                entries = entries[:MAX_PLAYLIST_TRACKS]
        else:
            entries = [info]

        tracks: list[Track] = []
        for entry in entries:
            if entry.get("_type") == "url" or not entry.get("url"):
                entry_url = entry.get("url") or entry.get("webpage_url")
                if not entry_url:
                    continue
                entry = await asyncio.to_thread(self._extract_info, entry_url, False, False)

            stream_url = entry.get("url")
            if not stream_url:
                continue

            tracks.append(
                Track(
                    title=entry.get("title") or "Unknown title",
                    webpage_url=entry.get("webpage_url") or entry.get("original_url") or query,
                    stream_url=stream_url,
                    requester_id=requester_id,
                    duration=entry.get("duration"),
                )
            )

        if not tracks:
            raise ValueError("Không lấy được audio stream. Thử link/từ khóa khác.")

        return tracks

    async def extract_track(self, query: str, requester_id: int) -> Track:
        return (await self.extract_tracks(query, requester_id))[0]

    def _extract_info(self, query: str, download: bool, playlist: bool = False) -> dict:
        options = dict(YTDLP_OPTIONS)
        options["noplaylist"] = not playlist
        if playlist:
            options["playlistend"] = MAX_PLAYLIST_TRACKS
        with yt_dlp.YoutubeDL(options) as ytdlp:
            return ytdlp.extract_info(query, download=download)

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
            if state.alarm_active:
                await state.queue.put(track)
                await asyncio.sleep(1)
                continue
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

    async def start_alarm_sound(self, guild: discord.Guild) -> bool:
        state = self.get_state(guild.id)
        state.alarm_active = True

        voice = guild.voice_client
        if not voice or not voice.is_connected():
            snapshot = await self.bot.store.snapshot()
            stay_channel_id = snapshot.get("stay_channels", {}).get(str(guild.id))
            channel = guild.get_channel(int(stay_channel_id)) if stay_channel_id else None
            if isinstance(channel, discord.VoiceChannel):
                voice = await self.connect_to_channel(channel)

        if not voice or not voice.is_connected():
            return False

        if voice.is_playing() or voice.is_paused():
            voice.stop()

        source = discord.FFmpegPCMAudio(
            str(self.alarm_sound_path),
            executable=self.bot.settings.ffmpeg_path,
            before_options="-stream_loop -1",
            options="-vn",
        )
        voice.play(source)
        return True

    async def stop_alarm_sound(self, guild: discord.Guild) -> bool:
        state = self.get_state(guild.id)
        was_active = state.alarm_active
        state.alarm_active = False

        voice = guild.voice_client
        if voice and (voice.is_playing() or voice.is_paused()):
            voice.stop()

        await self.start_audio_task(guild)
        return was_active

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
            tracks = await self.extract_tracks(query, interaction.user.id)
        except Exception as exc:
            await interaction.followup.send(f"Không thể phát bài này: `{exc}`")
            return

        state = self.get_state(interaction.guild.id)
        for track in tracks:
            await state.queue.put(track)
        await self.start_audio_task(interaction.guild)

        if len(tracks) == 1:
            track = tracks[0]
            await interaction.followup.send(
                f"Đã thêm vào queue: **{track.title}** (`{track.duration_text}`)\n{track.webpage_url}"
            )
        else:
            preview = "\n".join(f"{index}. {track.title}" for index, track in enumerate(tracks[:5], start=1))
            more = f"\n... và {len(tracks) - 5} bài nữa" if len(tracks) > 5 else ""
            await interaction.followup.send(
                f"Đã thêm **{len(tracks)}** bài vào queue từ playlist.\n{preview}{more}"
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

    @app_commands.command(name="stopalarm", description="Tat am bao thuc dang keu trong voice room.")
    async def stopalarm(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Lenh nay chi dung duoc trong server.", ephemeral=True)
            return

        stopped = await self.stop_alarm_sound(interaction.guild)
        if stopped:
            await interaction.response.send_message("Da tat bao thuc.")
        else:
            await interaction.response.send_message("Khong co bao thuc nao dang keu.", ephemeral=True)


async def setup(bot: VoiceAlarmBot) -> None:
    await bot.add_cog(MusicCog(bot))
