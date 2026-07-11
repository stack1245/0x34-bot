from __future__ import annotations
import logging

import discord
from discord import app_commands
from discord.ext import commands
from google.api_core import exceptions

from services.recruitment import (
    CreateRecruitmentRequest,
    RecruitmentPermissionError,
    RecruitmentService,
    PARTICIPANT_ACCEPTED,
    PARTICIPANT_PENDING,
    PARTICIPANT_REJECTED,
    STATUS_CLOSED,
    STATUS_OPEN,
)
from services.recruitment_copy import (
    GEMINI_RATE_LIMIT_MESSAGE,
    MAX_EMBED_DESCRIPTION_LENGTH,
    SCRAPING_ERROR_MESSAGE,
    RecruitmentCopyService,
)
from services.recruitment_embeds import RecruitmentEmbedFactory
from services.recruitment_messages import RecruitmentMessageService
from services.recruitment_models import (
    ParticipantStatus,
    RecruitmentRecord,
    RecruitmentRow,
)
from services.recruitment_workspaces import RecruitmentWorkspaceService
from utils.ai_input import (
    ScrapingError,
    trim_text as _trim_text,
)
from utils.embeds import STOP_COLOR, SUCCESS_COLOR, WARNING_COLOR, base_embed


class RecruitmentView(discord.ui.View):
    """모집 메시지 아래에 붙는 Persistent Button View입니다."""

    APPLY_BUTTON_ID = "0x34:recruitment:apply"
    MANAGE_BUTTON_ID = "0x34:recruitment:manage"
    STATUS_TOGGLE_BUTTON_ID = "0x34:recruitment:close"

    def __init__(
        self, cog: "RecruitmentCog", recruitment: RecruitmentRecord | None = None
    ) -> None:
        # timeout=None과 고정 custom_id를 쓰면 봇 재시작 후에도 버튼 이벤트를 받을 수 있습니다.
        super().__init__(timeout=None)
        self.cog = cog
        status = recruitment["status"] if recruitment is not None else STATUS_OPEN
        self.configure_status_controls(str(status))

    def configure_status_controls(self, status: str) -> None:
        """현재 모집 상태에 맞춰 신청/마감 토글 버튼을 렌더링합니다."""
        is_closed = status == STATUS_CLOSED
        for child in self.children:
            if not isinstance(child, discord.ui.Button):
                continue
            if child.custom_id == self.APPLY_BUTTON_ID:
                child.disabled = is_closed
            elif child.custom_id == self.STATUS_TOGGLE_BUTTON_ID:
                child.disabled = False
                if is_closed:
                    child.label = "마감 취소"
                    child.style = discord.ButtonStyle.success
                else:
                    child.label = "모집 마감"
                    child.style = discord.ButtonStyle.danger
            elif child.custom_id == self.MANAGE_BUTTON_ID:
                child.disabled = False

    def disable_buttons(self) -> None:
        """현재 모집 메시지의 버튼을 모두 비활성화합니다."""
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True

    def disable_for_closed_recruitment(self) -> None:
        """마감된 모집에서는 신청을 막고 마감 토글은 다시 열기로 바꿉니다."""
        self.configure_status_controls(STATUS_CLOSED)

    async def get_recruitment_from_message(
        self, interaction: discord.Interaction
    ) -> RecruitmentRecord | None:
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

    async def get_open_recruitment(
        self, interaction: discord.Interaction
    ) -> RecruitmentRecord | None:
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
        await interaction.response.send_message(
            f"{thread_text}\n{remove_text}{overflow_text}",
            view=view,
            ephemeral=True,
        )

    @discord.ui.button(
        label="모집 마감",
        style=discord.ButtonStyle.danger,
        custom_id="0x34:recruitment:close",
    )
    async def toggle_status_callback(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        """작성자가 모집 마감/마감 취소를 한 버튼에서 토글합니다."""
        recruitment = await self.get_recruitment_from_message(interaction)
        if recruitment is None:
            return
        was_closed = recruitment["status"] == STATUS_CLOSED
        try:
            updated_recruitment = (
                await self.cog.recruitment_service.toggle_recruitment_status_for_owner(
                    int(recruitment["id"]), interaction.user.id
                )
            )
        except RecruitmentPermissionError:
            await interaction.response.send_message(
                "모집 소유자만 마감 상태를 변경할 수 있습니다.", ephemeral=True
            )
            return

        updated_embed = await self.cog.build_recruitment_embed(
            recruitment["message_id"]
        )
        self.configure_status_controls(str(updated_recruitment["status"]))
        await interaction.response.edit_message(embed=updated_embed, view=self)

        if was_closed:
            await interaction.followup.send(
                "✅ 모집 마감을 취소했습니다. 신청하기 버튼이 다시 활성화되었습니다.",
                ephemeral=True,
            )
            return

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
        recruitment: RecruitmentRecord,
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
        participants: list[dict[str, object]],
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
                member.display_name
                if member is not None
                else f"알 수 없는 사용자 ({user_id})"
            )[:100]
            description = f"@{member.name}"[:100] if member is not None else None
            options.append(
                discord.SelectOption(
                    label=label,
                    description=description,
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


class AcceptedParticipantManageView(discord.ui.View):
    """관리하기 버튼에서 승인 참가자 제거를 처리하는 Ephemeral View입니다."""

    def __init__(
        self,
        cog: "RecruitmentCog",
        recruitment_id: int,
        channel_id: int,
        message_id: int,
        participants: list[dict],
        guild: discord.Guild,
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
        row: RecruitmentRow,
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
        row = await self.cog.recruitment_service.get_recruitment_row_for_guild(
            self.recruitment_id, interaction.guild.id
        )
        if row is None:
            await self.disable_source_view()
            await interaction.followup.send(
                "수정할 모집 글을 찾을 수 없습니다.", ephemeral=True
            )
            return
        if not self.cog.recruitment_service.can_edit_recruitment(
            row, interaction.user.id
        ):
            await interaction.followup.send(
                "모집 작성자이거나 현재 모집 중인 글만 수정할 수 있습니다.",
                ephemeral=True,
            )
            return

        title = str(self.title_input.value).strip()
        target = _trim_text(str(self.target_input.value), MAX_EMBED_DESCRIPTION_LENGTH)

        await self.cog.recruitment_service.update_recruitment_details(
            recruitment_id=self.recruitment_id,
            guild_id=interaction.guild.id,
            title=title,
            target=target,
            max_members=max_members,
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

    def __init__(self, cog: "RecruitmentCog", rows: list[RecruitmentRow]) -> None:
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
        row = await self.cog.recruitment_service.get_recruitment_row_for_guild(
            recruitment_id, interaction.guild.id
        )
        if row is None:
            await interaction.response.edit_message(
                content="이미 삭제되었거나 찾을 수 없는 모집 글입니다.",
                embed=None,
                view=None,
            )
            return
        if not self.cog.recruitment_service.can_edit_recruitment(
            row, interaction.user.id
        ):
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

    def __init__(
        self, cog: "RecruitmentCog", rows: list[RecruitmentRow], user_id: int
    ) -> None:
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
        self.copy_service = RecruitmentCopyService(
            self.bot.ai_provider, logger=logging.getLogger(__name__)
        )
        self.embed_factory = RecruitmentEmbedFactory()
        self.workspace_service = RecruitmentWorkspaceService(
            self.bot, self.recruitment_service, logger=logging.getLogger(__name__)
        )
        self.message_service = RecruitmentMessageService(
            self.bot,
            self.recruitment_service,
            self.embed_factory,
            self.build_recruitment_view,
            logger=logging.getLogger(__name__),
        )
        self.bot.add_view(RecruitmentView(self))

    def build_recruitment_view(
        self, recruitment: RecruitmentRecord | None
    ) -> RecruitmentView:
        return RecruitmentView(self, recruitment)

    async def resolve_recruitment_channel(
        self, interaction: discord.Interaction
    ) -> discord.abc.Messageable:
        """환경 변수에 모집 채널이 있으면 그 채널을, 없으면 현재 채널을 사용합니다."""
        return await self.workspace_service.resolve_recruitment_channel(interaction)

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
        """모집 전용 비공개 스레드를 만들고 작성자를 즉시 초대합니다."""
        return await self.workspace_service.create_private_workspace_thread(
            interaction, channel, title, source_message
        )

    async def ensure_private_workspace_thread(
        self,
        recruitment: RecruitmentRecord,
        interaction: discord.Interaction,
        source_message: discord.Message,
    ) -> tuple[discord.Thread | None, str | None]:
        """기존 비공개 스레드를 찾고, 없으면 새로 만들어 DB에 저장합니다."""
        return await self.workspace_service.ensure_private_workspace_thread(
            recruitment, interaction, source_message
        )

    async def fetch_private_thread(self, thread_id: int) -> discord.Thread | None:
        """저장된 스레드 ID로 Thread 객체를 가져옵니다."""
        return await self.workspace_service.fetch_private_thread(thread_id)

    async def get_recruitment_thread(
        self, recruitment: RecruitmentRecord, guild: discord.Guild | None
    ) -> discord.Thread | None:
        """모집에 연결된 Thread를 guild cache에서 먼저 찾고, 없으면 API로 조회합니다."""
        return await self.workspace_service.get_recruitment_thread(recruitment, guild)

    async def add_member_to_private_thread(
        self, guild: discord.Guild, thread: discord.Thread, user_id: int
    ) -> str | None:
        """user_id를 정확한 Member 객체로 조회한 뒤 비공개 스레드에 초대합니다."""
        return await self.workspace_service.add_member_to_private_thread(
            guild, thread, user_id
        )

    async def add_user_to_private_thread(
        self, thread: discord.Thread, user: discord.abc.Snowflake
    ) -> str | None:
        """비공개 스레드에 사용자를 초대하고 실패 사유를 사용자에게 보여줄 문구로 반환합니다."""
        return await self.workspace_service.add_user_to_private_thread(thread, user)

    async def remove_user_from_private_thread(
        self, thread: discord.Thread, user: discord.abc.Snowflake
    ) -> str | None:
        """참가를 취소한 사용자가 비공개 워크스페이스에 계속 남지 않도록 제거합니다."""
        return await self.workspace_service.remove_user_from_private_thread(
            thread, user
        )

    async def sync_private_thread_membership(
        self, recruitment: RecruitmentRecord, user: discord.abc.Snowflake, status: str
    ) -> str | None:
        """신청 승인/거절 상태와 비공개 스레드 멤버십을 맞춥니다."""
        return await self.workspace_service.sync_private_thread_membership(
            recruitment, user, status
        )

    async def generate_recruitment_copy_text(self, source_text: str) -> str:
        """AI Provider를 통해 모집글 JSON 원문을 생성합니다."""
        return await self.copy_service.generate_copy_text(source_text)

    async def generate_recruitment_copy(self, source_text: str) -> tuple[str, str, int]:
        """AI 응답을 Embed용 데이터로 파싱합니다."""
        return await self.copy_service.generate_copy(source_text)

    async def prepare_recruitment_source_text(self, target_info: str) -> str:
        """입력 텍스트에 포함된 여러 URL을 동시에 크롤링하고 일반 텍스트와 병합합니다."""
        return await self.copy_service.prepare_source_text(target_info)

    async def get_recruitment(self, message_id: int) -> RecruitmentRecord | None:
        """메시지 ID로 owner가 포함된 모집 레코드를 찾습니다."""
        return await self.recruitment_service.get_recruitment_by_message_id(message_id)

    async def get_recruitment_by_id(
        self, recruitment_id: int
    ) -> RecruitmentRecord | None:
        """모집 ID로 owner가 포함된 모집 레코드를 찾습니다."""
        return await self.recruitment_service.get_recruitment_by_id(recruitment_id)

    async def get_participant(
        self, recruitment_id: int, user_id: int
    ) -> dict[str, object] | None:
        """특정 모집의 신청자 상태를 가져옵니다."""
        return await self.recruitment_service.get_participant(recruitment_id, user_id)

    async def save_participant_status(
        self,
        recruitment_id: int,
        user_id: int,
        status: ParticipantStatus,
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
        self, recruitment_id: int, status: ParticipantStatus
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

    async def get_confirmed_participants(
        self, recruitment_id: int
    ) -> list[dict[str, object]]:
        """Embed 표시용 현재 참가자(owner + accepted)를 DB에서 직접 조회합니다."""
        return await self.recruitment_service.get_confirmed_participants(recruitment_id)

    async def close_recruitment_if_full(self, recruitment_id: int) -> bool:
        """정원이 있는 모집에서 현재 참가자가 정원에 도달하면 모집을 마감합니다."""
        return await self.recruitment_service.close_recruitment_if_full(recruitment_id)

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
        recruitment: RecruitmentRecord,
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
        return self.embed_factory.build_application_manage_embed(
            recruitment,
            applicant_id=applicant_id,
            application_reason=application_reason,
            applicant_mention=applicant_mention,
            title=title,
            color=color,
            decided_by=decided_by,
            notice=notice,
        )

    async def notify_recruitment_owner(
        self, recruitment: RecruitmentRecord, applicant: discord.abc.User, reason: str
    ) -> str | None:
        """새 신청을 비공개 스레드 또는 작성자 DM으로 알립니다."""
        return await self.workspace_service.notify_recruitment_owner(
            recruitment, applicant, reason
        )

    async def edit_recruitment_message(
        self, channel_id: int, message_id: int
    ) -> str | None:
        """DB에 저장된 채널/메시지 ID로 기존 모집 Embed 메시지를 찾아 수정합니다."""
        return await self.message_service.edit_recruitment_message(
            channel_id, message_id
        )

    async def build_recruitment_embed(self, message_id: int) -> discord.Embed:
        """기존 호출부 호환을 위한 래퍼입니다."""
        return await self.update_recruitment_embed(message_id)

    async def update_recruitment_embed(self, message_id: int) -> discord.Embed:
        """현재 DB 상태를 읽어 모집 Embed를 다시 만듭니다."""
        return await self.message_service.build_recruitment_embed(message_id)

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

        rows = await self.recruitment_service.list_editable_recruitment_rows(
            interaction.guild.id, interaction.user.id
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
