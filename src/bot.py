from __future__ import annotations

import logging

import discord
from discord.ext import commands

from src.config import STATE_FILE, load_settings
from src.storage import JsonStore


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)


class VoiceAlarmBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.guilds = True
        intents.voice_states = True

        super().__init__(command_prefix=commands.when_mentioned, intents=intents)
        self.settings = load_settings()
        self.store = JsonStore(STATE_FILE)
        self.initial_extensions = (
            "src.cogs.music",
            "src.cogs.alarms",
        )

    async def setup_hook(self) -> None:
        await self.store.load()
        for extension in self.initial_extensions:
            await self.load_extension(extension)

        if self.settings.dev_guild_id:
            guild = discord.Object(id=self.settings.dev_guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            logging.info("Synced %s slash commands to dev guild %s", len(synced), guild.id)
        else:
            synced = await self.tree.sync()
            logging.info("Synced %s global slash commands", len(synced))

    async def on_ready(self) -> None:
        logging.info("Logged in as %s (%s)", self.user, self.user.id if self.user else "?")
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.listening,
                name="/play and /setalarm",
            )
        )


def main() -> None:
    bot = VoiceAlarmBot()
    if not bot.settings.token:
        raise RuntimeError("Missing DISCORD_TOKEN. Copy .env.example to .env and fill it in.")

    bot.run(bot.settings.token)


if __name__ == "__main__":
    main()
