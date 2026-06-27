from __future__ import annotations

import asyncio
import re
import uuid
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands, tasks

from src.config import VIETNAM_TZ

if TYPE_CHECKING:
    from src.bot import VoiceAlarmBot


TIME_PATTERN = re.compile(r"^(?P<hour>[01]\d|2[0-3]):(?P<minute>[0-5]\d)$")


def parse_hhmm(value: str) -> tuple[int, int]:
    match = TIME_PATTERN.fullmatch(value.strip())
    if not match:
        raise ValueError("Thời gian phải đúng định dạng 24 giờ HH:MM, ví dụ 07:30 hoặc 22:15.")

    return int(match.group("hour")), int(match.group("minute"))


class AlarmCog(commands.Cog):
    def __init__(self, bot: VoiceAlarmBot) -> None:
        self.bot = bot
        self.alarm_scanner.start()

    def cog_unload(self) -> None:
        self.alarm_scanner.cancel()

    @tasks.loop(minutes=1)
    async def alarm_scanner(self) -> None:
        now = datetime.now(VIETNAM_TZ)
        current_time = now.strftime("%H:%M")
        today = now.date().isoformat()

        snapshot = await self.bot.store.snapshot()
        alarms = snapshot.get("alarms", [])
        fired_ids: set[str] = set()

        for alarm in alarms:
            if alarm.get("time") != current_time:
                continue

            # Prevent double-fire if Discord reconnects and the loop is manually restarted inside the same minute.
            if alarm.get("created_date") == today and alarm.get("created_time") == current_time:
                continue

            channel = self.bot.get_channel(int(alarm["channel_id"]))
            if not isinstance(channel, discord.abc.Messageable):
                fired_ids.add(alarm["id"])
                continue

            message = (
                f"<@{alarm['user_id']}> Báo thức `{alarm['time']}` giờ Việt Nam: "
                f"**{alarm['message']}**"
            )
            try:
                await channel.send(message)
            finally:
                fired_ids.add(alarm["id"])

        if fired_ids:
            await self.bot.store.remove_alarms(fired_ids)

    @alarm_scanner.before_loop
    async def before_alarm_scanner(self) -> None:
        await self.bot.wait_until_ready()
        now = datetime.now(VIETNAM_TZ)
        next_minute = (now + timedelta(minutes=1)).replace(second=0, microsecond=0)
        await asyncio.sleep((next_minute - now).total_seconds())

    @app_commands.command(name="setalarm", description="Đặt báo thức một lần theo giờ Việt Nam.")
    @app_commands.describe(
        time="Định dạng 24 giờ HH:MM, ví dụ 07:30 hoặc 22:15",
        message="Nội dung nhắc nhở",
    )
    async def setalarm(self, interaction: discord.Interaction, time: str, message: str) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Lệnh này chỉ dùng được trong server.", ephemeral=True)
            return

        try:
            parse_hhmm(time)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        now = datetime.now(VIETNAM_TZ)
        alarm = {
            "id": uuid.uuid4().hex[:8],
            "guild_id": interaction.guild.id,
            "channel_id": interaction.channel_id,
            "user_id": interaction.user.id,
            "time": time,
            "message": message[:1800],
            "created_at": now.isoformat(),
            "created_date": now.date().isoformat(),
            "created_time": now.strftime("%H:%M"),
        }

        await self.bot.store.add_alarm(alarm)
        await interaction.response.send_message(
            f"Đã đặt báo thức `{time}` giờ Việt Nam.\n"
            f"ID: `{alarm['id']}`\n"
            f"Nội dung: **{alarm['message']}**",
            ephemeral=True,
        )

    @app_commands.command(name="alarms", description="Xem báo thức bạn đã đặt.")
    async def alarms(self, interaction: discord.Interaction) -> None:
        snapshot = await self.bot.store.snapshot()
        user_alarms = [
            alarm
            for alarm in snapshot.get("alarms", [])
            if int(alarm["user_id"]) == interaction.user.id
            and int(alarm.get("guild_id", 0)) == (interaction.guild.id if interaction.guild else 0)
        ]

        if not user_alarms:
            await interaction.response.send_message("Bạn chưa có báo thức nào.", ephemeral=True)
            return

        lines = [
            f"`{alarm['id']}` - `{alarm['time']}` - {alarm['message']}"
            for alarm in user_alarms[:15]
        ]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @app_commands.command(name="cancelalarm", description="Xóa báo thức theo ID.")
    @app_commands.describe(alarm_id="ID nhận được khi dùng /setalarm")
    async def cancelalarm(self, interaction: discord.Interaction, alarm_id: str) -> None:
        snapshot = await self.bot.store.snapshot()
        alarm = next(
            (
                item
                for item in snapshot.get("alarms", [])
                if item["id"] == alarm_id and int(item["user_id"]) == interaction.user.id
            ),
            None,
        )
        if not alarm:
            await interaction.response.send_message("Không tìm thấy báo thức của bạn với ID này.", ephemeral=True)
            return

        await self.bot.store.remove_alarm(alarm_id)
        await interaction.response.send_message(f"Đã xóa báo thức `{alarm_id}`.", ephemeral=True)


async def setup(bot: VoiceAlarmBot) -> None:
    await bot.add_cog(AlarmCog(bot))
