from __future__ import annotations

import logging

import discord

from core.config import build_theme_embed
from database.connection import add_team_leader, get_team_contest_channel_id
from views.team_contest_button import TeamContestRejoinView

logger = logging.getLogger(__name__)
ERROR_EMBED_COLOR = 0xFF1744
SUCCESS_EMBED_COLOR = 0x00E5FF


def _apply_guild_footer(
    embed: discord.Embed,
    guild: discord.Guild | None,
) -> discord.Embed:
    icon_url = guild.icon.url if guild and guild.icon else None
    footer_text = embed.footer.text or "Team 0x34"
    embed.set_footer(text=footer_text, icon_url=icon_url)
    return embed


def _build_success_embed(
    *,
    title: str,
    description: str,
    guild: discord.Guild | None,
) -> discord.Embed:
    embed = build_theme_embed(title=title, description=description)
    embed.color = discord.Colour(SUCCESS_EMBED_COLOR)
    return _apply_guild_footer(embed, guild)


def _build_error_embed(
    *,
    title: str,
    description: str,
    guild: discord.Guild | None,
) -> discord.Embed:
    embed = build_theme_embed(title=title, description=description)
    embed.color = discord.Colour(ERROR_EMBED_COLOR)
    return _apply_guild_footer(embed, guild)


def _as_text_block(value: str) -> str:
    return f"```text\n{value}\n```"


class TeamContestModal(discord.ui.Modal):
    def __init__(self, *, contest_scope: str, is_team_leader: str) -> None:
        super().__init__(title="Team 0x34 대회 참가 등록")
        self.contest_scope = contest_scope
        self.is_team_leader = is_team_leader

        self.contest_name = discord.ui.InputText(
            label="대회명",
            placeholder="예시: 제8회 한국코드페어",
            min_length=1,
            max_length=100,
            required=True,
        )
        self.team_name = discord.ui.InputText(
            label="팀명",
            placeholder="예시: 0x34_Main",
            min_length=1,
            max_length=100,
            required=True,
        )
        self.add_item(self.contest_name)
        self.add_item(self.team_name)

    async def callback(self, interaction: discord.Interaction) -> None:
        logger.info(
            "Team contest modal submitted. guild_id=%s user_id=%s",
            interaction.guild_id,
            getattr(interaction.user, "id", None),
        )

        guild = interaction.guild
        guild_id = interaction.guild_id
        if guild is None or guild_id is None:
            await self._respond_error(
                interaction,
                title="❌ 대회 등록 실패",
                description="## 상태\n- ❌ 이 모달은 서버 내부에서만 사용할 수 있습니다.",
            )
            return

        if self.is_team_leader == "아니요":
            await self._respond_error(
                interaction,
                title="❌ 대회 등록 권한 제한",
                description=(
                    "❌ 대회 등록 권한 제한: 해당 명령어 및 대회 등록은 팀장 권한을 가진 부원만 진행할 수 있습니다. "
                    "팀장 본인이 직접 등록하게 하십시오."
                ),
            )
            return

        contest_name = self._normalize_required(self.contest_name.value)
        team_name = self._normalize_required(self.team_name.value)

        channel_id = get_team_contest_channel_id(guild_id)
        if channel_id is None:
            await self._respond_error(
                interaction,
                title="❌ 팀 대회 채널 미설정",
                description="## 상태\n- ❌ 먼저 /대회 채널설정 명령으로 대회 알림 채널을 등록해주세요.",
            )
            return

        channel = guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            logger.warning(
                "Stored team contest channel missing or invalid. guild_id=%s channel_id=%s",
                guild_id,
                channel_id,
            )
            await self._respond_error(
                interaction,
                title="❌ 팀 대회 채널 확인 필요",
                description="## 상태\n- ❌ 저장된 채널을 찾을 수 없습니다. /대회 채널설정 으로 다시 등록해주세요.",
            )
            return

        bot_member = guild.me
        if bot_member is None:
            await self._respond_error(
                interaction,
                title="❌ 봇 정보 확인 실패",
                description="## 상태\n- ❌ 봇 멤버 정보를 확인할 수 없습니다. 잠시 후 다시 시도해주세요.",
            )
            return

        permissions = channel.permissions_for(bot_member)
        if (
            not permissions.send_messages
            or not permissions.embed_links
            or not permissions.create_private_threads
            or not permissions.send_messages_in_threads
        ):
            await self._respond_error(
                interaction,
                title="❌ 채널 권한 부족",
                description="## 상태\n- ❌ 선택된 채널에서 임베드 전송 또는 비공개 스레드 생성을 수행할 권한이 없습니다.",
            )
            return

        contest_embed = _build_success_embed(
            title="🏆  Team 0x34 NEW CONTEST REGISTERED",
            description="## 참가 등록이 접수되었습니다.",
            guild=guild,
        )
        contest_embed.add_field(
            name="📌  대회 구분",
            value=_as_text_block(self.contest_scope),
            inline=False,
        )
        contest_embed.add_field(
            name="📝  대회명 및 팀명",
            value=_as_text_block(f"{contest_name} | {team_name}"),
            inline=False,
        )
        contest_embed.add_field(
            name="👑  팀장 정보",
            value=f"<@{interaction.user.id}>",
            inline=False,
        )

        try:
            thread = await channel.create_thread(
                name=self._build_thread_name(
                    team_name=team_name, contest_name=contest_name
                ),
                type=discord.ChannelType.private_thread,
                invitable=False,
            )
            add_team_leader(interaction.guild_id, thread.id, interaction.user.id)
            rejoin_view = TeamContestRejoinView(thread_id=thread.id)
            announcement_message = await channel.send(
                embed=contest_embed, view=rejoin_view
            )
            await thread.send(f"원본 공고: {announcement_message.jump_url}")
            if self.is_team_leader == "예":
                await thread.add_user(interaction.user)
        except Exception as error:
            logger.exception(
                "Failed to publish team contest registration. guild_id=%s channel_id=%s user_id=%s",
                guild_id,
                channel_id,
                interaction.user.id,
                exc_info=error,
            )
            await self._respond_error(
                interaction,
                title="❌ 대회 등록 처리 실패",
                description="## 상태\n- ❌ 임베드 전송 또는 비공개 스레드 생성 중 오류가 발생했습니다. 로그를 확인해주세요.",
            )
            return

        await self._respond_success(
            interaction,
            title="📨 대회 등록 완료",
            description="\n".join(
                [
                    "## 실행 결과",
                    f"- 공고 채널: {channel.mention}",
                    f"- 비공개 스레드: {thread.mention}",
                    f"- 팀장 자동 진입: {'완료' if self.is_team_leader == '예' else '미실행'}",
                ]
            ),
        )

    @staticmethod
    def _normalize_required(value: str) -> str:
        return " ".join(value.split()).strip()

    @staticmethod
    def _build_thread_name(*, team_name: str, contest_name: str) -> str:
        thread_name = f"🔒-{team_name}-{contest_name}"
        return thread_name[:100]

    async def _respond_success(
        self,
        interaction: discord.Interaction,
        *,
        title: str,
        description: str,
    ) -> None:
        embed = _build_success_embed(
            title=title,
            description=description,
            guild=interaction.guild,
        )
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def _respond_error(
        self,
        interaction: discord.Interaction,
        *,
        title: str,
        description: str,
    ) -> None:
        embed = _build_error_embed(
            title=title,
            description=description,
            guild=interaction.guild,
        )
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        await interaction.response.send_message(embed=embed, ephemeral=True)
