from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Optional

import discord

from core.config import build_theme_embed
from database.connection import add_schedule

logger = logging.getLogger(__name__)
DATETIME_TOKEN_PATTERN = re.compile(r"\d+")
ERROR_EMBED_COLOR = 0xFF1744
SECTION_DIVIDER = "━━━━━━━━━━━━━━━━━━━━━━━━"


def parse_to_timestamp(raw_value: str, *, allow_empty: bool) -> Optional[int | str]:
    normalized = " ".join(raw_value.split()).strip()
    if not normalized:
        return None if allow_empty else raw_value

    tokens = DATETIME_TOKEN_PATTERN.findall(normalized)
    if len(tokens) < 3:
        logger.warning(
            "Schedule date parsing fell back to raw text because token count was insufficient. raw=%s tokens=%s",
            normalized,
            tokens,
        )
        return raw_value

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
        logger.warning(
            "Schedule date parsing fell back to raw text because datetime construction failed. raw=%s",
            normalized,
        )
        return raw_value

    return int(parsed_datetime.timestamp())


class ScheduleModal(discord.ui.Modal):
    def __init__(self) -> None:
        super().__init__(title="0x34 일정 등록 시스템")

        self.schedule_title = discord.ui.InputText(
            label="📆 일정/대회명",
            placeholder="예시: Team 0x34 Algorithm Open / 제8회 한국코드페어 SW공모전",
            min_length=1,
            max_length=50,
            required=True,
        )
        self.start_datetime = discord.ui.InputText(
            label="🛫 시작 일시 (선택 - YYYY-MM-DD HH:MM)",
            placeholder="예시: 2026-06-30 10:00 (빈칸 허용)",
            required=False,
            max_length=40,
        )
        self.end_datetime = discord.ui.InputText(
            label="🏁 종료/마감 일시 (필수 - YYYY-MM-DD HH:MM)",
            placeholder="예시: 2026-06-30 20:00",
            required=True,
            max_length=40,
        )

        self.add_item(self.schedule_title)
        self.add_item(self.start_datetime)
        self.add_item(self.end_datetime)

    async def callback(self, interaction: discord.Interaction) -> None:
        logger.info(
            "Schedule modal submitted. guild_id=%s user_id=%s",
            interaction.guild_id,
            getattr(interaction.user, "id", None),
        )
        guild_id = interaction.guild_id
        if guild_id is None:
            await self._respond(
                interaction,
                title="❌ 일정 등록 실패",
                description="## 상태\n- ❌ 이 모달은 서버 내부에서만 사용할 수 있습니다.",
            )
            return

        title = " ".join(self.schedule_title.value.split()).strip()
        start_raw = " ".join((self.start_datetime.value or "").split()).strip()
        end_raw = " ".join(self.end_datetime.value.split()).strip()

        if not title:
            await self._respond(
                interaction,
                title="❌ 입력 검증 실패",
                description="## 상태\n- ❌ 일정/대회명은 공백 없이 1자 이상 입력해야 합니다.",
            )
            return

        if not end_raw:
            await self._respond(
                interaction,
                title="❌ 날짜 형식 오류",
                description="\n".join(
                    [
                        "## 가이드",
                        "- 종료/마감 일시는 필수입니다.",
                        "- 예시: 2026-06-30 20:00",
                    ]
                ),
            )
            return

        start_timestamp = parse_to_timestamp(
            start_raw,
            allow_empty=True,
        )
        end_timestamp = parse_to_timestamp(
            end_raw,
            allow_empty=False,
        )

        if isinstance(start_timestamp, str) or isinstance(end_timestamp, str):
            start_display = (
                start_timestamp if isinstance(start_timestamp, str) else "정상 파싱"
            )
            end_display = (
                end_timestamp if isinstance(end_timestamp, str) else "정상 파싱"
            )
            await self._respond(
                interaction,
                title="❌ 날짜 변환 실패",
                description="\n".join(
                    [
                        "## 입력 원문",
                        f"- 시작 일시: {start_display}",
                        f"- 종료/마감 일시: {end_display}",
                    ]
                ),
            )
            return

        if start_timestamp is not None and start_timestamp > end_timestamp:
            await self._respond(
                interaction,
                title="❌ 일정 시간 검증 실패",
                description="## 상태\n- ❌ 시작 일시는 종료 일시보다 빠를 수 없습니다.",
            )
            return

        try:
            add_schedule(guild_id, title, start_timestamp, end_timestamp)
        except Exception as error:
            logger.exception(
                "Failed to store schedule from modal. guild_id=%s title=%s start_timestamp=%s end_timestamp=%s",
                guild_id,
                title,
                start_timestamp,
                end_timestamp,
                exc_info=error,
            )
            await self._respond(
                interaction,
                title="❌ 일정 저장 실패",
                description="## 상태\n- ❌ DB 트랜잭션 오류로 일정을 저장하지 못했습니다.",
            )
            return

        try:
            from cogs.schedule import update_live_dashboard

            if isinstance(interaction.client, discord.Bot):
                await update_live_dashboard(interaction.client, guild_id)
        except Exception as error:
            logger.exception(
                "Schedule saved but live dashboard update failed. guild_id=%s title=%s",
                guild_id,
                title,
                exc_info=error,
            )

        logger.info(
            "Schedule saved from modal. guild_id=%s title=%s start_timestamp=%s end_timestamp=%s",
            guild_id,
            title,
            start_timestamp,
            end_timestamp,
        )

        period_line = (
            f"- 기간: <t:{start_timestamp}:F> ~ <t:{end_timestamp}:F>"
            if start_timestamp is not None
            else f"- 마감: <t:{end_timestamp}:F> (<t:{end_timestamp}:R>)"
        )
        await self._respond(
            interaction,
            title="✅ 일정 등록 완료",
            description="\n".join(
                [
                    "## 실행 결과",
                    SECTION_DIVIDER,
                    f"- 제목: **{title}**",
                    period_line,
                    "- 상태: guild_schedules 저장 완료",
                ]
            ),
        )

    async def on_error(
        self,
        error: Exception,
        interaction: discord.Interaction,
    ) -> None:
        logger.exception("Unhandled error in ScheduleModal.", exc_info=error)
        await self._respond(
            interaction,
            title="❌ 일정 등록 오류",
            description="## 상태\n- ❌ 모달 처리 중 예기치 않은 오류가 발생했습니다. 로그를 확인해주세요.",
        )

    @staticmethod
    async def _respond(
        interaction: discord.Interaction,
        *,
        title: str,
        description: str,
    ) -> None:
        embed = build_theme_embed(title=title, description=description)
        if title.startswith("❌"):
            embed.color = discord.Colour(ERROR_EMBED_COLOR)

        guild = interaction.guild
        icon_url = guild.icon.url if guild and guild.icon else None
        footer_text = embed.footer.text or "Team 0x34 IT Operations"
        embed.set_footer(text=footer_text, icon_url=icon_url)

        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        await interaction.response.send_message(embed=embed, ephemeral=True)
