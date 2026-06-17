from __future__ import annotations

import logging
import os
import warnings
from pathlib import Path
from typing import Final

warnings.filterwarnings(
    "ignore",
    category=SyntaxWarning,
)

import discord
from dotenv import load_dotenv

BASE_DIR: Final[Path] = Path(__file__).resolve().parent.parent
COGS_DIR: Final[Path] = BASE_DIR / "cogs"
DATA_DIR: Final[Path] = BASE_DIR / "data"
DATABASE_PATH: Final[Path] = DATA_DIR / "settings.db"
COG_PACKAGE_NAME: Final[str] = "cogs"
ENV_FILE: Final[Path] = BASE_DIR / ".env"
TOKEN_ENV_NAME: Final[str] = "TOKEN"
LEGACY_TOKEN_ENV_NAME: Final[str] = "DISCORD_BOT_TOKEN"
DEFAULT_LOG_LEVEL: Final[str] = os.getenv("LOG_LEVEL", "WARNING").upper()
BOT_NAME: Final[str] = "Team 0x34"
EMBED_COLOR_RGB: Final[int] = 0x00E5FF
CONTEST_EMBED_COLOR: Final[int] = EMBED_COLOR_RGB
EMBED_FOOTER_TEXT: Final[str] = f"{BOT_NAME}"
EMBED_AUTHOR_TEXT: Final[str] = f"{BOT_NAME}"
MAX_LINK_BUTTONS: Final[int] = 5
MAX_EMBED_DESCRIPTION_LENGTH: Final[int] = 3800
LOG_FORMAT: Final[str] = "%(asctime)s | %(levelname)s | %(message)s"
LOG_DATE_FORMAT: Final[str] = "%Y-%m-%d %H:%M:%S"

load_dotenv(ENV_FILE)

TOKEN: Final[str] = (
    os.getenv(TOKEN_ENV_NAME, "").strip()
    or os.getenv(LEGACY_TOKEN_ENV_NAME, "").strip()
)


def configure_logging() -> None:
    target_level = getattr(logging, DEFAULT_LOG_LEVEL, logging.WARNING)
    root_logger = logging.getLogger()

    formatter = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT)
    if not root_logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(formatter)
        root_logger.addHandler(handler)
    else:
        for handler in root_logger.handlers:
            handler.setFormatter(formatter)

    root_logger.setLevel(target_level)

    noisy_logger_levels = {
        "discord": logging.WARNING,
        "discord.client": logging.WARNING,
        "discord.gateway": logging.ERROR,
        "discord.http": logging.ERROR,
        "discord.state": logging.ERROR,
        "discord.ext": logging.WARNING,
        "websockets": logging.ERROR,
        "asyncio": logging.WARNING,
        "aiosqlite": logging.ERROR,
        "sqlite3": logging.ERROR,
    }
    for logger_name, level in noisy_logger_levels.items():
        logger_instance = logging.getLogger(logger_name)
        logger_instance.setLevel(level)
        logger_instance.propagate = True

    app_logger_levels = {
        "main": logging.ERROR,
        "core": logging.ERROR,
        "cogs": logging.ERROR,
        "database": logging.ERROR,
        "views": logging.ERROR,
    }
    for logger_name, level in app_logger_levels.items():
        logger_instance = logging.getLogger(logger_name)
        logger_instance.setLevel(level)
        logger_instance.propagate = True


def build_theme_embed(
    *,
    title: str,
    description: str,
) -> discord.Embed:
    embed = discord.Embed(
        title=title,
        description=description[:MAX_EMBED_DESCRIPTION_LENGTH],
        color=discord.Colour(EMBED_COLOR_RGB),
        timestamp=discord.utils.utcnow(),
    )
    embed.set_author(name=EMBED_AUTHOR_TEXT)
    embed.set_footer(text=EMBED_FOOTER_TEXT)
    return embed


def get_token() -> str:
    if not TOKEN:
        raise RuntimeError(
            f"{ENV_FILE.name} 파일에 {TOKEN_ENV_NAME}=... 값을 설정해주세요."
        )

    return TOKEN
