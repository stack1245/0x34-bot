from __future__ import annotations
import json
import logging
import re

import discord
from discord import app_commands
from discord.ext import commands
from google.api_core import exceptions

from services.ai import AIRequest
from services.recruitment import (
    CreateRecruitmentRequest,
    RecruitmentService,
    PARTICIPANT_ACCEPTED,
    PARTICIPANT_OWNER,
    PARTICIPANT_PENDING,
    PARTICIPANT_REJECTED,
    STATUS_CLOSED,
    STATUS_OPEN,
)
from utils.ai_input import (
    CONVERSATIONAL_INPUT_INSTRUCTION,
    ScrapingError,
    prepare_conversational_source_text,
    trim_text as _trim_text,
)
from utils.datetime import get_current_time_context
from utils.embeds import STOP_COLOR, SUCCESS_COLOR, WARNING_COLOR, base_embed

DEFAULT_AI_RECRUITMENT_CAPACITY = 4
MAX_AI_TITLE_LENGTH = 50
MAX_EMBED_DESCRIPTION_LENGTH = 3900
MAX_RECRUITMENT_SOURCE_TEXT_LENGTH = 12000
SCRAPING_ERROR_MESSAGE = "웹페이지 내용을 불러오지 못했습니다. 사이트 링크 대신 상세 텍스트를 직접 입력해 주세요."
GEMINI_RATE_LIMIT_MESSAGE = (
    "⚠️ 봇이 너무 많은 요청을 처리하고 있습니다. 1분 뒤에 다시 시도해 주세요."
)


GEMINI_SYSTEM_PROMPT = """
주어진 해커톤/대회 웹사이트 텍스트를 분석하여 다음 JSON 스키마에 맞게 결과를 반환해라.
{
    "title": "이모지를 포함한 50자 이내의 모집 제목",
    "description": "마크다운을 활용한 대회 일정, 참가 자격, 주제, 혜택 요약글",
    "max_members": "본문에 명시된 최대 팀원 수 (정수형). 명시되어 있지 않으면 4로 설정"
}
텍스트에 없는 내용은 추측하지 말고, 확인할 수 없는 항목은 "공개된 정보 없음"이라고 적어라.
응답은 반드시 JSON 객체 하나로만 작성해라.
""".strip()


def build_recruitment_system_prompt() -> str:
    """Gemini 모집 글 생성에 현재 한국 시간과 날짜 규칙을 주입합니다."""
    return f"""
{get_current_time_context()}
위 제공된 '현재 시간'을 기준으로 날짜를 계산해라. 본문에 연도가 생략되어 있다면 무조건 현재 연도를 사용하고, 절대로 지나간 과거 연도로 작성하지 마라.
{CONVERSATIONAL_INPUT_INSTRUCTION}

{GEMINI_SYSTEM_PROMPT}
""".strip()


def _strip_json_code_fence(value: str) -> str:
    """Gemini가 ```json 코드블록으로 감싸서 답해도 JSON만 꺼낼 수 있게 합니다."""
    text = value.strip()
    fence_match = re.search(
        r"```(?:json)?\s*(.*?)\s*```", text, flags=re.IGNORECASE | re.DOTALL
    )
    if fence_match is None:
        return text
    return fence_match.group(1).strip()


def _clean_title(value: str) -> str:
    """Markdown 제목 기호나 `제목:` 접두어를 제거하고 50자 제한을 맞춥니다."""
    title = value.strip()
    title = re.sub(r"^#+\s*", "", title)
    title = re.sub(
        r"^\*{0,2}(제목|title)\*{0,2}\s*[:：]\s*", "", title, flags=re.IGNORECASE
    )
    return _trim_text(title, MAX_AI_TITLE_LENGTH)


def _parse_max_members(value: object) -> int:
    """Gemini JSON의 max_members를 안전하게 정수로 바꾸고 실패하면 기본값 4를 사용합니다."""
    try:
        if isinstance(value, bool):
            raise ValueError("bool is not a valid max_members")
        if isinstance(value, int):
            parsed = value
        elif isinstance(value, float):
            parsed = int(value)
        elif isinstance(value, str):
            match = re.search(r"\d+", value)
            if match is None:
                raise ValueError("no integer in max_members string")
            parsed = int(match.group(0))
        else:
            raise ValueError("unsupported max_members type")
    except (TypeError, ValueError):
        return DEFAULT_AI_RECRUITMENT_CAPACITY

    if parsed < 0:
        return DEFAULT_AI_RECRUITMENT_CAPACITY
    return parsed


def parse_gemini_recruitment(
    raw_text: str, fallback_source: str
) -> tuple[str, str, int]:
    """Gemini 응답을 모집 Embed에 넣을 제목과 본문으로 변환합니다.

    모델에는 JSON만 반환하라고 지시하지만, 실제 LLM 응답은 코드블록이나 일반 텍스트가 섞일 수 있습니다.
    그래서 json.loads를 먼저 시도하고, 실패하면 첫 줄을 제목, 나머지를 본문으로 쓰는 fallback을 둡니다.
    max_members는 JSON 파싱 실패나 값 오류가 있어도 기본값 4로 안전하게 보정합니다.
    """
    cleaned = _strip_json_code_fence(raw_text)
    title = ""
    description = ""
    max_members = DEFAULT_AI_RECRUITMENT_CAPACITY

    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError:
        object_match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if object_match is not None:
            try:
                payload = json.loads(object_match.group(0))
            except json.JSONDecodeError:
                payload = None
        else:
            payload = None

    if isinstance(payload, dict):
        title = str(payload.get("title", ""))
        description = str(payload.get("description", ""))
        max_members = _parse_max_members(
            payload.get("max_members", DEFAULT_AI_RECRUITMENT_CAPACITY)
        )

    if not title or not description:
        lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
        if lines:
            title = title or lines[0]
            description = description or "\n".join(lines[1:])

    title = _clean_title(title) or "🚀 Team 0x34 모집"
    description = description.strip() or f"**대상 정보**\n- {fallback_source}"
    return title, _trim_text(description, MAX_EMBED_DESCRIPTION_LENGTH), max_members


