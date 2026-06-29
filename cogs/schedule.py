from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta

import discord
from discord import app_commands
from discord.ext import commands
from google.api_core import exceptions
import google.generativeai as genai

from utils.ai_input import CONVERSATIONAL_INPUT_INSTRUCTION, ScrapingError, prepare_conversational_source_text
from utils.datetime import (
    fix_past_year,
    format_discord_timestamp,
    from_storage_iso,
    get_current_time_context,
    now_utc_iso,
    parse_datetime,
    to_storage_iso,
)
from utils.embeds import SUCCESS_COLOR, WARNING_COLOR, base_embed


MAX_SCHEDULE_SOURCE_TEXT_LENGTH = 12000
SCRAPING_ERROR_MESSAGE = "웹페이지 내용을 불러오지 못했습니다. 사이트 링크 대신 상세 텍스트를 직접 입력해 주세요."
GEMINI_RATE_LIMIT_MESSAGE = "⚠️ 봇이 너무 많은 요청을 처리하고 있습니다. 1분 뒤에 다시 시도해 주세요."
SCHEDULE_BOARD_STATE_KEY = "schedule_board"
DISCORD_EVENT_SYNC_TAGS = ("[예선]", "[본선]")
DISCORD_EVENT_SYNC_KEYWORDS = ("예선", "본선")
SCHEDULE_END_LINE_PATTERN = re.compile(r"^\*\*종료\*\*\s*<t:(\d+)(?::[tTdDfFR])?>.*$", re.MULTILINE)
SCHEDULE_LOCATION_LINE_PATTERN = re.compile(r"^\*\*장소\*\*\s*(.+)$", re.MULTILINE)
SCHEDULE_METADATA_LINE_PATTERN = re.compile(r"^\*\*(?:종료|장소)\*\*.*(?:\n|$)", re.MULTILINE)


SCHEDULE_GENERATION_PROMPT = """
제공된 텍스트에서 '참가 신청', '예선', '본선' 등 유의미한 모든 일정을 찾아내어 JSON 배열(Array) 형태로 반환해라.
각 일정의 제목(title) 앞에는 성격을 나타내는 태그(예: [참가신청], [예선], [본선])를 붙여라.
반드시 아래 JSON 스키마의 배열만 반환해라.
[
    {
        "title": "[태그] 행사/대회 이름",
        "start_time": "YYYY-MM-DD HH:MM:SS",
        "end_time": "YYYY-MM-DD HH:MM:SS",
        "location": "장소",
        "description": "해당 세부 일정에 대한 간단한 요약"
    }
]
텍스트에 명시되지 않은 일정은 추측하지 말고 제외해라.
날짜가 불완전해서 YYYY-MM-DD HH:MM:SS 형식으로 확정할 수 없는 항목도 제외해라.
""".strip()


def build_schedule_generation_prompt() -> str:
    """Gemini 일정 생성에 현재 한국 시간과 날짜 규칙을 주입합니다."""
    return f"""
{get_current_time_context()}
위 제공된 '현재 시간'을 기준으로 날짜를 계산해라. 본문에 연도가 생략되어 있다면 무조건 현재 연도를 사용하고, 절대로 지나간 과거 연도로 작성하지 마라.
{CONVERSATIONAL_INPUT_INSTRUCTION}

{SCHEDULE_GENERATION_PROMPT}
""".strip()


def build_schedule_datetime_parse_prompt() -> str:
    """수동 일정 추가 Modal의 자연어 날짜/시간을 Gemini가 JSON으로 변환하게 합니다."""
    return f"""
{get_current_time_context()}
사용자가 입력한 자유 형식의 날짜/시간 텍스트를 분석하여 DB에 저장할 수 있는 정확한 시작 시간과 종료 시간으로 변환해라.
본문에 연도가 생략되어 있다면 무조건 현재 연도를 사용하고, 절대로 지나간 과거 연도로 작성하지 마라.
제목과 내용은 날짜/시간 해석을 위한 추가 문맥으로만 사용해라.
반드시 아래 JSON 스키마의 객체 하나만 반환해라.
{{
    "start_time": "YYYY-MM-DD HH:MM:SS",
    "end_time": "YYYY-MM-DD HH:MM:SS"
}}
종료 시간이 명확하지 않으면 end_time은 start_time과 동일하게 설정해라.
날짜나 시간을 확정할 수 없으면 빈 문자열을 넣지 말고 JSON 파싱이 가능한 가장 엄격한 추론 결과를 반환해라.
""".strip()


