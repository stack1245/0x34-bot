from __future__ import annotations

import logging

import discord

from cogs.team_contest import (
    build_team_contest_embed,
    handle_command_error,
    respond_ephemeral,
    대회,
)
from database.connection import get_team_contest_channel_id

logger = logging.getLogger(__name__)


@대회.command(name="채널확인", description="현재 팀 대회 채널 설정 상태를 확인합니다.")
async def team_contest_check(ctx: discord.ApplicationContext) -> None:
    logger.info(
        "Team contest check command invoked. guild_id=%s user_id=%s",
        ctx.guild_id,
        getattr(getattr(ctx, "author", None), "id", None),
    )
    if ctx.guild is None or ctx.guild_id is None:
        await respond_ephemeral(
            ctx,
            title="❌ 채널확인 사용 범위 제한",
            description="## 상태\n- ❌ /대회 채널확인 명령은 서버 내부에서만 사용할 수 있습니다.",
        )
        return

    channel_id = get_team_contest_channel_id(ctx.guild_id)
    if channel_id is None:
        await respond_ephemeral(
            ctx,
            title="❌ 팀 대회 채널 미설정",
            description="## 상태\n- ❌ 등록된 채널이 없습니다. /대회 채널설정 으로 먼저 저장해주세요.",
        )
        return

    channel = ctx.guild.get_channel(channel_id)
    channel_display = (
        channel.mention
        if isinstance(channel, discord.TextChannel)
        else f"알 수 없음 ({channel_id})"
    )

    embed = build_team_contest_embed(
        title="📡 팀 대회 채널 상태 리포트",
        description="\n".join(
            [
                "## 현재 설정",
                f"- 서버: {ctx.guild.name}",
                f"- 연동 채널: {channel_display}",
                f"- 채널 ID: {channel_id}",
                "- 상태: 대회 등록 임베드 및 비공개 스레드 생성 준비 완료",
            ]
        ),
        guild=ctx.guild,
    )
    await ctx.respond(embed=embed, ephemeral=True)


@team_contest_check.error
async def team_contest_check_error(
    ctx: discord.ApplicationContext,
    error: Exception,
) -> None:
    await handle_command_error(ctx, error, command_name="/대회 채널확인")


def setup(bot: discord.Bot) -> None:
    logger.info(
        "Team contest check subcommand module mounted. bot_class=%s",
        bot.__class__.__name__,
    )
