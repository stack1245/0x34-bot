from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import discord

from services.recruitment_models import (
    PARTICIPANT_OWNER,
    STATUS_OPEN,
    RecruitmentParticipant,
    RecruitmentRecord,
)
from utils.ai_input import trim_text
from utils.embeds import STOP_COLOR, SUCCESS_COLOR, WARNING_COLOR, base_embed


class RecruitmentEmbedFactory:
    """Builds Discord embeds for recruitment UI surfaces.

    This factory keeps presentation formatting outside of Cog command routing so
    Discord views can ask for the same embed shape after every state mutation.
    """

    def build_missing_recruitment_embed(self) -> discord.Embed:
        """Builds the fallback embed used when a recruitment row is missing.

        Returns:
            A Discord embed that can replace a stale recruitment message.
        """
        return base_embed("모집 정보를 찾을 수 없습니다.", color=STOP_COLOR)

    def build_recruitment_embed(
        self,
        recruitment: RecruitmentRecord,
        confirmed_participants: Sequence[RecruitmentParticipant | dict[str, Any]],
        pending_count: int,
    ) -> discord.Embed:
        """Builds the public recruitment embed from hydrated domain state.

        Args:
            recruitment: Hydrated recruitment record from the service layer.
            confirmed_participants: Owner and accepted participants to render.
            pending_count: Number of pending applications.

        Returns:
            A Discord embed that reflects the current DB state.
        """
        participant_lines: list[str] = []
        seen_user_ids: set[int] = set()
        for row in confirmed_participants:
            user_id = int(row["user_id"])
            if user_id in seen_user_ids:
                continue
            seen_user_ids.add(user_id)
            if row["status"] == PARTICIPANT_OWNER:
                participant_lines.append(f"👑 <@{user_id}> [팀장]")
            else:
                participant_lines.append(f"<@{user_id}>")

        status_text = (
            "🟢 모집 중" if recruitment["status"] == STATUS_OPEN else "🔴 모집 마감"
        )
        color = (
            SUCCESS_COLOR
            if recruitment["status"] == STATUS_OPEN
            else discord.Color.dark_grey()
        )
        max_members = int(recruitment["max_members"])
        capacity = (
            f"{len(seen_user_ids)}명 / 제한 없음"
            if max_members == 0
            else f"{len(seen_user_ids)} / {max_members}"
        )
        participant_text = "\n".join(participant_lines) or "없음"

        embed = base_embed(
            str(recruitment["title"]), str(recruitment["target"]), color=color
        )
        embed.add_field(name="상태", value=status_text, inline=True)
        embed.add_field(name="현재 인원", value=capacity, inline=True)
        embed.add_field(
            name="작성자", value=f"<@{recruitment['author_id']}>", inline=True
        )
        if recruitment["thread_id"]:
            embed.add_field(
                name="비공개 워크스페이스",
                value=f"<#{recruitment['thread_id']}>",
                inline=False,
            )
        embed.add_field(
            name="참가자", value=trim_text(participant_text, 1024), inline=False
        )
        embed.add_field(name="신청 대기", value=f"{pending_count}명", inline=True)
        return embed

    def build_application_manage_embed(
        self,
        recruitment: RecruitmentRecord,
        *,
        applicant_id: int,
        application_reason: str,
        applicant_mention: str | None = None,
        title: str = "새로운 참가 신청",
        color: discord.Color = WARNING_COLOR,
        decided_by: str | None = None,
        notice: str | None = None,
    ) -> discord.Embed:
        """Builds the private application management embed.

        Args:
            recruitment: Recruitment that owns the application.
            applicant_id: Discord user id of the applicant.
            application_reason: Free-form applicant reason text.
            applicant_mention: Optional mention string from the interaction user.
            title: Embed title for the current application state.
            color: Embed color for the current application state.
            decided_by: Optional moderator mention that made the decision.
            notice: Optional decision or side-effect notice.

        Returns:
            A Discord embed for the private workspace review flow.
        """
        embed = base_embed(
            title, "비공개 워크스페이스에서 참가 신청을 검토하세요.", color=color
        )
        embed.add_field(name="모집", value=str(recruitment["title"]), inline=False)
        embed.add_field(
            name="신청자", value=applicant_mention or f"<@{applicant_id}>", inline=True
        )
        embed.add_field(
            name="신청 사유",
            value=(application_reason or "작성되지 않음")[:1024],
            inline=False,
        )
        if decided_by is not None:
            embed.add_field(name="처리자", value=decided_by, inline=True)
        if notice is not None:
            embed.add_field(name="처리 결과", value=notice[:1024], inline=False)
        return embed
