from __future__ import annotations

import logging

import discord

from core.config import build_theme_embed
from database.connection import is_team_leader_of_thread

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


class TeamContestRejoinView(discord.ui.View):
    def __init__(self, *, thread_id: int) -> None:
        super().__init__(timeout=None)
        self.thread_id = thread_id

    @discord.ui.button(
        label="🔓 스레드 참가",
        style=discord.ButtonStyle.success,
        custom_id="team_contest_rejoin_button",
    )
    async def rejoin_button(
        self,
        button: discord.ui.Button,
        interaction: discord.Interaction,
    ) -> None:
        del button

        guild = interaction.guild
        guild_id = interaction.guild_id
        if guild is None or guild_id is None:
            embed = _build_error_embed(
                title="❌ 스레드 참가 실패",
                description="## 상태\n- ❌ 이 버튼은 서버 내부에서만 사용할 수 있습니다.",
                guild=guild,
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        if not is_team_leader_of_thread(guild_id, self.thread_id, interaction.user.id):
            embed = _build_error_embed(
                title="❌ 권한 부족",
                description=(
                    "❌ 권한 부족: 해당 비공개 프로젝트 스레드의 등록된 팀장이 아닙니다. "
                    "팀장만 진입 버튼을 이용할 수 있습니다."
                ),
                guild=guild,
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        thread = guild.get_thread(self.thread_id)
        if not isinstance(thread, discord.Thread):
            embed = _build_error_embed(
                title="❌ 스레드 조회 실패",
                description="## 상태\n- ❌ 해당 비공개 프로젝트 스레드를 찾을 수 없습니다.",
                guild=guild,
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        try:
            await thread.add_user(interaction.user)
        except Exception as error:
            logger.exception(
                "Failed to rejoin team contest thread via button. guild_id=%s thread_id=%s user_id=%s",
                guild_id,
                self.thread_id,
                interaction.user.id,
                exc_info=error,
            )
            embed = _build_error_embed(
                title="❌ 스레드 복귀 실패",
                description="## 상태\n- ❌ 스레드 복귀 처리 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요.",
                guild=guild,
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        embed = _build_success_embed(
            title="✅ 스레드 복귀 성공",
            description=(
                "✅ 스레드 복귀 성공: 비공개 프로젝트 룸으로 안전하게 재진입되었습니다.\n"
                f"🔗 [스레드 바로 가기]({thread.jump_url})"
            ),
            guild=guild,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
