from __future__ import annotations

import logging

import discord
from discord.ext import commands

from cogs.team_contest import handle_command_error, respond_ephemeral, 대회
from database.connection import (
    get_team_contest_channel_id,
    reset_team_contest_channel,
)

logger = logging.getLogger(__name__)


@대회.command(name="채널초기화", description="저장된 팀 대회 채널 설정을 삭제합니다.")
@commands.has_permissions(administrator=True)
async def team_contest_reset(ctx: discord.ApplicationContext) -> None:
    logger.info(
        "Team contest reset command invoked. guild_id=%s user_id=%s",
        ctx.guild_id,
        getattr(getattr(ctx, "author", None), "id", None),
    )
    if ctx.guild is None or ctx.guild_id is None:
        await respond_ephemeral(
            ctx,
            title="❌ 채널초기화 사용 범위 제한",
            description="## 상태\n- ❌ /대회 채널초기화 명령은 서버 내부에서만 사용할 수 있습니다.",
        )
        return

    existing_channel_id = get_team_contest_channel_id(ctx.guild_id)
    if existing_channel_id is None:
        await respond_ephemeral(
            ctx,
            title="❌ 초기화할 설정 없음",
            description="## 상태\n- ❌ 현재 서버에는 삭제할 팀 대회 채널 설정이 없습니다.",
        )
        return

    reset_team_contest_channel(ctx.guild_id)
    await respond_ephemeral(
        ctx,
        title="📡 팀 대회 채널 초기화 완료",
        description="\n".join(
            [
                "## 실행 결과",
                f"- 서버: {ctx.guild.name}",
                f"- 제거된 채널 ID: {existing_channel_id}",
                "- 상태: 팀 대회 공고 및 비공개 스레드 라우팅 비활성화",
            ]
        ),
    )


@team_contest_reset.error
async def team_contest_reset_error(
    ctx: discord.ApplicationContext,
    error: Exception,
) -> None:
    await handle_command_error(ctx, error, command_name="/대회 채널초기화")


def setup(bot: discord.Bot) -> None:
    logger.info(
        "Team contest reset subcommand module mounted. bot_class=%s",
        bot.__class__.__name__,
    )
