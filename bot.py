from __future__ import annotations

import asyncio
import logging

import discord
from discord.ext import commands

from config import Settings, load_settings
from utils.database import Database


# Cog는 기능 단위 모듈입니다. 새 기능을 추가할 때 여기에 모듈 경로만 더하면 됩니다.
EXTENSIONS: tuple[str, ...] = (
    "cogs.schedule",
    "cogs.tournament",
    "cogs.recruitment",
)


class Team0x34Bot(commands.Bot):
    """Team 0x34 봇의 설정, DB, Cog 로딩을 담당하는 Bot 클래스입니다."""

    def __init__(self, settings: Settings) -> None:
        # Slash Command, Button, Modal만 사용하므로 message_content 같은 특권 인텐트는 켜지 않습니다.
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)

        self.settings = settings
        self.database = Database(settings.database_path)

    async def setup_hook(self) -> None:
        """Discord에 로그인한 직후, on_ready보다 먼저 실행되는 초기화 지점입니다."""
        await self.database.connect()

        for extension in EXTENSIONS:
            await self.load_extension(extension)

        # 개발 중에는 GUILD_ID를 지정하면 커맨드가 거의 즉시 테스트 서버에 반영됩니다.
        if self.settings.sync_commands:
            if self.settings.guild_id is not None:
                guild = discord.Object(id=self.settings.guild_id)
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                logging.info("Synced %s guild commands to %s", len(synced), self.settings.guild_id)
            else:
                synced = await self.tree.sync()
                logging.info("Synced %s global commands", len(synced))

    async def close(self) -> None:
        """봇 종료 시 SQLite 연결도 같이 정리합니다."""
        await self.database.close()
        await super().close()

    async def on_ready(self) -> None:
        """봇이 Discord Gateway에 완전히 연결되면 현재 계정을 로그에 남깁니다."""
        if self.user is None:
            logging.info("Bot is ready")
            return
        logging.info("Logged in as %s (%s)", self.user, self.user.id)


async def main() -> None:
    """설정을 읽고 봇을 실행하는 진입점입니다."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()

    async with Team0x34Bot(settings) as bot:
        await bot.start(settings.token)


if __name__ == "__main__":
    asyncio.run(main())