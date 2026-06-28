from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


def _optional_int(value: str | None) -> int | None:
    """환경 변수가 비어 있으면 None, 값이 있으면 int로 변환합니다."""
    if value is None or value.strip() == "":
        return None
    return int(value)


def _env_flag(name: str, default: bool) -> bool:
    """Railway Variables처럼 문자열로 들어오는 true/false 값을 bool로 바꿉니다."""
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


@dataclass(frozen=True)
class Settings:
    """봇 전체에서 공유하는 설정 값입니다."""

    token: str
    guild_id: int | None
    database_path: str
    timezone: str
    schedule_channel_id: int | None
    tournament_channel_id: int | None
    recruitment_channel_id: int | None
    enable_server_events: bool
    sync_commands: bool


def load_settings() -> Settings:
    """`.env`와 실제 환경 변수를 읽어 Settings 객체를 만듭니다."""
    load_dotenv()

    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token:
        raise RuntimeError("DISCORD_TOKEN 환경 변수가 필요합니다. .env 또는 Railway Variables를 확인하세요.")

    return Settings(
        token=token,
        guild_id=_optional_int(os.getenv("GUILD_ID")),
        database_path=os.getenv("DATABASE_PATH", "data/0x34.sqlite3"),
        timezone=os.getenv("TIMEZONE", "Asia/Seoul"),
        schedule_channel_id=_optional_int(os.getenv("SCHEDULE_CHANNEL_ID")),
        tournament_channel_id=_optional_int(os.getenv("TOURNAMENT_CHANNEL_ID")),
        recruitment_channel_id=_optional_int(os.getenv("RECRUITMENT_CHANNEL_ID")),
        enable_server_events=_env_flag("ENABLE_SERVER_EVENTS", True),
        sync_commands=_env_flag("SYNC_COMMANDS", True),
    )