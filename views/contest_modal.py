from __future__ import annotations

import logging
import re
from datetime import datetime

import discord

from core.config import build_theme_embed
from database.connection import get_contest_channel_id
from views.contest_view import ContestLinkView

logger = logging.getLogger(__name__)

DATE_TOKEN_PATTERN = re.compile(r"\d+")
SECTION_DIVIDER = "━━━━━━━━━━━━━━━━━━━━━━━━"
ERROR_EMBED_COLOR = 0xFF1744


def parse_to_timestamp(date_str: str) -> str:
    normalized = " ".join(date_str.split()).strip()
    if not normalized:
        logger.warning("Contest date parsing skipped because the input was empty.")
        return date_str

    range_parts = _split_date_range(normalized)
    if len(range_parts) == 2:
        start_ts = _parse_single_datetime_to_timestamp(range_parts[0])
        end_ts = _parse_single_datetime_to_timestamp(range_parts[1])
        if start_ts is None or end_ts is None:
            logger.warning(
                "Contest date range parsing fell back to raw text. raw=%s",
                normalized,
            )
            return date_str

        formatted_range = (
            f"<t:{start_ts}:F> (<t:{start_ts}:R>) ~ " f"<t:{end_ts}:F> (<t:{end_ts}:R>)"
        )
        logger.info(
            "Contest date range parsed into Discord timestamps. raw=%s start=%s end=%s",
            normalized,
            start_ts,
            end_ts,
        )
        return formatted_range

    timestamp = _parse_single_datetime_to_timestamp(normalized)
    if timestamp is None:
        logger.warning(
            "Contest date parsing fell back to raw text because datetime construction failed. raw=%s",
            normalized,
        )
        return date_str

    formatted_timestamp = f"<t:{timestamp}:F> (<t:{timestamp}:R>)"
    logger.info(
        "Contest date parsed into Discord timestamp. raw=%s timestamp=%s formatted=%s",
        normalized,
        timestamp,
        formatted_timestamp,
    )
    return formatted_timestamp


def _split_date_range(value: str) -> list[str]:
    if "~" in value:
        parts = [part.strip() for part in re.split(r"\s*~\s*", value, maxsplit=1)]
        return [part for part in parts if part]

    dash_range_match = re.search(r"\s[-–—]\s", value)
    if dash_range_match:
        parts = [part.strip() for part in re.split(r"\s[-–—]\s", value, maxsplit=1)]
        return [part for part in parts if part]

    return [value]


def _parse_single_datetime_to_timestamp(raw_value: str) -> int | None:
    tokens = DATE_TOKEN_PATTERN.findall(raw_value)
    if len(tokens) < 3:
        return None

    try:
        year = int(tokens[0])
        if year < 100:
            year += 2000

        month = int(tokens[1])
        day = int(tokens[2])

        if len(tokens) == 3:
            hour = 0
            minute = 0
            second = 0
        elif len(tokens) == 4:
            hour = int(tokens[3])
            minute = 0
            second = 0
        else:
            hour = int(tokens[3])
            minute = int(tokens[4])
            second = int(tokens[5]) if len(tokens) >= 6 else 0

        timezone_info = datetime.now().astimezone().tzinfo
        parsed_datetime = datetime(
            year,
            month,
            day,
            hour,
            minute,
            second,
            tzinfo=timezone_info,
        )
    except ValueError:
        return None

    return int(parsed_datetime.timestamp())


def _as_text_block(value: str) -> str:
    return f"```text\n{value}\n```"