class ScheduleModal(discord.ui.Modal, title="일정 추가"):
    """Slash Command에서 띄우는 입력 창입니다. Discord Modal은 짧은 폼 입력에 적합합니다."""

    title_input = discord.ui.TextInput(label="제목", placeholder="예: Team 0x34 정기 회의", max_length=100)
    starts_at_input = discord.ui.TextInput(label="날짜/시간", placeholder="예: 2026-07-01 19:00", max_length=40)
    body_input = discord.ui.TextInput(
        label="내용",
        placeholder="회의 안건, 준비물, 장소 등을 적어 주세요.",
        style=discord.TextStyle.long,
        max_length=1000,
    )

    def __init__(self, cog: "ScheduleCog") -> None:
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        """사용자가 Modal을 제출하면 일정을 DB에 저장하고 필요하면 서버 이벤트도 만듭니다."""
        await interaction.response.defer(ephemeral=True, thinking=True)

        if interaction.guild is None:
            await interaction.followup.send("서버 안에서만 일정을 추가할 수 있습니다.", ephemeral=True)
            return

        try:
            starts_at, ends_at = await self.cog.parse_manual_schedule_datetimes(
                title=str(self.title_input.value),
                date_text=str(self.starts_at_input.value),
                body=str(self.body_input.value),
            )
        except exceptions.ResourceExhausted:
            await interaction.followup.send(GEMINI_RATE_LIMIT_MESSAGE, ephemeral=True)
            return
        except Exception:
            logging.exception("Gemini manual schedule datetime parsing failed")
            await interaction.followup.send("⚠️ 날짜를 이해하지 못했습니다. 조금 더 명확하게 적어주세요.", ephemeral=True)
            return

        body = str(self.body_input.value).strip()
        stored_body = body
        if ends_at != starts_at:
            stored_body = f"**종료** {self.cog.format_schedule_time(ends_at)}\n{body}".strip()

        cursor = await self.cog.bot.database.execute(
            """
            INSERT INTO schedules (guild_id, title, starts_at, body, created_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                interaction.guild.id,
                str(self.title_input.value),
                to_storage_iso(starts_at),
                stored_body,
                interaction.user.id,
                now_utc_iso(),
            ),
        )

        event_id: int | None = None
        event_status = "서버 이벤트 생성은 비활성화되어 있습니다."
        if self.cog.bot.settings.enable_server_events:
            event_id, event_status = await self.cog.create_server_event(
                interaction.guild,
                str(self.title_input.value),
                starts_at,
                body,
                end_time=ends_at if ends_at > starts_at else None,
            )

        if event_id is not None:
            await self.cog.bot.database.execute(
                "UPDATE schedules SET event_id = ? WHERE id = ?",
                (event_id, cursor.lastrowid),
            )

        await self.cog.update_schedule_board()

        await interaction.followup.send(f"✅ 성공적으로 일정이 추가되었습니다.\n{event_status}", ephemeral=True)


class ScheduleDeleteSelect(discord.ui.Select):
    """등록된 일정 목록을 드롭다운으로 보여주고 선택된 일정을 삭제합니다."""

    def __init__(self, cog: "ScheduleCog", rows: list) -> None:
        self.cog = cog
        options: list[discord.SelectOption] = []

        for row in rows:
            starts_at = from_storage_iso(row["starts_at"], cog.bot.settings.timezone)
            label = str(row["title"])[:100]
            description = format_discord_timestamp(starts_at, "F")
            options.append(
                discord.SelectOption(
                    label=label,
                    description=description,
                    value=str(row["id"]),
                )
            )

        super().__init__(
            placeholder="삭제할 일정을 선택하세요.",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        """선택한 schedule id로 DB 레코드를 삭제하고 View를 제거합니다."""
        await interaction.response.defer(ephemeral=True)

        if interaction.guild is None:
            await interaction.edit_original_response(content="서버 안에서만 일정을 삭제할 수 있습니다.", embed=None, view=None)
            return

        schedule_id = int(self.values[0])
        row = await self.cog.bot.database.fetch_one(
            """
            SELECT * FROM schedules
            WHERE id = ? AND guild_id = ?
            """,
            (schedule_id, interaction.guild.id),
        )
        if row is None:
            await interaction.edit_original_response(content="이미 삭제되었거나 찾을 수 없는 일정입니다.", embed=None, view=None)
            return

        event_notice = await self.cog.delete_linked_server_event(interaction.guild, row["event_id"])
        await self.cog.bot.database.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))
        await self.cog.update_schedule_board()

        message = "✅ 성공적으로 일정이 삭제되었습니다."
        if event_notice:
            message += f"\n{event_notice}"
        await interaction.edit_original_response(content=message, embed=None, view=None)


class ScheduleDeleteView(discord.ui.View):
    """일정 삭제 Select를 담는 Ephemeral View입니다."""

    def __init__(self, cog: "ScheduleCog", rows: list, user_id: int) -> None:
        super().__init__(timeout=120)
        self.user_id = user_id
        self.add_item(ScheduleDeleteSelect(cog, rows))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.user_id:
            return True
        await interaction.response.send_message("이 일정 삭제 메뉴는 명령어를 실행한 사람만 사용할 수 있습니다.", ephemeral=True)
        return False


class ScheduleEditModal(discord.ui.Modal, title="일정 수정"):
    """기존 일정 값을 채운 상태로 열리는 수정 Modal입니다."""

    def __init__(self, cog: "ScheduleCog", row, source_message: discord.Message | None, user_id: int) -> None:
        super().__init__()
        self.cog = cog
        self.schedule_id = int(row["id"])
        self.event_id = row["event_id"]
        self.source_message = source_message
        self.user_id = user_id

        starts_at = from_storage_iso(row["starts_at"], cog.bot.settings.timezone)
        self.title_input = discord.ui.TextInput(
            label="제목",
            placeholder="예: Team 0x34 정기 회의",
            default=str(row["title"])[:100],
            max_length=100,
        )
        self.starts_at_input = discord.ui.TextInput(
            label="날짜/시간",
            placeholder="예: 2026-07-01 19:00",
            default=starts_at.isoformat(sep=" ", timespec="minutes"),
            max_length=40,
        )
        self.body_input = discord.ui.TextInput(
            label="내용",
            placeholder="회의 안건, 준비물, 장소 등을 적어 주세요.",
            default=str(row["body"])[:1000],
            style=discord.TextStyle.long,
            max_length=1000,
        )
        self.add_item(self.title_input)
        self.add_item(self.starts_at_input)
        self.add_item(self.body_input)

    async def disable_source_view(self) -> None:
        if self.source_message is None:
            return
        try:
            await self.source_message.edit(view=None)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("서버 안에서만 일정을 수정할 수 있습니다.", ephemeral=True)
            return
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("이 일정 수정 창은 명령어를 실행한 사람만 제출할 수 있습니다.", ephemeral=True)
            return

        try:
            starts_at = parse_datetime(str(self.starts_at_input.value), self.cog.bot.settings.timezone)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        row = await self.cog.bot.database.fetch_one(
            """
            SELECT * FROM schedules
            WHERE id = ? AND guild_id = ?
            """,
            (self.schedule_id, interaction.guild.id),
        )
        if row is None:
            await self.disable_source_view()
            await interaction.followup.send("수정할 일정을 찾을 수 없습니다.", ephemeral=True)
            return

        title = str(self.title_input.value).strip()
        body = str(self.body_input.value).strip()
        await self.cog.bot.database.execute(
            """
            UPDATE schedules
            SET title = ?, starts_at = ?, body = ?
            WHERE id = ? AND guild_id = ?
            """,
            (title, to_storage_iso(starts_at), body, self.schedule_id, interaction.guild.id),
        )

        event_notice = await self.cog.edit_linked_server_event(interaction.guild, row["event_id"], title, starts_at, body)
        await self.cog.update_schedule_board()
        await self.disable_source_view()

        message = "✅ 성공적으로 수정되었습니다."
        if event_notice:
            message += f"\n{event_notice}"
        await interaction.followup.send(message, ephemeral=True)


class ScheduleEditSelect(discord.ui.Select):
    """수정할 일정을 선택하는 드롭다운입니다."""

    def __init__(self, cog: "ScheduleCog", rows: list) -> None:
        self.cog = cog
        options: list[discord.SelectOption] = []
        for row in rows:
            starts_at = from_storage_iso(row["starts_at"], cog.bot.settings.timezone)
            options.append(
                discord.SelectOption(
                    label=str(row["title"])[:100],
                    description=format_discord_timestamp(starts_at, "F"),
                    value=str(row["id"]),
                )
            )

        super().__init__(
            placeholder="수정할 일정을 선택하세요.",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.edit_message(content="서버 안에서만 일정을 수정할 수 있습니다.", embed=None, view=None)
            return

        schedule_id = int(self.values[0])
        row = await self.cog.bot.database.fetch_one(
            """
            SELECT * FROM schedules
            WHERE id = ? AND guild_id = ?
            """,
            (schedule_id, interaction.guild.id),
        )
        if row is None:
            await interaction.response.edit_message(content="이미 삭제되었거나 찾을 수 없는 일정입니다.", embed=None, view=None)
            return

        await interaction.response.send_modal(ScheduleEditModal(self.cog, row, interaction.message, interaction.user.id))


class ScheduleEditView(discord.ui.View):
    """일정 수정 Select를 담는 Ephemeral View입니다."""

    def __init__(self, cog: "ScheduleCog", rows: list, user_id: int) -> None:
        super().__init__(timeout=120)
        self.user_id = user_id
        self.add_item(ScheduleEditSelect(cog, rows))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.user_id:
            return True
        await interaction.response.send_message("이 일정 수정 메뉴는 명령어를 실행한 사람만 사용할 수 있습니다.", ephemeral=True)
        return False


class ScheduleCog(commands.Cog):
    """일정 조회와 일정 추가 기능을 담당하는 Cog입니다."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def create_server_event(
        self,
        guild: discord.Guild,
        title: str,
        starts_at,
        description: str,
        *,
        end_time=None,
        location: str = "Discord",
    ) -> tuple[int | None, str]:
        """Discord 서버 이벤트 API를 호출합니다. 권한이 없으면 실패 메시지만 돌려줍니다."""
        try:
            event = await guild.create_scheduled_event(
                name=title,
                start_time=starts_at,
                end_time=end_time or starts_at + timedelta(hours=1),
                description=description[:1000],
                entity_type=discord.EntityType.external,
                privacy_level=discord.PrivacyLevel.guild_only,
                location=(location or "Discord")[:100],
                reason="Team 0x34 일정 등록",
            )
        except discord.Forbidden:
            return None, "서버 이벤트 권한이 없어 DB에만 저장했습니다."
        except discord.HTTPException as exc:
            return None, f"서버 이벤트 생성에 실패해 DB에만 저장했습니다: {exc.text}"
        return event.id, "Discord 서버 이벤트도 함께 생성했습니다."

    def should_sync_discord_event(self, schedule_row) -> bool:
        """대시보드 갱신 중 공식 서버 이벤트로 보정할 가치가 있는 일정인지 판단합니다."""
        if schedule_row["event_id"] is not None:
            return False

        title = str(schedule_row["title"])
        compact_title = title.replace(" ", "")
        return any(tag in title for tag in DISCORD_EVENT_SYNC_TAGS) or any(
            keyword.replace(" ", "") in compact_title for keyword in DISCORD_EVENT_SYNC_KEYWORDS
        )

    def parse_schedule_event_fields(self, schedule_row) -> tuple[datetime, datetime, str, str]:
        """schedules 행의 starts_at/body에서 Discord Scheduled Event 필드를 추출합니다."""
        starts_at = from_storage_iso(schedule_row["starts_at"], self.bot.settings.timezone)
        body = str(schedule_row["body"] or "")

        ends_at = self.extract_schedule_end_time(body, starts_at) or starts_at + timedelta(hours=1)
        if ends_at <= starts_at:
            ends_at = starts_at + timedelta(hours=1)

        location_match = SCHEDULE_LOCATION_LINE_PATTERN.search(body)
        location = location_match.group(1).strip() if location_match is not None else "Discord"
        if not location or location == "공개된 정보 없음":
            location = "Discord"

        description = SCHEDULE_METADATA_LINE_PATTERN.sub("", body).strip() or str(schedule_row["title"])
        return starts_at, ends_at, location[:100], description[:1000]

    def format_schedule_time(self, value: datetime) -> str:
        """절대 시간과 상대 시간을 함께 표시하는 Discord timestamp 문자열을 만듭니다."""
        return f"{format_discord_timestamp(value, 'F')} ({format_discord_timestamp(value, 'R')})"

    def format_schedule_range(self, starts_at: datetime, ends_at: datetime | None = None) -> str:
        """일정 시작~종료 범위를 Discord timestamp 마크다운 한 줄로 표시합니다."""
        if ends_at is None or int(ends_at.timestamp()) == int(starts_at.timestamp()):
            return self.format_schedule_time(starts_at)
        return f"{self.format_schedule_time(starts_at)} ~ {self.format_schedule_time(ends_at)}"

    def extract_schedule_end_time(self, body: str, starts_at: datetime) -> datetime | None:
        """body에 저장된 종료 timestamp 메타 라인을 datetime으로 되살립니다."""
        end_match = SCHEDULE_END_LINE_PATTERN.search(body)
        if end_match is None:
            return None
        return datetime.fromtimestamp(int(end_match.group(1)), tz=starts_at.tzinfo)

    def clean_schedule_body_for_display(self, body: str) -> str:
        """대시보드/목록에서 별도 기간 라인과 중복되는 종료 메타 라인을 제거합니다."""
        return SCHEDULE_END_LINE_PATTERN.sub("", body).strip()

    async def find_existing_discord_event(self, guild: discord.Guild, title: str, starts_at: datetime) -> discord.ScheduledEvent | None:
        """DB event_id는 없지만 같은 이름/시작 시간의 서버 이벤트가 이미 있는지 확인합니다."""
        try:
            scheduled_events = await guild.fetch_scheduled_events()
        except discord.Forbidden as exc:
            logging.warning("Missing permission to fetch scheduled events for guild %s: %s", guild.id, exc)
            return None
        except discord.HTTPException as exc:
            logging.warning("Failed to fetch scheduled events for guild %s: %s", guild.id, exc)
            return None

        event_name = title[:100]
        for event in scheduled_events:
            event_start = getattr(event, "start_time", None)
            if event_start is None or event.name != event_name:
                continue
            try:
                if abs((event_start - starts_at).total_seconds()) < 60:
                    return event
            except TypeError:
                continue
        return None

    async def create_discord_event(self, schedule_row) -> int | None:
        """schedules 행을 기준으로 Discord 공식 Scheduled Event를 만들고 event_id를 DB에 저장합니다."""
        if not self.bot.settings.enable_server_events or schedule_row["event_id"] is not None:
            return None

        try:
            guild_id = int(schedule_row["guild_id"])
            schedule_id = int(schedule_row["id"])
        except (TypeError, ValueError):
            return None

        guild = self.bot.get_guild(guild_id)
        if guild is None:
            try:
                guild = await self.bot.fetch_guild(guild_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                logging.warning("Failed to resolve guild %s for schedule event sync: %s", guild_id, exc)
                return None

        title = str(schedule_row["title"])[:100]
        starts_at, ends_at, location, description = self.parse_schedule_event_fields(schedule_row)

        existing_event = await self.find_existing_discord_event(guild, title, starts_at)
        if existing_event is not None:
            await self.bot.database.execute(
                "UPDATE schedules SET event_id = ? WHERE id = ?",
                (existing_event.id, schedule_id),
            )
            return existing_event.id

        try:
            event = await guild.create_scheduled_event(
                name=title,
                start_time=starts_at,
                end_time=ends_at,
                description=description,
                entity_type=discord.EntityType.external,
                privacy_level=discord.PrivacyLevel.guild_only,
                location=location,
                reason="Team 0x34 일정 대시보드 서버 이벤트 동기화",
            )
        except discord.Forbidden as exc:
            logging.warning("Missing permission to create scheduled event for schedule %s: %s", schedule_id, exc)
            return None
        except discord.HTTPException as exc:
            logging.warning("Failed to create scheduled event for schedule %s: %s", schedule_id, exc)
            return None

        await self.bot.database.execute(
            "UPDATE schedules SET event_id = ? WHERE id = ?",
            (event.id, schedule_id),
        )
        return event.id

    async def resolve_schedule_channel(self, interaction: discord.Interaction) -> discord.abc.Messageable | None:
        """환경 변수에 일정 채널이 지정된 경우 공지할 채널을 찾습니다."""
        channel_id = self.bot.settings.schedule_channel_id
        if interaction.guild is None or channel_id is None:
            return None

        channel = interaction.guild.get_channel(channel_id)
        if channel is None:
            channel = await self.bot.fetch_channel(channel_id)
        if isinstance(channel, discord.abc.Messageable):
            return channel
        return None

    async def fetch_schedule_board_state(self):
        """DB에서 일정 대시보드 메시지 위치를 가져옵니다."""
        return await self.bot.database.fetch_one(
            """
            SELECT board_channel_id, board_message_id FROM dashboard_state
            WHERE name = ?
            """,
            (SCHEDULE_BOARD_STATE_KEY,),
        )

    async def save_schedule_board_state(self, channel_id: int, message_id: int) -> None:
        """일정 대시보드 메시지 위치를 DB에 저장하거나 덮어씁니다."""
        await self.bot.database.execute(
            """
            INSERT INTO dashboard_state (name, board_channel_id, board_message_id, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(name)
            DO UPDATE SET
                board_channel_id = excluded.board_channel_id,
                board_message_id = excluded.board_message_id,
                updated_at = excluded.updated_at
            """,
            (SCHEDULE_BOARD_STATE_KEY, channel_id, message_id, now_utc_iso()),
        )

    async def clear_schedule_board_state(self) -> None:
        """삭제된 채널/메시지를 가리키는 일정 대시보드 상태를 제거합니다."""
        await self.bot.database.execute(
            "DELETE FROM dashboard_state WHERE name = ?",
            (SCHEDULE_BOARD_STATE_KEY,),
        )

    async def resolve_schedule_board_channel(self, guild_id: int | None = None) -> discord.abc.Messageable | None:
        """새 일정 대시보드를 만들 채널을 찾습니다."""
        channel_id = self.bot.settings.schedule_channel_id
        if channel_id is not None:
            try:
                channel = self.bot.get_channel(channel_id)
                if channel is None:
                    channel = await self.bot.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                logging.warning("Failed to resolve configured schedule board channel %s: %s", channel_id, exc)
            else:
                if isinstance(channel, discord.abc.Messageable):
                    return channel

        if guild_id is None:
            return None

        guild = self.bot.get_guild(int(guild_id))
        if guild is None:
            try:
                guild = await self.bot.fetch_guild(int(guild_id))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                logging.warning("Failed to resolve guild for schedule board %s: %s", guild_id, exc)
                return None

        for channel in getattr(guild, "text_channels", []):
            if "일정" in channel.name:
                return channel

        system_channel = getattr(guild, "system_channel", None)
        if isinstance(system_channel, discord.abc.Messageable):
            return system_channel
        return None

    def build_schedule_board_embed(self, rows: list) -> discord.Embed:
        """전체 일정을 하나의 대시보드 Embed description으로 조립합니다."""
        if not rows:
            return base_embed("Team 0x34 일정 대시보드", "등록된 일정이 없습니다.", color=WARNING_COLOR)

        lines: list[str] = []
        for row in rows:
            starts_at = from_storage_iso(row["starts_at"], self.bot.settings.timezone)
            body = str(row["body"] or "").strip()
            ends_at = self.extract_schedule_end_time(body, starts_at)
            details = self.clean_schedule_body_for_display(body)
            line = f"• **{row['title']}**\n  {self.format_schedule_range(starts_at, ends_at)}"
            if details:
                line += f"\n  {details[:300]}"
            if row["event_id"]:
                line += f"\n  서버 이벤트 ID: `{row['event_id']}`"
            lines.append(line)

        description = "\n\n".join(lines)
        if len(description) > 4000:
            description = f"{description[:3997].rstrip()}..."

        embed = base_embed("Team 0x34 일정 대시보드", description, color=SUCCESS_COLOR)
        embed.set_footer(text="일정이 추가, 수정, 삭제될 때 자동으로 갱신됩니다.")
        return embed

    async def update_schedule_board(self) -> None:
        """DB에 저장된 한 개의 일정 대시보드 메시지를 생성하거나 갱신합니다."""
        rows = await self.bot.database.fetch_all(
            """
            SELECT * FROM schedules
            ORDER BY starts_at ASC
            """,
        )
        synced_event = False
        for row in rows:
            if self.should_sync_discord_event(row):
                event_id = await self.create_discord_event(row)
                if event_id is not None:
                    synced_event = True

        if synced_event:
            rows = await self.bot.database.fetch_all(
                """
                SELECT * FROM schedules
                ORDER BY starts_at ASC
                """,
            )

        embed = self.build_schedule_board_embed(rows)
        state = await self.fetch_schedule_board_state()

        if state is not None and state["board_channel_id"] is not None and state["board_message_id"] is not None:
            try:
                channel_id = int(state["board_channel_id"])
                message_id = int(state["board_message_id"])
                channel = self.bot.get_channel(channel_id)
                if channel is None:
                    channel = await self.bot.fetch_channel(channel_id)
                if not hasattr(channel, "fetch_message"):
                    await self.clear_schedule_board_state()
                else:
                    message = await channel.fetch_message(message_id)
                    await message.edit(embed=embed)
                    return
            except (TypeError, ValueError):
                await self.clear_schedule_board_state()
            except discord.NotFound:
                await self.clear_schedule_board_state()
            except discord.Forbidden as exc:
                logging.warning("Missing permission to update schedule board: %s", exc)
                return
            except discord.HTTPException as exc:
                logging.warning("Failed to update schedule board message: %s", exc)
                return

        guild_id = int(rows[0]["guild_id"]) if rows else None
        channel = await self.resolve_schedule_board_channel(guild_id)
        if channel is None:
            logging.warning("Schedule board channel is not configured and no default channel was found.")
            return

        try:
            message = await channel.send(embed=embed)
        except discord.NotFound:
            await self.clear_schedule_board_state()
            return
        except discord.Forbidden as exc:
            logging.warning("Missing permission to create schedule board: %s", exc)
            return
        except discord.HTTPException as exc:
            logging.warning("Failed to create schedule board message: %s", exc)
            return

        await self.save_schedule_board_state(message.channel.id, message.id)

    async def delete_linked_server_event(self, guild: discord.Guild, event_id) -> str | None:
        """DB 일정과 연결된 Discord 서버 이벤트가 있으면 함께 삭제합니다."""
        if event_id is None:
            return None

        try:
            event = await guild.fetch_scheduled_event(int(event_id))
            await event.delete(reason="Team 0x34 일정 삭제")
        except (TypeError, ValueError):
            return "저장된 서버 이벤트 ID가 올바르지 않아 Discord 이벤트는 삭제하지 못했습니다."
        except discord.NotFound:
            return "연결된 서버 이벤트는 이미 삭제되어 있었습니다."
        except discord.Forbidden:
            return "서버 이벤트 삭제 권한이 없어 Discord 이벤트는 삭제하지 못했습니다."
        except discord.HTTPException as exc:
            return f"서버 이벤트 삭제 중 오류가 발생했습니다: {exc.text}"
        return "연결된 Discord 서버 이벤트도 함께 삭제했습니다."

    async def edit_linked_server_event(self, guild: discord.Guild, event_id, title: str, starts_at, body: str) -> str | None:
        """DB 일정과 연결된 Discord 서버 이벤트가 있으면 수정 내용도 반영합니다."""
        if event_id is None:
            return None

        try:
            event = await guild.fetch_scheduled_event(int(event_id))
            await event.edit(
                name=title[:100],
                start_time=starts_at,
                end_time=starts_at + timedelta(hours=1),
                description=body[:1000],
                reason="Team 0x34 일정 수정",
            )
        except (TypeError, ValueError):
            return "저장된 서버 이벤트 ID가 올바르지 않아 Discord 이벤트는 수정하지 못했습니다."
        except discord.NotFound:
            return "연결된 서버 이벤트를 찾을 수 없어 Discord 이벤트는 수정하지 못했습니다."
        except discord.Forbidden:
            return "서버 이벤트 수정 권한이 없어 Discord 이벤트는 수정하지 못했습니다."
        except discord.HTTPException as exc:
            return f"서버 이벤트 수정 중 오류가 발생했습니다: {exc.text}"
        return "연결된 Discord 서버 이벤트도 함께 수정했습니다."

    def parse_generated_schedules(self, raw_text: str) -> list[dict]:
        """Gemini가 반환한 JSON 배열을 Python 리스트로 변환합니다."""
        text = raw_text.strip()
        if text.startswith("```"):
            text = text.strip("`")
            text = text.removeprefix("json").strip()

        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            array_match = text[text.find("[") : text.rfind("]") + 1]
            try:
                payload = json.loads(array_match)
            except json.JSONDecodeError:
                return []

        if not isinstance(payload, list):
            return []
        return [item for item in payload if isinstance(item, dict)]

    def generate_schedule_json_sync(self, source_text: str) -> str:
        """Gemini 동기 SDK 호출을 별도 스레드에서 실행하기 위한 함수입니다."""
        if self.bot.settings.gemini_api_key is None:
            raise RuntimeError("GEMINI_API_KEY가 설정되어 있지 않습니다. .env 또는 Railway Variables에 추가해 주세요.")

        genai.configure(api_key=self.bot.settings.gemini_api_key)
        model = genai.GenerativeModel(
            model_name=self.bot.settings.gemini_model,
            system_instruction=build_schedule_generation_prompt(),
        )
        response = model.generate_content(
            "다음은 사용자가 자유롭게 제공한 대화형 입력과 URL 크롤링 내용을 합친 원문입니다. "
            "사용자의 요청 의도와 어조를 유지하면서 등록할 수 있는 모든 일정을 JSON 배열로 추출해 주세요.\n\n"
            f"{source_text}",
            generation_config={
                "temperature": 0.2,
                "response_mime_type": "application/json",
            },
        )
        return str(getattr(response, "text", "") or "")

    async def generate_schedule_items(self, source_text: str) -> list[dict]:
        """Gemini 호출을 이벤트 루프 밖 스레드로 넘기고 JSON 배열을 파싱합니다."""
        raw_text = await asyncio.to_thread(self.generate_schedule_json_sync, source_text)
        return self.parse_generated_schedules(raw_text)

    async def prepare_schedule_source_text(self, target_info: str) -> str:
        """구어체 입력 원문과 URL 크롤링 결과를 일정 생성용 Gemini 입력으로 합칩니다."""
        return await prepare_conversational_source_text(
            target_info,
            max_length=MAX_SCHEDULE_SOURCE_TEXT_LENGTH,
            logger=logging.getLogger(__name__),
        )

    def parse_manual_schedule_datetime_payload(self, raw_text: str) -> tuple[datetime, datetime]:
        """Gemini가 반환한 start_time/end_time JSON을 datetime으로 변환하고 과거 연도를 보정합니다."""
        text = raw_text.strip()
        if text.startswith("```"):
            text = text.strip("`")
            text = text.removeprefix("json").strip()

        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            object_text = text[text.find("{") : text.rfind("}") + 1]
            payload = json.loads(object_text)

        if not isinstance(payload, dict):
            raise ValueError("Gemini datetime payload must be a JSON object")

        start_text = str(payload.get("start_time") or "").strip()
        end_text = str(payload.get("end_time") or start_text).strip()
        if not start_text or not end_text:
            raise ValueError("Gemini datetime payload is missing start_time or end_time")

        starts_at = fix_past_year(parse_datetime(start_text, self.bot.settings.timezone))
        ends_at = fix_past_year(parse_datetime(end_text, self.bot.settings.timezone))
        if ends_at < starts_at:
            raise ValueError("Gemini datetime payload has end_time before start_time")
        return starts_at, ends_at

    def generate_manual_schedule_datetime_json_sync(self, *, title: str, date_text: str, body: str) -> str:
        """수동 일정 Modal의 자유 형식 날짜/시간을 Gemini JSON 응답으로 변환합니다."""
        if self.bot.settings.gemini_api_key is None:
            raise RuntimeError("GEMINI_API_KEY가 설정되어 있지 않습니다. .env 또는 Railway Variables에 추가해 주세요.")

        genai.configure(api_key=self.bot.settings.gemini_api_key)
        model = genai.GenerativeModel(
            model_name=self.bot.settings.gemini_model,
            system_instruction=build_schedule_datetime_parse_prompt(),
        )
        response = model.generate_content(
            "다음 수동 일정 입력에서 날짜/시간을 해석해 start_time과 end_time JSON으로 변환해 주세요.\n\n"
            f"제목: {title}\n"
            f"날짜/시간 입력: {date_text}\n"
            f"내용: {body}",
            generation_config={
                "temperature": 0.1,
                "response_mime_type": "application/json",
            },
        )
        return str(getattr(response, "text", "") or "")

    async def parse_manual_schedule_datetimes(self, *, title: str, date_text: str, body: str) -> tuple[datetime, datetime]:
        """Gemini 호출을 백그라운드 스레드에서 실행하고 수동 일정 날짜 범위를 반환합니다."""
        raw_text = await asyncio.to_thread(
            self.generate_manual_schedule_datetime_json_sync,
            title=title,
            date_text=date_text,
            body=body,
        )
        return self.parse_manual_schedule_datetime_payload(raw_text)

    async def insert_generated_schedule(
        self,
        interaction: discord.Interaction,
        item: dict,
    ) -> tuple[str, datetime, datetime] | None:
        """Gemini JSON 항목 하나를 검증한 뒤 DB에 등록합니다. 실패한 항목은 None으로 건너뜁니다."""
        if interaction.guild is None:
            return None

        try:
            title = str(item["title"]).strip()
            start_time_text = str(item["start_time"]).strip()
            end_time_text = str(item["end_time"]).strip()
            location = str(item.get("location") or "Discord").strip()
            description = str(item["description"]).strip()
            if not title or not start_time_text or not end_time_text or not description:
                return None

            starts_at = fix_past_year(parse_datetime(start_time_text, self.bot.settings.timezone))
            ends_at = fix_past_year(parse_datetime(end_time_text, self.bot.settings.timezone))
        except (KeyError, TypeError, ValueError) as exc:
            logging.info("Skipping invalid generated schedule item %s: %s", item, exc)
            return None

        body = (
            f"**종료** {self.format_schedule_time(ends_at)}\n"
            f"**장소** {location or '공개된 정보 없음'}\n"
            f"{description}"
        )
        cursor = await self.bot.database.execute(
            """
            INSERT INTO schedules (guild_id, title, starts_at, body, created_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                interaction.guild.id,
                title[:100],
                to_storage_iso(starts_at),
                body[:1000],
                interaction.user.id,
                now_utc_iso(),
            ),
        )

        if self.bot.settings.enable_server_events:
            event_id, _ = await self.create_server_event(
                interaction.guild,
                title[:100],
                starts_at,
                description,
                end_time=ends_at,
                location=location or "Discord",
            )
            if event_id is not None:
                await self.bot.database.execute(
                    "UPDATE schedules SET event_id = ? WHERE id = ?",
                    (event_id, cursor.lastrowid),
                )

        return title[:100], starts_at, ends_at

    @app_commands.command(name="일정", description="Team 0x34의 등록된 일정을 Embed로 확인합니다.")
    async def list_schedules(self, interaction: discord.Interaction) -> None:
        """서버에 등록된 일정을 시간순으로 보여줍니다."""
        if interaction.guild is None:
            await interaction.response.send_message("서버 안에서만 일정을 확인할 수 있습니다.", ephemeral=True)
            return

        rows = await self.bot.database.fetch_all(
            """
            SELECT * FROM schedules
            WHERE guild_id = ?
            ORDER BY starts_at ASC
            LIMIT 20
            """,
            (interaction.guild.id,),
        )

        if not rows:
            embed = base_embed("Team 0x34 일정", "등록된 일정이 없습니다.", color=WARNING_COLOR)
            await interaction.response.send_message(embed=embed)
            return

        embed = base_embed("Team 0x34 일정", "가까운 일정부터 최대 20개까지 표시합니다.")
        for row in rows:
            starts_at = from_storage_iso(row["starts_at"], self.bot.settings.timezone)
            body = str(row["body"] or "").strip()
            ends_at = self.extract_schedule_end_time(body, starts_at)
            details = self.clean_schedule_body_for_display(body)
            value = f"{self.format_schedule_range(starts_at, ends_at)}"
            if details:
                value += f"\n{details}"
            value += f"\n등록자: <@{row['created_by']}>"
            if row["event_id"]:
                value += f"\n서버 이벤트 ID: `{row['event_id']}`"
            embed.add_field(name=row["title"], value=value[:1024], inline=False)

        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="일정추가", description="Modal로 새 일정을 등록합니다.")
    async def add_schedule(self, interaction: discord.Interaction) -> None:
        """Discord Modal을 열어 일정 정보를 입력받습니다."""
        await interaction.response.send_modal(ScheduleModal(self))

    @app_commands.command(name="일정삭제", description="드롭다운 메뉴로 등록된 일정을 삭제합니다.")
    async def delete_schedule(self, interaction: discord.Interaction) -> None:
        """등록된 일정 최대 25개를 Select Menu로 보여주고 선택한 일정을 삭제합니다."""
        if interaction.guild is None:
            await interaction.response.send_message("서버 안에서만 일정을 삭제할 수 있습니다.", ephemeral=True)
            return

        rows = await self.bot.database.fetch_all(
            """
            SELECT id, title, starts_at, event_id FROM schedules
            WHERE guild_id = ?
            ORDER BY starts_at ASC
            LIMIT 25
            """,
            (interaction.guild.id,),
        )

        if not rows:
            await interaction.response.send_message("등록된 일정이 없습니다.", ephemeral=True)
            return

        embed = base_embed(
            "삭제할 일정을 선택하세요",
            "드롭다운에는 가까운 일정부터 최대 25개까지 표시됩니다.",
            color=WARNING_COLOR,
        )
        await interaction.response.send_message(embed=embed, view=ScheduleDeleteView(self, rows, interaction.user.id), ephemeral=True)

    @app_commands.command(name="일정수정", description="드롭다운 메뉴와 Modal로 등록된 일정을 수정합니다.")
    async def edit_schedule(self, interaction: discord.Interaction) -> None:
        """내가 작성했거나 아직 지나지 않은 일정 최대 25개를 Select Menu로 보여줍니다."""
        if interaction.guild is None:
            await interaction.response.send_message("서버 안에서만 일정을 수정할 수 있습니다.", ephemeral=True)
            return

        rows = await self.bot.database.fetch_all(
            """
            SELECT id, title, starts_at, body, created_by, event_id FROM schedules
            WHERE guild_id = ? AND (created_by = ? OR starts_at >= ?)
            ORDER BY starts_at ASC
            LIMIT 25
            """,
            (interaction.guild.id, interaction.user.id, now_utc_iso()),
        )

        if not rows:
            await interaction.response.send_message("수정할 일정이 없습니다.", ephemeral=True)
            return

        embed = base_embed(
            "수정할 일정을 선택하세요",
            "내가 작성했거나 아직 지나지 않은 일정이 최대 25개까지 표시됩니다.",
            color=WARNING_COLOR,
        )
        await interaction.response.send_message(embed=embed, view=ScheduleEditView(self, rows, interaction.user.id), ephemeral=True)

    @app_commands.command(name="일정생성", description="Gemini로 안내 텍스트에서 여러 일정을 자동 등록합니다.")
    @app_commands.describe(target_info="참가 신청, 예선, 본선 등 일정을 추출할 대회/행사 안내 텍스트")
    async def create_schedules(self, interaction: discord.Interaction, target_info: str) -> None:
        """Gemini가 반환한 JSON 배열의 모든 일정을 검증 후 일괄 등록합니다."""
        await interaction.response.defer(ephemeral=True, thinking=True)

        if interaction.guild is None:
            await interaction.followup.send("서버 안에서만 일정을 생성할 수 있습니다.", ephemeral=True)
            return

        target_info = target_info.strip()
        if not target_info:
            await interaction.followup.send("일정을 추출할 텍스트를 입력해 주세요.", ephemeral=True)
            return

        try:
            source_text = await self.prepare_schedule_source_text(target_info)
            items = await self.generate_schedule_items(source_text)
        except exceptions.ResourceExhausted:
            await interaction.followup.send(GEMINI_RATE_LIMIT_MESSAGE, ephemeral=True)
            return
        except ScrapingError:
            await interaction.followup.send(SCRAPING_ERROR_MESSAGE, ephemeral=True)
            return
        except RuntimeError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        except Exception:
            logging.exception("Gemini schedule generation failed")
            await interaction.followup.send("Gemini API 호출 중 문제가 발생했습니다. API 키, 모델명, 할당량을 확인해 주세요.", ephemeral=True)
            return

        registered: list[tuple[str, datetime, datetime]] = []
        for item in items:
            result = await self.insert_generated_schedule(interaction, item)
            if result is None:
                continue
            registered.append(result)

        if not registered:
            await interaction.followup.send("Gemini 응답에서 등록 가능한 일정을 찾지 못했습니다.", ephemeral=True)
            return

        await self.update_schedule_board()

        summary_lines: list[str] = []
        for title, starts_at, ends_at in registered:
            summary_lines.append(f"• **{title}**: {self.format_schedule_range(starts_at, ends_at)}")
        embed = base_embed(
            f"✨ 총 {len(registered)}개의 일정이 자동 등록되었습니다!",
            "\n".join(summary_lines)[:4000],
            color=SUCCESS_COLOR,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    """discord.py가 이 파일을 Cog로 로드할 때 호출하는 함수입니다."""
    await bot.add_cog(ScheduleCog(bot))