class RecruitmentView(discord.ui.View):
    """모집 메시지 아래에 붙는 Persistent Button View입니다."""

    def __init__(self, cog: "RecruitmentCog") -> None:
        # timeout=None과 고정 custom_id를 쓰면 봇 재시작 후에도 버튼 이벤트를 받을 수 있습니다.
        super().__init__(timeout=None)
        self.cog = cog

    def disable_buttons(self) -> None:
        """현재 모집 메시지의 버튼을 모두 비활성화합니다."""
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True

    def disable_for_closed_recruitment(self) -> None:
        """마감된 모집에서는 신청과 마감만 막고 관리는 유지합니다."""
        for child in self.children:
            if (
                isinstance(child, discord.ui.Button)
                and child.custom_id != "0x34:recruitment:manage"
            ):
                child.disabled = True

    async def get_recruitment_from_message(self, interaction: discord.Interaction):
        """버튼이 눌린 모집 메시지에서 모집 레코드를 찾습니다."""
        if interaction.message is None:
            await interaction.response.send_message(
                "모집 메시지를 찾을 수 없습니다.", ephemeral=True
            )
            return None

        recruitment = await self.cog.get_recruitment(interaction.message.id)
        if recruitment is None:
            await interaction.response.send_message(
                "DB에서 모집 정보를 찾을 수 없습니다.", ephemeral=True
            )
            return None
        return recruitment

    async def get_open_recruitment(self, interaction: discord.Interaction):
        """버튼이 눌린 모집 메시지에서 열려 있는 모집 레코드를 찾습니다."""
        recruitment = await self.get_recruitment_from_message(interaction)
        if recruitment is None:
            return None
        if recruitment["status"] == STATUS_CLOSED:
            await interaction.response.send_message(
                "이미 마감된 모집입니다.", ephemeral=True
            )
            return None
        return recruitment

    @discord.ui.button(
        label="신청하기",
        style=discord.ButtonStyle.success,
        custom_id="0x34:recruitment:apply",
    )
    async def apply(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        """신청 사유를 받는 Modal을 엽니다."""
        recruitment = await self.get_open_recruitment(interaction)
        if recruitment is None:
            return
        if await self.cog.is_recruitment_owner(recruitment["id"], interaction.user.id):
            await interaction.response.send_message(
                "본인이 생성한 모집에는 신청할 수 없습니다.", ephemeral=True
            )
            return

        existing = await self.cog.get_participant(
            recruitment["id"], interaction.user.id
        )
        if existing is not None and existing["status"] == PARTICIPANT_ACCEPTED:
            await interaction.response.send_message(
                "이미 승인된 신청입니다.", ephemeral=True
            )
            return
        if existing is not None and existing["status"] == PARTICIPANT_PENDING:
            await interaction.response.send_message(
                "이미 신청 대기 중입니다. 작성자의 승인을 기다려 주세요.",
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(
            RecruitmentApplicationModal(
                self.cog, int(recruitment["id"]), int(recruitment["message_id"])
            )
        )

    @discord.ui.button(
        label="관리하기",
        style=discord.ButtonStyle.secondary,
        custom_id="0x34:recruitment:manage",
    )
    async def manage(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        """모집 소유자에게 승인 참가자 제거 메뉴와 워크스페이스 위치를 안내합니다."""
        recruitment = await self.get_recruitment_from_message(interaction)
        if recruitment is None:
            return
        if not await self.cog.is_recruitment_owner(
            recruitment["id"], interaction.user.id
        ):
            await interaction.response.send_message(
                "모집 소유자만 신청자를 관리할 수 있습니다.", ephemeral=True
            )
            return

        if interaction.guild is None:
            await interaction.response.send_message(
                "서버 안에서만 신청자를 관리할 수 있습니다.", ephemeral=True
            )
            return

        thread = await self.cog.get_recruitment_thread(recruitment, interaction.guild)
        accepted_participants = [
            row
            for row in recruitment["participants"]
            if row["status"] == PARTICIPANT_ACCEPTED
        ]

        thread_text = (
            f"비공개 워크스페이스: {thread.mention}"
            if thread is not None
            else "연결된 비공개 워크스페이스를 찾을 수 없습니다."
        )
        is_closed = recruitment["status"] == STATUS_CLOSED
        if not accepted_participants and not is_closed:
            await interaction.response.send_message(
                f"{thread_text}\n제거할 승인 참가자가 없습니다.",
                ephemeral=True,
            )
            return

        view = AcceptedParticipantManageView(
            self.cog,
            int(recruitment["id"]),
            int(recruitment["channel_id"]),
            int(recruitment["message_id"]),
            accepted_participants,
            interaction.guild,
            str(recruitment["status"]),
        )
        overflow_text = (
            "\n승인 참가자가 25명을 넘어서 목록에는 25명만 표시합니다."
            if len(accepted_participants) > 25
            else ""
        )
        remove_text = (
            "제거할 승인 참가자를 선택하세요."
            if accepted_participants
            else "제거할 승인 참가자가 없습니다."
        )
        reopen_text = (
            "\n마감된 모집은 [마감 취소] 버튼으로 다시 열 수 있습니다."
            if is_closed
            else ""
        )
        await interaction.response.send_message(
            f"{thread_text}\n{remove_text}{overflow_text}{reopen_text}",
            view=view,
            ephemeral=True,
        )

    @discord.ui.button(
        label="모집 마감",
        style=discord.ButtonStyle.primary,
        custom_id="0x34:recruitment:close",
    )
    async def close_recruitment(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        """작성자가 모집을 마감하고 원본 모집 메시지를 닫힌 상태로 갱신합니다."""
        if interaction.message is None:
            await interaction.response.send_message(
                "모집 메시지를 찾을 수 없습니다.", ephemeral=True
            )
            return

        recruitment = await self.cog.get_recruitment(interaction.message.id)
        if recruitment is None:
            await interaction.response.send_message(
                "DB에서 모집 정보를 찾을 수 없습니다.", ephemeral=True
            )
            return
        if not await self.cog.is_recruitment_owner(
            recruitment["id"], interaction.user.id
        ):
            await interaction.response.send_message(
                "모집 소유자만 마감할 수 있습니다.", ephemeral=True
            )
            return
        if recruitment["status"] == STATUS_CLOSED:
            await interaction.response.send_message(
                "이미 마감된 모집입니다.", ephemeral=True
            )
            return

        await self.cog.recruitment_service.close_recruitment(int(recruitment["id"]))

        updated_embed = await self.cog.build_recruitment_embed(
            recruitment["message_id"]
        )
        self.disable_for_closed_recruitment()
        await interaction.response.edit_message(embed=updated_embed, view=self)

        thread, thread_notice = await self.cog.ensure_private_workspace_thread(
            recruitment, interaction, interaction.message
        )
        confirmed = await self.cog.get_confirmed_participant_user_ids(recruitment["id"])
        mentions = (
            " ".join(f"<@{user_id}>" for user_id in confirmed) or "참가자가 없습니다."
        )

        if thread is None:
            await interaction.followup.send(
                "✅ 모집이 성공적으로 마감되었습니다.\n"
                "비공개 스레드를 사용할 수 없어 참가자 멘션은 공개 채널에 보내지 않았습니다.\n"
                f"{thread_notice or '채널 권한과 서버 부스트 레벨을 확인해 주세요.'}",
                ephemeral=True,
            )
            return

        for user_id in confirmed:
            await self.cog.add_user_to_private_thread(
                thread, discord.Object(id=user_id)
            )

        await thread.send(f"모집이 마감되었습니다.\n참가 인원: {mentions}")
        await interaction.followup.send(
            f"✅ 모집이 성공적으로 마감되었습니다.\n비공개 워크스페이스에 참가자를 안내했습니다: {thread.mention}",
            ephemeral=True,
        )


class RecruitmentApplicationModal(discord.ui.Modal, title="모집 신청"):
    """신청자가 자기소개/신청 사유를 입력하는 Modal입니다."""

    reason_input = discord.ui.TextInput(
        label="신청 사유 또는 자기소개",
        placeholder="가능한 역할, 참여 가능 시간, 간단한 자기소개를 적어 주세요.",
        style=discord.TextStyle.long,
        max_length=1000,
    )

    def __init__(
        self, cog: "RecruitmentCog", recruitment_id: int, message_id: int
    ) -> None:
        super().__init__()
        self.cog = cog
        self.recruitment_id = recruitment_id
        self.message_id = message_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        """신청 내용을 pending 상태로 저장하고 비공개 스레드에 관리 Embed를 남깁니다."""
        await interaction.response.defer(ephemeral=True, thinking=True)

        if interaction.guild is None:
            await interaction.followup.send(
                "서버 안에서만 모집에 신청할 수 있습니다.", ephemeral=True
            )
            return

        recruitment = await self.cog.get_recruitment(self.message_id)
        if recruitment is None or int(recruitment["id"]) != self.recruitment_id:
            await interaction.followup.send(
                "모집 정보를 찾을 수 없습니다.", ephemeral=True
            )
            return
        if recruitment["status"] == STATUS_CLOSED:
            await interaction.followup.send("이미 마감된 모집입니다.", ephemeral=True)
            return
        if await self.cog.is_recruitment_owner(
            self.recruitment_id, interaction.user.id
        ):
            await interaction.followup.send(
                "본인이 생성한 모집에는 신청할 수 없습니다.", ephemeral=True
            )
            return

        reason = str(self.reason_input.value).strip()
        await self.cog.save_participant_status(
            self.recruitment_id,
            interaction.user.id,
            PARTICIPANT_PENDING,
            application_reason=reason,
        )
        message_notice = await self.cog.edit_recruitment_message(
            int(recruitment["channel_id"]), int(recruitment["message_id"])
        )

        thread = await self.cog.get_recruitment_thread(recruitment, interaction.guild)
        if thread is None:
            notice = await self.cog.notify_recruitment_owner(
                recruitment, interaction.user, reason
            )
            message = (
                "신청은 접수되었지만 연결된 비공개 워크스페이스를 찾지 못했습니다."
            )
            if message_notice:
                message += f"\n{message_notice}"
            if notice:
                message += f"\n{notice}"
            await interaction.followup.send(message, ephemeral=True)
            return

        application_embed = self.cog.build_application_manage_embed(
            recruitment,
            applicant_id=interaction.user.id,
            applicant_mention=interaction.user.mention,
            application_reason=reason,
        )
        application_view = ApplicationManageView(
            self.cog,
            recruitment_id=self.recruitment_id,
            applicant_id=interaction.user.id,
            application_reason=reason,
        )
        try:
            await thread.send(embed=application_embed, view=application_view)
        except discord.Forbidden:
            message = (
                "신청은 접수되었지만 비공개 워크스페이스에 알림을 보낼 권한이 없습니다."
            )
            if message_notice:
                message += f"\n{message_notice}"
            await interaction.followup.send(message, ephemeral=True)
            return
        except discord.HTTPException as exc:
            message = f"신청은 접수되었지만 비공개 워크스페이스 알림 전송에 실패했습니다: {exc.text}"
            if message_notice:
                message += f"\n{message_notice}"
            await interaction.followup.send(message, ephemeral=True)
            return

        message = "신청이 접수되었습니다. 작성자의 승인을 기다려 주세요."
        if message_notice:
            message += f"\n{message_notice}"
        await interaction.followup.send(message, ephemeral=True)


class ApplicationManageView(discord.ui.View):
    """비공개 워크스페이스 안에서 개별 참가 신청을 승인/거절하는 View입니다."""

    def __init__(
        self,
        cog: "RecruitmentCog",
        recruitment_id: int,
        applicant_id: int,
        application_reason: str,
    ) -> None:
        super().__init__(timeout=7 * 24 * 60 * 60)
        self.cog = cog
        self.recruitment_id = recruitment_id
        self.applicant_id = applicant_id
        self.application_reason = application_reason

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if await self.cog.is_recruitment_owner(
            self.recruitment_id, interaction.user.id
        ):
            return True
        await interaction.response.send_message(
            "모집 소유자만 신청을 승인하거나 거절할 수 있습니다.", ephemeral=True
        )
        return False

    def disable_buttons(self) -> None:
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True

    @discord.ui.button(
        label="✅ 승인",
        style=discord.ButtonStyle.success,
        custom_id="0x34:recruitment:application:approve",
    )
    async def approve(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        """신청자를 accepted로 바꾸고 정확한 Member 객체를 비공개 스레드에 초대합니다."""
        await interaction.response.defer(ephemeral=True, thinking=True)
        if interaction.guild is None:
            await interaction.followup.send(
                "서버 정보를 찾을 수 없어 신청을 승인할 수 없습니다.", ephemeral=True
            )
            return

        recruitment = await self.cog.get_recruitment_by_id(self.recruitment_id)
        if recruitment is None:
            await interaction.followup.send(
                "모집 정보를 찾을 수 없습니다.", ephemeral=True
            )
            return
        if recruitment["status"] == STATUS_CLOSED:
            await interaction.followup.send("이미 마감된 모집입니다.", ephemeral=True)
            return

        if recruitment["max_members"] > 0:
            confirmed = await self.cog.get_confirmed_participant_user_ids(
                self.recruitment_id
            )
            if (
                self.applicant_id not in confirmed
                and len(confirmed) >= recruitment["max_members"]
            ):
                await interaction.followup.send("정원이 이미 찼습니다.", ephemeral=True)
                return

        await self.cog.save_participant_status(
            self.recruitment_id, self.applicant_id, PARTICIPANT_ACCEPTED
        )
        auto_closed = await self.cog.close_recruitment_if_full(self.recruitment_id)
        if auto_closed:
            updated_recruitment = await self.cog.get_recruitment_by_id(
                self.recruitment_id
            )
            if updated_recruitment is not None:
                recruitment = updated_recruitment
        message_notice = await self.cog.edit_recruitment_message(
            int(recruitment["channel_id"]), int(recruitment["message_id"])
        )

        thread = await self.cog.get_recruitment_thread(recruitment, interaction.guild)
        invite_notice = (
            "연결된 비공개 워크스페이스를 찾지 못해 스레드 초대는 건너뛰었습니다."
        )
        if thread is not None:
            invite_notice = await self.cog.add_member_to_private_thread(
                interaction.guild, thread, self.applicant_id
            )
            if invite_notice is None:
                invite_notice = (
                    f"<@{self.applicant_id}> 님을 비공개 워크스페이스에 초대했습니다."
                )
        notice = invite_notice
        if auto_closed:
            notice += "\n정원이 모두 차서 모집을 자동으로 마감했습니다."
        if message_notice:
            notice += f"\n{message_notice}"

        await self.finish(
            interaction,
            recruitment,
            title="✅ 승인 완료",
            color=SUCCESS_COLOR,
            notice=notice,
        )
        await interaction.followup.send(notice, ephemeral=True)

    @discord.ui.button(
        label="❌ 거절",
        style=discord.ButtonStyle.danger,
        custom_id="0x34:recruitment:application:reject",
    )
    async def reject(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        """신청자를 rejected 상태로 바꾸고 스레드 신청 카드에 처리 내역을 남깁니다."""
        await interaction.response.defer(ephemeral=True, thinking=True)
        recruitment = await self.cog.get_recruitment_by_id(self.recruitment_id)
        if recruitment is None:
            await interaction.followup.send(
                "모집 정보를 찾을 수 없습니다.", ephemeral=True
            )
            return

        await self.cog.save_participant_status(
            self.recruitment_id, self.applicant_id, PARTICIPANT_REJECTED
        )
        message_notice = await self.cog.edit_recruitment_message(
            int(recruitment["channel_id"]), int(recruitment["message_id"])
        )
        notice = f"<@{self.applicant_id}> 신청을 거절했습니다."
        if message_notice:
            notice += f"\n{message_notice}"
        await self.finish(
            interaction,
            recruitment,
            title="❌ 거절됨",
            color=STOP_COLOR,
            notice=notice,
        )
        await interaction.followup.send(notice, ephemeral=True)

    async def finish(
        self,
        interaction: discord.Interaction,
        recruitment,
        *,
        title: str,
        color: int,
        notice: str,
    ) -> None:
        self.disable_buttons()
        embed = self.cog.build_application_manage_embed(
            recruitment,
            applicant_id=self.applicant_id,
            application_reason=self.application_reason,
            title=title,
            color=color,
            decided_by=interaction.user.mention,
            notice=notice,
        )
        if interaction.message is not None:
            await interaction.message.edit(embed=embed, view=self)


class AcceptedParticipantRemoveSelect(discord.ui.Select):
    """관리하기 메뉴에서 승인된 참가자를 다시 제거하는 Select입니다."""

    def __init__(
        self,
        cog: "RecruitmentCog",
        recruitment_id: int,
        channel_id: int,
        message_id: int,
        participants: list[dict],
        guild: discord.Guild,
    ) -> None:
        self.cog = cog
        self.recruitment_id = recruitment_id
        self.channel_id = channel_id
        self.message_id = message_id

        options: list[discord.SelectOption] = []
        for row in participants[:25]:
            user_id = int(row["user_id"])
            member = guild.get_member(user_id)
            label = (
                member.display_name if member is not None else f"사용자 {user_id}"
            )[:100]
            options.append(
                discord.SelectOption(
                    label=label,
                    description=f"ID {user_id} · 승인 참가자에서 제거"[:100],
                    value=str(user_id),
                )
            )

        super().__init__(
            placeholder="제거할 승인 참가자를 선택하세요.",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def _finish(self, interaction: discord.Interaction, message: str) -> None:
        try:
            await interaction.edit_original_response(content=message, view=None)
        except discord.HTTPException:
            await interaction.followup.send(message, ephemeral=True)

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        removed_user_id = int(self.values[0])

        recruitment = await self.cog.get_recruitment_by_id(self.recruitment_id)
        if recruitment is None:
            await self._finish(interaction, "모집 정보를 찾을 수 없습니다.")
            return
        if not await self.cog.is_recruitment_owner(
            self.recruitment_id, interaction.user.id
        ):
            await self._finish(
                interaction, "모집 소유자만 승인 참가자를 제거할 수 있습니다."
            )
            return

        participant = await self.cog.get_participant(
            self.recruitment_id, removed_user_id
        )
        if participant is None or participant["status"] != PARTICIPANT_ACCEPTED:
            await self._finish(
                interaction, "이미 제거되었거나 승인 상태가 아닌 참가자입니다."
            )
            return

        await self.cog.save_participant_status(
            self.recruitment_id,
            removed_user_id,
            PARTICIPANT_REJECTED,
            rejection_reason="관리하기 메뉴에서 승인 참가자에서 제거되었습니다.",
        )
        thread_notice = await self.cog.sync_private_thread_membership(
            recruitment,
            discord.Object(id=removed_user_id),
            PARTICIPANT_REJECTED,
        )
        message_notice = await self.cog.edit_recruitment_message(
            self.channel_id, self.message_id
        )

        notice = f"<@{removed_user_id}> 님을 승인 참가자에서 제거했습니다."
        if thread_notice:
            notice += f"\n{thread_notice}"
        if message_notice:
            notice += f"\n{message_notice}"
        await self._finish(interaction, notice)


class ReopenRecruitmentButton(discord.ui.Button):
    """관리하기 메뉴에서 마감된 모집을 다시 여는 Button입니다."""

    def __init__(
        self,
        cog: "RecruitmentCog",
        recruitment_id: int,
        channel_id: int,
        message_id: int,
    ) -> None:
        super().__init__(label="마감 취소", style=discord.ButtonStyle.primary)
        self.cog = cog
        self.recruitment_id = recruitment_id
        self.channel_id = channel_id
        self.message_id = message_id

    async def _finish(self, interaction: discord.Interaction, message: str) -> None:
        try:
            await interaction.edit_original_response(content=message, view=None)
        except discord.HTTPException:
            await interaction.followup.send(message, ephemeral=True)

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()

        recruitment = await self.cog.get_recruitment_by_id(self.recruitment_id)
        if recruitment is None:
            await self._finish(interaction, "모집 정보를 찾을 수 없습니다.")
            return
        if not await self.cog.is_recruitment_owner(
            self.recruitment_id, interaction.user.id
        ):
            await self._finish(interaction, "모집 소유자만 마감을 취소할 수 있습니다.")
            return
        if recruitment["status"] != STATUS_CLOSED:
            await self._finish(interaction, "이미 모집 중인 글입니다.")
            return

        await self.cog.reopen_recruitment(self.recruitment_id)
        message_notice = await self.cog.edit_recruitment_message(
            self.channel_id, self.message_id
        )

        notice = "모집 마감을 취소했습니다. 신청/마감 버튼이 다시 활성화되었습니다."
        if message_notice:
            notice += f"\n{message_notice}"
        await self._finish(interaction, notice)


class AcceptedParticipantManageView(discord.ui.View):
    """관리하기 버튼에서 승인 참가자 제거와 마감 취소를 처리하는 Ephemeral View입니다."""

    def __init__(
        self,
        cog: "RecruitmentCog",
        recruitment_id: int,
        channel_id: int,
        message_id: int,
        participants: list[dict],
        guild: discord.Guild,
        recruitment_status: str,
    ) -> None:
        super().__init__(timeout=120)
        self.cog = cog
        self.recruitment_id = recruitment_id
        if participants:
            self.add_item(
                AcceptedParticipantRemoveSelect(
                    cog, recruitment_id, channel_id, message_id, participants, guild
                )
            )
        if recruitment_status == STATUS_CLOSED:
            self.add_item(
                ReopenRecruitmentButton(cog, recruitment_id, channel_id, message_id)
            )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if await self.cog.is_recruitment_owner(
            self.recruitment_id, interaction.user.id
        ):
            return True
        await interaction.response.send_message(
            "모집 소유자만 이 관리 메뉴를 사용할 수 있습니다.", ephemeral=True
        )
        return False


class RecruitmentModal(discord.ui.Modal, title="팀원 모집"):
    """모집 Embed를 만들기 위한 정보를 입력받는 Modal입니다."""

    title_input = discord.ui.TextInput(
        label="모집 제목", placeholder="예: DEF CON Quals 팀원 모집", max_length=100
    )
    target_input = discord.ui.TextInput(
        label="대회/프로젝트 설명",
        placeholder="모집 목적, 기간, 필요한 역할 등을 적어 주세요.",
        style=discord.TextStyle.long,
        max_length=4000,
    )
    max_members_input = discord.ui.TextInput(
        label="정원", placeholder="예: 4", default="4", max_length=3
    )

    def __init__(self, cog: "RecruitmentCog") -> None:
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        """모집 정보를 DB에 만들고 버튼이 달린 Embed 메시지를 전송합니다."""
        if interaction.guild is None:
            await interaction.response.send_message(
                "서버 안에서만 모집을 만들 수 있습니다.", ephemeral=True
            )
            return

        try:
            max_members = int(str(self.max_members_input.value).strip())
        except ValueError:
            await interaction.response.send_message(
                "정원은 숫자로 입력해 주세요.", ephemeral=True
            )
            return
        if max_members < 0:
            await interaction.response.send_message(
                "정원은 0 이상이어야 합니다. 0은 제한 없음입니다.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        message, _, thread_notice = await self.cog.post_recruitment_message(
            interaction,
            title=str(self.title_input.value),
            target=str(self.target_input.value),
            max_members=max_members,
            use_deferred_response=False,
        )
        response = f"모집 글을 만들었습니다: {message.jump_url}"
        if thread_notice:
            response += f"\n{thread_notice}"
        await interaction.followup.send(response, ephemeral=True)


class RecruitmentEditModal(discord.ui.Modal, title="모집 수정"):
    """기존 모집 데이터를 TextInput default로 채워서 여는 수정 Modal입니다."""

    def __init__(
        self,
        cog: "RecruitmentCog",
        row,
        source_message: discord.Message | None,
        user_id: int,
    ) -> None:
        super().__init__()
        self.cog = cog
        self.recruitment_id = int(row["id"])
        self.channel_id = int(row["channel_id"])
        self.message_id = int(row["message_id"])
        self.source_message = source_message
        self.user_id = user_id

        self.title_input = discord.ui.TextInput(
            label="모집 제목",
            placeholder="예: DEF CON Quals 팀원 모집",
            default=str(row["title"])[:100],
            max_length=100,
        )
        self.target_input = discord.ui.TextInput(
            label="대회/프로젝트 설명",
            placeholder="모집 목적, 기간, 필요한 역할 등을 적어 주세요.",
            default=_trim_text(str(row["target"]), MAX_EMBED_DESCRIPTION_LENGTH),
            style=discord.TextStyle.long,
            max_length=MAX_EMBED_DESCRIPTION_LENGTH,
        )
        self.max_members_input = discord.ui.TextInput(
            label="정원",
            placeholder="예: 4",
            default=str(row["max_members"])[:6],
            max_length=6,
        )
        self.add_item(self.title_input)
        self.add_item(self.target_input)
        self.add_item(self.max_members_input)

    async def disable_source_view(self) -> None:
        if self.source_message is None:
            return
        try:
            await self.source_message.edit(view=None)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "서버 안에서만 모집을 수정할 수 있습니다.", ephemeral=True
            )
            return
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "이 모집 수정 창은 명령어를 실행한 사람만 제출할 수 있습니다.",
                ephemeral=True,
            )
            return

        try:
            max_members = int(str(self.max_members_input.value).strip())
        except ValueError:
            await interaction.response.send_message(
                "정원은 숫자로 입력해 주세요.", ephemeral=True
            )
            return
        if max_members < 0:
            await interaction.response.send_message(
                "정원은 0 이상이어야 합니다. 0은 제한 없음입니다.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        row = await self.cog.bot.database.fetch_one(
            """
            SELECT * FROM recruitments
            WHERE id = ? AND guild_id = ?
            """,
            (self.recruitment_id, interaction.guild.id),
        )
        if row is None:
            await self.disable_source_view()
            await interaction.followup.send(
                "수정할 모집 글을 찾을 수 없습니다.", ephemeral=True
            )
            return
        if row["author_id"] != interaction.user.id and row["status"] != STATUS_OPEN:
            await interaction.followup.send(
                "모집 작성자이거나 현재 모집 중인 글만 수정할 수 있습니다.",
                ephemeral=True,
            )
            return

        title = str(self.title_input.value).strip()
        target = _trim_text(str(self.target_input.value), MAX_EMBED_DESCRIPTION_LENGTH)

        # aiosqlite UPDATE를 먼저 실행해 DB를 최신 상태로 만든 뒤 Embed를 다시 빌드합니다.
        # Database.execute()는 내부에서 commit까지 수행하므로, 아래 build_recruitment_embed()는
        # 방금 저장한 title/target/max_members 값을 같은 DB 연결에서 바로 읽을 수 있습니다.
        await self.cog.bot.database.execute(
            """
            UPDATE recruitments
            SET title = ?, target = ?, max_members = ?
            WHERE id = ? AND guild_id = ?
            """,
            (title, target, max_members, self.recruitment_id, interaction.guild.id),
        )
        await self.cog.close_recruitment_if_full(self.recruitment_id)

        message_notice = await self.cog.edit_recruitment_message(
            self.channel_id, self.message_id
        )
        await self.disable_source_view()

        response = "✅ 성공적으로 수정되었습니다."
        if message_notice:
            response += f"\n{message_notice}"
        await interaction.followup.send(response, ephemeral=True)


class RecruitmentEditSelect(discord.ui.Select):
    """수정할 모집 글을 선택하는 드롭다운입니다."""

    def __init__(self, cog: "RecruitmentCog", rows: list) -> None:
        self.cog = cog
        options: list[discord.SelectOption] = []
        for row in rows:
            status_text = "모집 중" if row["status"] == STATUS_OPEN else "모집 마감"
            capacity_text = (
                "제한 없음"
                if row["max_members"] == 0
                else f"정원 {row['max_members']}명"
            )
            options.append(
                discord.SelectOption(
                    label=str(row["title"])[:100],
                    description=f"{status_text} · {capacity_text}"[:100],
                    value=str(row["id"]),
                )
            )

        super().__init__(
            placeholder="수정할 모집 글을 선택하세요.",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.edit_message(
                content="서버 안에서만 모집을 수정할 수 있습니다.",
                embed=None,
                view=None,
            )
            return

        recruitment_id = int(self.values[0])
        row = await self.cog.bot.database.fetch_one(
            """
            SELECT * FROM recruitments
            WHERE id = ? AND guild_id = ?
            """,
            (recruitment_id, interaction.guild.id),
        )
        if row is None:
            await interaction.response.edit_message(
                content="이미 삭제되었거나 찾을 수 없는 모집 글입니다.",
                embed=None,
                view=None,
            )
            return
        if row["author_id"] != interaction.user.id and row["status"] != STATUS_OPEN:
            await interaction.response.send_message(
                "모집 작성자이거나 현재 모집 중인 글만 수정할 수 있습니다.",
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(
            RecruitmentEditModal(
                self.cog, row, interaction.message, interaction.user.id
            )
        )


class RecruitmentEditView(discord.ui.View):
    """모집 수정 Select를 담는 Ephemeral View입니다."""

    def __init__(self, cog: "RecruitmentCog", rows: list, user_id: int) -> None:
        super().__init__(timeout=120)
        self.user_id = user_id
        self.add_item(RecruitmentEditSelect(cog, rows))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.user_id:
            return True
        await interaction.response.send_message(
            "이 모집 수정 메뉴는 명령어를 실행한 사람만 사용할 수 있습니다.",
            ephemeral=True,
        )
        return False


class RecruitmentCog(commands.Cog):
    """팀 빌딩과 참가 버튼 업데이트를 담당하는 Cog입니다."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.recruitment_service = RecruitmentService(self.bot.database)
        self.bot.add_view(RecruitmentView(self))

    def build_recruitment_view(self, recruitment) -> RecruitmentView:
        view = RecruitmentView(self)
        if recruitment is not None and recruitment["status"] == STATUS_CLOSED:
            view.disable_for_closed_recruitment()
        return view

    async def resolve_recruitment_channel(
        self, interaction: discord.Interaction
    ) -> discord.abc.Messageable:
        """환경 변수에 모집 채널이 있으면 그 채널을, 없으면 현재 채널을 사용합니다."""
        channel_id = self.bot.settings.recruitment_channel_id
        if interaction.guild is not None and channel_id is not None:
            channel = interaction.guild.get_channel(channel_id)
            if channel is None:
                channel = await self.bot.fetch_channel(channel_id)
            if isinstance(channel, discord.abc.Messageable):
                return channel

        if interaction.channel is None or not isinstance(
            interaction.channel, discord.abc.Messageable
        ):
            raise RuntimeError("모집 글을 보낼 채널을 찾을 수 없습니다.")
        return interaction.channel

    async def post_recruitment_message(
        self,
        interaction: discord.Interaction,
        *,
        title: str,
        target: str,
        max_members: int,
        use_deferred_response: bool,
    ) -> tuple[discord.Message, bool, str | None]:
        """DB 저장과 버튼 달린 모집 메시지 생성을 한 곳에서 처리합니다.

        `/모집`과 `/모집생성`이 같은 테이블, 같은 Embed 빌더, 같은 Persistent View를 사용해야
        버튼 클릭 시 참가자 목록이 동일한 방식으로 업데이트됩니다.
        """
        if interaction.guild is None:
            raise RuntimeError("서버 안에서만 모집을 만들 수 있습니다.")

        channel = await self.resolve_recruitment_channel(interaction)
        placeholder = base_embed("모집을 준비하는 중입니다.")
        view = RecruitmentView(self)

        # /모집생성은 이미 공개 defer를 했으므로, 같은 채널이라면 그 deferred 응답 자체를 모집 메시지로 씁니다.
        # 이렇게 하면 Gemini를 기다리는 동안 Discord 3초 제한을 피하면서도 불필요한 안내 메시지를 만들지 않습니다.
        should_use_followup = (
            use_deferred_response
            and interaction.channel is not None
            and getattr(channel, "id", None) == interaction.channel.id
        )
        if should_use_followup:
            message = await interaction.followup.send(
                embed=placeholder, view=view, wait=True
            )
        else:
            message = await channel.send(embed=placeholder, view=view)

        recruitment_id = await self.recruitment_service.create_recruitment(
            CreateRecruitmentRequest(
                guild_id=interaction.guild.id,
                channel_id=message.channel.id,
                message_id=message.id,
                author_id=interaction.user.id,
                title=title,
                target=target,
                max_members=max_members,
            )
        )

        thread, thread_notice = await self.create_private_workspace_thread(
            interaction, channel, title, message
        )
        if thread is not None:
            await self.recruitment_service.update_thread_id(recruitment_id, thread.id)

        await self.close_recruitment_if_full(recruitment_id)
        recruitment = await self.get_recruitment_by_id(recruitment_id)
        embed = await self.build_recruitment_embed(message.id)
        await message.edit(embed=embed, view=self.build_recruitment_view(recruitment))
        if thread_notice:
            logging.info(
                "Private recruitment thread notice for message %s: %s",
                message.id,
                thread_notice,
            )
        return message, should_use_followup, thread_notice

    async def create_private_workspace_thread(
        self,
        interaction: discord.Interaction,
        channel: discord.abc.Messageable,
        title: str,
        source_message: discord.Message,
    ) -> tuple[discord.Thread | None, str | None]:
        """모집 전용 비공개 스레드를 만들고 작성자를 즉시 초대합니다.

        비공개 스레드를 만들려면 봇이 해당 텍스트 채널에서 `Create Private Threads`와
        `Send Messages in Threads` 권한을 가져야 합니다. 서버 설정이나 채널 오버라이드에 따라
        멤버 초대/아카이브된 스레드 관리를 위해 `Manage Threads` 권한이 추가로 필요할 수 있습니다.
        또한 Discord 정책상 일부 서버 기능은 부스트 레벨 2 이상에서만 안정적으로 사용할 수 있습니다.
        """
        if not isinstance(channel, discord.TextChannel):
            return None, "비공개 스레드는 일반 텍스트 채널에서만 생성할 수 있습니다."

        thread_name = _trim_text(f"{title} 워크스페이스", 90)
        try:
            thread = await channel.create_thread(
                name=thread_name,
                type=discord.ChannelType.private_thread,
                auto_archive_duration=1440,
                invitable=False,
                reason="Team 0x34 private recruitment workspace",
            )
        except discord.Forbidden:
            return (
                None,
                "봇에게 비공개 스레드 생성 권한이 없습니다. Create Private Threads, Send Messages in Threads, Manage Threads 권한을 확인해 주세요.",
            )
        except discord.HTTPException as exc:
            return (
                None,
                f"비공개 스레드 생성에 실패했습니다. 서버 부스트 레벨 또는 Discord API 제한을 확인해 주세요: {exc.text}",
            )

        author_notice = await self.add_user_to_private_thread(thread, interaction.user)
        intro_embed = base_embed(
            "Team 0x34 비공개 워크스페이스",
            "Team 0x34의 비공개 워크스페이스가 생성되었습니다.",
            color=SUCCESS_COLOR,
        )
        intro_embed.add_field(
            name="모집 글", value=source_message.jump_url, inline=False
        )
        intro_embed.add_field(
            name="접근 안내",
            value="작성자와 승인된 신청자만 이 스레드에 초대됩니다.",
            inline=False,
        )

        try:
            await thread.send(embed=intro_embed)
        except discord.HTTPException as exc:
            return (
                thread,
                f"비공개 스레드는 만들었지만 안내 Embed 전송에 실패했습니다: {exc.text}",
            )

        return thread, author_notice

    async def ensure_private_workspace_thread(
        self,
        recruitment,
        interaction: discord.Interaction,
        source_message: discord.Message,
    ) -> tuple[discord.Thread | None, str | None]:
        """기존 비공개 스레드를 찾고, 없으면 새로 만들어 DB에 저장합니다."""
        thread_id = recruitment["thread_id"]
        if thread_id is not None:
            thread = await self.fetch_private_thread(int(thread_id))
            if thread is not None:
                return thread, await self.add_user_to_private_thread(
                    thread, interaction.user
                )

        thread, notice = await self.create_private_workspace_thread(
            interaction, source_message.channel, recruitment["title"], source_message
        )
        if thread is not None:
            await self.recruitment_service.update_thread_id(
                int(recruitment["id"]), thread.id
            )
        return thread, notice

    async def fetch_private_thread(self, thread_id: int) -> discord.Thread | None:
        """저장된 스레드 ID로 Thread 객체를 가져옵니다."""
        channel = self.bot.get_channel(thread_id)
        if isinstance(channel, discord.Thread):
            return channel

        try:
            fetched = await self.bot.fetch_channel(thread_id)
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            return None
        if isinstance(fetched, discord.Thread):
            return fetched
        return None

    async def get_recruitment_thread(
        self, recruitment, guild: discord.Guild | None
    ) -> discord.Thread | None:
        """모집에 연결된 Thread를 guild cache에서 먼저 찾고, 없으면 API로 조회합니다."""
        thread_id = recruitment["thread_id"]
        if thread_id is None:
            return None

        if guild is not None:
            thread = guild.get_thread(int(thread_id))
            if thread is not None:
                return thread

        return await self.fetch_private_thread(int(thread_id))

    async def add_member_to_private_thread(
        self, guild: discord.Guild, thread: discord.Thread, user_id: int
    ) -> str | None:
        """user_id를 정확한 Member 객체로 조회한 뒤 비공개 스레드에 초대합니다."""
        try:
            member = await guild.fetch_member(int(user_id))
        except discord.NotFound:
            return "신청자를 서버 멤버 목록에서 찾을 수 없어 비공개 워크스페이스에 초대하지 못했습니다."
        except discord.Forbidden:
            return "봇에게 서버 멤버 조회 권한이 없어 비공개 워크스페이스에 초대하지 못했습니다."
        except discord.HTTPException as exc:
            return f"서버 멤버 조회 중 오류가 발생해 비공개 워크스페이스에 초대하지 못했습니다: {exc.text}"

        try:
            await thread.add_user(member)
        except discord.Forbidden:
            return "비공개 스레드 초대 권한이 없어 워크스페이스에 자동 초대하지 못했습니다. Manage Threads 권한을 확인해 주세요."
        except discord.HTTPException as exc:
            return f"비공개 스레드 초대에 실패했습니다: {exc.text}"
        return None

    async def add_user_to_private_thread(
        self, thread: discord.Thread, user: discord.abc.Snowflake
    ) -> str | None:
        """비공개 스레드에 사용자를 초대하고 실패 사유를 사용자에게 보여줄 문구로 반환합니다."""
        if not isinstance(user, discord.Member):
            return await self.add_member_to_private_thread(
                thread.guild, thread, int(user.id)
            )

        try:
            await thread.add_user(user)
        except discord.Forbidden:
            return "비공개 스레드 초대 권한이 없어 워크스페이스에 자동 초대하지 못했습니다. Manage Threads 권한을 확인해 주세요."
        except discord.HTTPException as exc:
            return f"비공개 스레드 초대에 실패했습니다: {exc.text}"
        return None

    async def remove_user_from_private_thread(
        self, thread: discord.Thread, user: discord.abc.Snowflake
    ) -> str | None:
        """참가를 취소한 사용자가 비공개 워크스페이스에 계속 남지 않도록 제거합니다."""
        try:
            await thread.remove_user(user)
        except discord.Forbidden:
            return "비공개 스레드에서 사용자를 제거할 권한이 없습니다. Manage Threads 권한을 확인해 주세요."
        except discord.NotFound:
            return None
        except discord.HTTPException as exc:
            return f"비공개 스레드 멤버 제거에 실패했습니다: {exc.text}"
        return None

    async def sync_private_thread_membership(
        self, recruitment, user: discord.abc.Snowflake, status: str
    ) -> str | None:
        """신청 승인/거절 상태와 비공개 스레드 멤버십을 맞춥니다."""
        thread_id = recruitment["thread_id"]
        if thread_id is None:
            return "비공개 워크스페이스가 아직 없어 스레드 초대는 건너뛰었습니다."

        thread = await self.fetch_private_thread(int(thread_id))
        if thread is None:
            return "저장된 비공개 워크스페이스를 찾을 수 없습니다. 작성자가 모집 마감 시 다시 생성할 수 있습니다."

        if status in {PARTICIPANT_ACCEPTED, PARTICIPANT_OWNER}:
            notice = await self.add_user_to_private_thread(thread, user)
            return notice or f"비공개 워크스페이스에 초대했습니다: {thread.mention}"

        if user.id == recruitment["author_id"]:
            return None
        return await self.remove_user_from_private_thread(thread, user)

    async def generate_recruitment_copy_text(self, source_text: str) -> str:
        """AI Provider를 통해 모집글 JSON 원문을 생성합니다."""
        response = await self.bot.ai_provider.generate(
            AIRequest(
                system_instruction=build_recruitment_system_prompt(),
                response_mime_type="application/json",
                temperature=0.4,
                prompt=(
                    "다음은 사용자가 자유롭게 제공한 대화형 입력과 URL 크롤링 내용을 합친 원문입니다. "
                    "사용자의 요청 의도와 어조를 유지하면서 모집글을 작성해라: "
                    f"\n\n{source_text}\n\n"
                    "이 텍스트 내용만을 엄격하게 바탕으로, 없는 내용을 지어내지 말고 다음 규칙에 따라 모집글을 작성해라."
                ),
            )
        )
        return response.text

    async def generate_recruitment_copy(self, source_text: str) -> tuple[str, str, int]:
        """AI 응답을 Embed용 데이터로 파싱합니다."""
        raw_text = await self.generate_recruitment_copy_text(source_text)
        return parse_gemini_recruitment(raw_text, source_text)

    async def prepare_recruitment_source_text(self, target_info: str) -> str:
        """입력 텍스트에 포함된 여러 URL을 동시에 크롤링하고 일반 텍스트와 병합합니다."""
        return await prepare_conversational_source_text(
            target_info,
            max_length=MAX_RECRUITMENT_SOURCE_TEXT_LENGTH,
            logger=logging.getLogger(__name__),
        )

    async def get_recruitment(self, message_id: int):
        """메시지 ID로 owner가 포함된 모집 레코드를 찾습니다."""
        return await self.recruitment_service.get_recruitment_by_message_id(message_id)

    async def get_recruitment_by_id(self, recruitment_id: int):
        """모집 ID로 owner가 포함된 모집 레코드를 찾습니다."""
        return await self.recruitment_service.get_recruitment_by_id(recruitment_id)

    async def get_participant(self, recruitment_id: int, user_id: int):
        """특정 모집의 신청자 상태를 가져옵니다."""
        return await self.recruitment_service.get_participant(recruitment_id, user_id)

    async def save_participant_status(
        self,
        recruitment_id: int,
        user_id: int,
        status: str,
        *,
        application_reason: str | None = None,
        rejection_reason: str | None = None,
    ) -> None:
        """신청자 상태를 upsert합니다."""
        await self.recruitment_service.save_participant_status(
            recruitment_id,
            user_id,
            status,
            application_reason=application_reason,
            rejection_reason=rejection_reason,
        )

    async def is_recruitment_owner(self, recruitment_id: int, user_id: int) -> bool:
        """DB의 participant status=owner를 기준으로 모집 소유자 권한을 확인합니다."""
        return await self.recruitment_service.is_owner(recruitment_id, user_id)

    async def get_participant_user_ids(
        self, recruitment_id: int, status: str
    ) -> list[int]:
        """특정 신청 상태인 유저 ID 목록을 가져옵니다."""
        return await self.recruitment_service.get_participant_user_ids(
            recruitment_id, status
        )

    async def get_confirmed_participant_user_ids(
        self, recruitment_id: int
    ) -> list[int]:
        """owner와 accepted를 함께 현재 참가자로 계산합니다."""
        return await self.recruitment_service.get_confirmed_participant_user_ids(
            recruitment_id
        )

    async def get_confirmed_participants(self, recruitment_id: int) -> list[dict]:
        """Embed 표시용 현재 참가자(owner + accepted)를 DB에서 직접 조회합니다."""
        return await self.recruitment_service.get_confirmed_participants(recruitment_id)

    async def close_recruitment_if_full(self, recruitment_id: int) -> bool:
        """정원이 있는 모집에서 현재 참가자가 정원에 도달하면 모집을 마감합니다."""
        recruitment = await self.get_recruitment_by_id(recruitment_id)
        if recruitment is None or recruitment["status"] == STATUS_CLOSED:
            return False

        max_members = int(recruitment["max_members"])
        if max_members == 0:
            return False

        confirmed = await self.get_confirmed_participant_user_ids(recruitment_id)
        if len(confirmed) < max_members:
            return False

        await self.recruitment_service.close_recruitment(recruitment_id)
        return True

    async def reopen_recruitment(self, recruitment_id: int) -> None:
        """마감된 모집을 다시 모집 중 상태로 되돌립니다."""
        await self.recruitment_service.reopen_recruitment(recruitment_id)

    async def get_pending_participant_count(self, recruitment_id: int) -> int:
        """Embed 표시용 신청 대기 인원을 DB에서 직접 조회합니다."""
        return await self.recruitment_service.get_pending_participant_count(
            recruitment_id
        )

    def build_application_manage_embed(
        self,
        recruitment,
        *,
        applicant_id: int,
        application_reason: str,
        applicant_mention: str | None = None,
        title: str = "새로운 참가 신청",
        color: int = WARNING_COLOR,
        decided_by: str | None = None,
        notice: str | None = None,
    ) -> discord.Embed:
        """비공개 워크스페이스에 남길 개별 신청 관리 Embed를 만듭니다."""
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

    async def notify_recruitment_owner(
        self, recruitment, applicant: discord.abc.User, reason: str
    ) -> str | None:
        """새 신청을 비공개 스레드 또는 작성자 DM으로 알립니다."""
        message = f"새 모집 신청이 도착했습니다.\n신청자: {applicant.mention}\n신청 사유: {reason or '작성되지 않음'}"
        thread_id = recruitment["thread_id"]
        if thread_id is not None:
            thread = await self.fetch_private_thread(int(thread_id))
            if thread is not None:
                try:
                    await thread.send(message)
                    return None
                except discord.HTTPException:
                    pass

        try:
            owner = self.bot.get_user(
                int(recruitment["author_id"])
            ) or await self.bot.fetch_user(int(recruitment["author_id"]))
            await owner.send(message)
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            return "작성자에게 DM 알림을 보내지 못했습니다."
        return None

    async def edit_recruitment_message(
        self, channel_id: int, message_id: int
    ) -> str | None:
        """DB에 저장된 채널/메시지 ID로 기존 모집 Embed 메시지를 찾아 수정합니다."""
        try:
            channel = self.bot.get_channel(int(channel_id))
            if channel is None:
                channel = await self.bot.fetch_channel(int(channel_id))
            if not hasattr(channel, "fetch_message"):
                return "저장된 채널에서 모집 메시지를 가져올 수 없어 Discord 메시지는 수정하지 못했습니다."

            message = await channel.fetch_message(int(message_id))
            embed = await self.update_recruitment_embed(int(message_id))
            recruitment = await self.get_recruitment(int(message_id))
            await message.edit(
                embed=embed, view=self.build_recruitment_view(recruitment)
            )
        except (TypeError, ValueError):
            return "저장된 채널 또는 메시지 ID가 올바르지 않아 Discord 메시지는 수정하지 못했습니다."
        except discord.NotFound:
            return "기존 모집 메시지를 찾을 수 없어 DB만 수정했습니다."
        except discord.Forbidden:
            return "모집 메시지를 수정할 권한이 없어 DB만 수정했습니다."
        except discord.HTTPException as exc:
            return f"모집 메시지 수정 중 오류가 발생해 DB만 수정했습니다: {exc.text}"
        return None

    async def build_recruitment_embed(self, message_id: int) -> discord.Embed:
        """Backward-compatible wrapper for older call sites."""
        return await self.update_recruitment_embed(message_id)

    async def update_recruitment_embed(self, message_id: int) -> discord.Embed:
        """현재 DB 상태를 읽어 모집 Embed를 다시 만듭니다."""
        recruitment = await self.get_recruitment(message_id)
        if recruitment is None:
            return base_embed("모집 정보를 찾을 수 없습니다.", color=STOP_COLOR)

        confirmed = await self.get_confirmed_participants(int(recruitment["id"]))
        pending_count = await self.get_pending_participant_count(int(recruitment["id"]))
        participant_lines: list[str] = []
        seen_user_ids: set[int] = set()
        for row in confirmed:
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
        max_members = recruitment["max_members"]
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
            name="참가자", value=_trim_text(participant_text, 1024), inline=False
        )
        embed.add_field(name="신청 대기", value=f"{pending_count}명", inline=True)
        return embed

    @app_commands.command(
        name="모집", description="팀원 모집 Embed와 참가 버튼을 생성합니다."
    )
    async def create_recruitment(self, interaction: discord.Interaction) -> None:
        """모집 생성 Modal을 엽니다."""
        await interaction.response.send_modal(RecruitmentModal(self))

    @app_commands.command(
        name="모집수정", description="드롭다운 메뉴와 Modal로 모집 글을 수정합니다."
    )
    async def edit_recruitment(self, interaction: discord.Interaction) -> None:
        """내가 작성했거나 현재 모집 중인 글 최대 25개를 Select Menu로 보여줍니다."""
        if interaction.guild is None:
            await interaction.response.send_message(
                "서버 안에서만 모집을 수정할 수 있습니다.", ephemeral=True
            )
            return

        rows = await self.bot.database.fetch_all(
            """
            SELECT id, title, target, max_members, status, author_id, channel_id, message_id FROM recruitments
            WHERE guild_id = ? AND (author_id = ? OR status = ?)
            ORDER BY created_at DESC
            LIMIT 25
            """,
            (interaction.guild.id, interaction.user.id, STATUS_OPEN),
        )

        if not rows:
            await interaction.response.send_message(
                "수정할 모집 글이 없습니다.", ephemeral=True
            )
            return

        embed = base_embed(
            "수정할 모집 글을 선택하세요",
            "내가 작성했거나 현재 모집 중인 글이 최대 25개까지 표시됩니다.",
            color=WARNING_COLOR,
        )
        await interaction.response.send_message(
            embed=embed,
            view=RecruitmentEditView(self, rows, interaction.user.id),
            ephemeral=True,
        )

    @app_commands.command(
        name="모집생성",
        description="Gemini로 대회/해커톤 정보를 분석해 모집 글을 생성합니다.",
    )
    @app_commands.describe(target_info="대회/해커톤 웹사이트 링크 또는 상세 텍스트")
    async def create_ai_recruitment(
        self, interaction: discord.Interaction, target_info: str
    ) -> None:
        """Gemini가 만든 모집 글을 기존 모집 버튼 로직과 연결합니다."""
        # Gemini 응답은 3초를 넘길 수 있으므로 명령어가 들어오자마자 공개 defer를 보냅니다.
        # 이 줄이 늦게 실행되면 Discord가 "상호작용 실패"로 처리할 수 있습니다.
        await interaction.response.defer(ephemeral=False, thinking=True)

        if interaction.guild is None:
            await interaction.followup.send(
                "서버 안에서만 모집을 만들 수 있습니다.", ephemeral=True
            )
            return

        target_info = target_info.strip()
        if not target_info:
            await interaction.followup.send(
                "target_info에는 링크나 상세 텍스트를 입력해 주세요.", ephemeral=True
            )
            return

        try:
            source_text = await self.prepare_recruitment_source_text(target_info)
        except ScrapingError:
            await interaction.followup.send(SCRAPING_ERROR_MESSAGE, ephemeral=True)
            return

        try:
            title, description, max_members = await self.generate_recruitment_copy(
                source_text
            )
        except exceptions.ResourceExhausted:
            await interaction.followup.send(GEMINI_RATE_LIMIT_MESSAGE, ephemeral=True)
            return
        except RuntimeError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        except Exception:
            logging.exception("Gemini recruitment generation failed")
            await interaction.followup.send(
                "Gemini API 호출 중 문제가 발생했습니다. API 키, 모델명, 할당량을 확인해 주세요.",
                ephemeral=True,
            )
            return

        message, used_deferred_response, thread_notice = (
            await self.post_recruitment_message(
                interaction,
                title=title,
                target=description,
                max_members=max_members,
                use_deferred_response=True,
            )
        )

        if not used_deferred_response:
            await interaction.followup.send(
                f"Gemini가 모집 글을 만들었습니다: {message.jump_url}"
            )
        if thread_notice:
            await interaction.followup.send(thread_notice, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    """discord.py가 이 파일을 Cog로 로드할 때 호출하는 함수입니다."""
    await bot.add_cog(RecruitmentCog(bot))
