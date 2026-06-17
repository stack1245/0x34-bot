from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable

import discord

from core import config
from database.connection import initialize_database

logger = logging.getLogger(__name__)

EXPLICIT_EXTENSION_NAMES: tuple[str, ...] = ("cogs.schedule",)


def _suppress_noisy_discord_logs() -> None:
    for logger_name in (
        "discord",
        "discord.client",
        "discord.gateway",
        "discord.http",
        "discord.state",
        "discord.player",
        "discord.voice_client",
        "discord.voice_state",
        "websockets",
        "asyncio",
    ):
        logging.getLogger(logger_name).setLevel(logging.ERROR)


class ZeroXThirtyFourBot(discord.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.guilds = True

        super().__init__(intents=intents)
        self._ready_announced = False

        _suppress_noisy_discord_logs()
        initialize_database()
        self._load_extensions()

    async def on_ready(self) -> None:
        if self.user is None or self._ready_announced:
            return

        self._ready_announced = True
        logger.critical(
            "%s online | account=%s (%s)",
            config.BOT_NAME,
            self.user,
            self.user.id,
        )

    def _load_extensions(self) -> None:
        for extension_name in self._iter_extension_names():
            if extension_name in self.extensions:
                continue

            self.load_extension(extension_name)

    def _iter_extension_names(self) -> Iterable[str]:
        yielded: set[str] = set()
        for extension_name in EXPLICIT_EXTENSION_NAMES:
            if extension_name in yielded:
                continue

            yielded.add(extension_name)
            yield extension_name

        for entry in sorted(
            Path(config.COGS_DIR).iterdir(), key=lambda path: path.name
        ):
            if entry.name.startswith("_"):
                continue

            if (
                entry.is_file()
                and entry.suffix == ".py"
                and entry.name != "__init__.py"
            ):
                extension_name = f"{config.COG_PACKAGE_NAME}.{entry.stem}"
                if extension_name in yielded:
                    continue

                yielded.add(extension_name)
                yield extension_name
                continue

            if entry.is_dir() and (entry / "__init__.py").exists():
                extension_name = f"{config.COG_PACKAGE_NAME}.{entry.name}"
                if extension_name in yielded:
                    continue

                yielded.add(extension_name)
                yield extension_name