class ContestModal(discord.ui.Modal):
    def __init__(self) -> None:
        super().__init__(title="새 대회 공고 작성")

        self.contest_name = discord.ui.InputText(
            label="대회명",
            placeholder="예시: Team 0x34 Algorithm Open / 제8회 한국코드페어 SW공모전",
            min_length=1,
            max_length=100,
        )
        self.organizer = discord.ui.InputText(
            label="주최 기관",
            placeholder="예시: Team 0x34 Advanced Software Team / Sunrin SW Center",
            max_length=100,
            required=False,
        )
        self.target_audience = discord.ui.InputText(
            label="참가 대상",
            placeholder="예시: 선린인터넷고 재학생 및 졸업생",
            max_length=100,
            required=False,
        )
        self.recruitment_schedule = discord.ui.InputText(
            label="📅 모집 및 접수 일정",
            placeholder="예시: 2026-06-15 ~ 2026-06-30 또는 데드라인 일시 입력",
            max_length=100,
        )
        self.description = discord.ui.InputText(
            label="상세 설명",
            placeholder="대회 세부 운영 규칙과 공식 웹페이지 URL을 기입하십시오.",
            style=discord.InputTextStyle.long,
            max_length=1600,
        )

        self.add_item(self.contest_name)
        self.add_item(self.organizer)
        self.add_item(self.target_audience)
        self.add_item(self.recruitment_schedule)
        self.add_item(self.description)

    async def callback(self, interaction: discord.Interaction) -> None:
        logger.info(
            "Contest modal submitted. guild_id=%s user_id=%s",
            interaction.guild_id,
            getattr(interaction.user, "id", None),
        )

        guild = interaction.guild
        guild_id = interaction.guild_id
        if guild is None or guild_id is None:
            await self._respond(
                interaction,
                title="❌ 대회 공고 작성 실패",
                description="## 상태\n- 이 모달은 서버 내부에서만 사용할 수 있습니다.",
            )
            return

        channel_id = get_contest_channel_id(guild_id)
        if channel_id is None:
            await self._respond(
                interaction,
                title="❌ 대회 공고 채널 미설정",
                description="## 상태\n- 먼저 /대회공고 채널설정 명령으로 공고 채널을 등록해주세요.",
            )
            return

        channel = guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            logger.warning(
                "Stored contest channel not found or invalid. guild_id=%s channel_id=%s",
                guild_id,
                channel_id,
            )
            await self._respond(
                interaction,
                title="❌ 대회 공고 채널 확인 필요",
                description="## 상태\n- 저장된 채널을 찾을 수 없습니다. /대회공고 채널설정 으로 다시 등록해주세요.",
            )
            return

        contest_name = self._normalize_required(self.contest_name.value)
        recruitment_schedule_raw = self._normalize_required(
            self.recruitment_schedule.value
        )
        description = self._normalize_required(self.description.value)
        organizer = self._normalize_optional(self.organizer.value, default="미입력")
        target_audience = self._normalize_optional(
            self.target_audience.value, default="제한 없음"
        )

        if not contest_name or not recruitment_schedule_raw or not description:
            await self._respond(
                interaction,
                title="❌ 입력 검증 실패",
                description=(
                    "## 상태\n"
                    "- 대회명, 모집 및 접수 일정, 상세 설명은 공백 없이 입력해야 합니다."
                ),
            )
            return

        recruitment_schedule = parse_to_timestamp(recruitment_schedule_raw)

        link_view = ContestLinkView(description)
        link_summary = (
            "\n".join(f"- {link.label}" for link in link_view.links)
            or "- 자동 감지된 링크가 없습니다."
        )

        notice_embed = build_theme_embed(
            title="Team 0x34 교내·외 대회 및 공모전 신규 공고",
            description="\n".join(
                [
                    "## 📝  상세 설명",
                    SECTION_DIVIDER,
                    description,
                ]
            ),
        )
        notice_embed.add_field(
            name="📌 대회명",
            value=_as_text_block(contest_name),
            inline=False,
        )
        notice_embed.add_field(
            name="🏢 주최 기관",
            value=_as_text_block(organizer),
            inline=True,
        )
        notice_embed.add_field(
            name="📌 참가 대상",
            value=_as_text_block(target_audience),
            inline=True,
        )
        notice_embed.add_field(
            name="📅 모집 및 접수 일정",
            value=recruitment_schedule,
            inline=False,
        )
        notice_embed.add_field(
            name="📌  바로가기 버튼", value=link_summary, inline=False
        )
        self._apply_guild_footer(notice_embed, interaction)

        try:
            await channel.send(
                embed=notice_embed, view=link_view if link_view.has_link else None
            )
        except discord.Forbidden:
            logger.exception(
                "Missing permission while sending contest notice. guild_id=%s channel_id=%s",
                guild_id,
                channel.id,
            )
            await self._respond(
                interaction,
                title="❌ 권한 부족",
                description=(
                    "## 상태\n"
                    f"{SECTION_DIVIDER}\n"
                    "- 봇에게 해당 채널의 메시지 전송 또는 임베드 링크 권한이 없습니다."
                ),
                is_error=True,
            )
            return

        logger.info(
            "Contest notice delivered. guild_id=%s channel_id=%s buttons=%s",
            guild_id,
            channel.id,
            len(link_view.links),
        )
        await self._respond(
            interaction,
            title="대회 공고 전송 완료",
            description="\n".join(
                [
                    "## 실행 결과",
                    SECTION_DIVIDER,
                    f"- 공고 채널: {channel.mention}",
                    f"- 자동 생성 버튼 수: {len(link_view.links)}",
                    "- 상태: 메시지 게시 완료",
                ]
            ),
        )

    async def on_error(
        self, error: Exception, interaction: discord.Interaction
    ) -> None:
        logger.exception("Unhandled error in ContestModal.", exc_info=error)
        await self._respond(
            interaction,
            title="❌ 대회 공고 작성 오류",
            description=(
                "## 상태\n"
                f"{SECTION_DIVIDER}\n"
                "- 모달 처리 중 예기치 않은 오류가 발생했습니다. 로그를 확인해주세요."
            ),
            is_error=True,
        )

    @staticmethod
    def _normalize_required(value: str) -> str:
        return " ".join(value.split()).strip()

    @staticmethod
    def _normalize_optional(value: str | None, *, default: str) -> str:
        normalized = " ".join((value or "").split()).strip()
        return normalized or default

    @staticmethod
    async def _respond(
        interaction: discord.Interaction,
        *,
        title: str,
        description: str,
        is_error: bool = False,
    ) -> None:
        embed = build_theme_embed(title=title, description=description)
        if is_error or title.startswith("❌"):
            embed.color = discord.Colour(ERROR_EMBED_COLOR)

        ContestModal._apply_guild_footer(embed, interaction)
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @staticmethod
    def _apply_guild_footer(
        embed: discord.Embed,
        interaction: discord.Interaction,
    ) -> None:
        guild = interaction.guild
        icon_url = guild.icon.url if guild and guild.icon else None
        footer_text = embed.footer.text or "Team 0x34 | IT Operations"
        embed.set_footer(text=footer_text, icon_url=icon_url)
