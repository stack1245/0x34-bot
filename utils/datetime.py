from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo


INPUT_FORMATS: tuple[str, ...] = (
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y/%m/%d %H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%dT%H:%M:%S",
)


def parse_datetime(value: str, timezone_name: str) -> datetime:
    """사용자가 입력한 날짜/시간 문자열을 timezone이 있는 datetime으로 변환합니다."""
    text = value.strip()
    tzinfo = ZoneInfo(timezone_name)

    for input_format in INPUT_FORMATS:
        try:
            parsed = datetime.strptime(text, input_format)
        except ValueError:
            continue
        return parsed.replace(tzinfo=tzinfo)

    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("날짜 형식은 `2026-07-01 19:00`처럼 입력해 주세요.") from exc

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=tzinfo)
    return parsed.astimezone(tzinfo)


def now_utc_iso() -> str:
    """DB에 저장하기 좋은 UTC ISO 문자열을 만듭니다."""
    return datetime.now(timezone.utc).isoformat()


def to_storage_iso(value: datetime) -> str:
    """어떤 timezone의 datetime이든 UTC ISO 문자열로 통일해 저장합니다."""
    return value.astimezone(timezone.utc).isoformat()


def from_storage_iso(value: str, timezone_name: str) -> datetime:
    """DB에서 꺼낸 UTC ISO 문자열을 팀의 기본 timezone으로 변환합니다."""
    return datetime.fromisoformat(value).astimezone(ZoneInfo(timezone_name))


def format_discord_timestamp(value: datetime, style: str = "F") -> str:
    """Discord 클라이언트가 사용자 로컬 시간으로 보여주는 timestamp 문법을 만듭니다."""
    unix_timestamp = int(value.timestamp())
    return f"<t:{unix_timestamp}:{style}>"