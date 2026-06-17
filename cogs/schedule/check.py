from __future__ import annotations

import logging

import discord
from discord.ext import commands

from cogs.schedule import (
    SECTION_DIVIDER,
    build_schedule_embed,
    일정,
    handle_command_error,
    respond_ephemeral,
)
from database.connection import get_schedule_channel_id, get_schedule_message_id

logger = logging.getLogger(__name__)


@일정.command(
    name="채널확인", description="실시간 일정 대시보드 설정 상태를 확인합니다."
)
@commands.has_permissions(administrator=True)
async def schedule_channel_check(ctx: discord.ApplicationContext) -> None:
    logger.info(
        "Schedule channel check invoked. guild_id=%s user_id=%s",
        ctx.guild_id,
        getattr(getattr(ctx, "author", None), "id", None),
    )
    if ctx.guild is None or ctx.guild_id is None:
        await respond_ephemeral(
            ctx,
            title="❌ 채널확인 사용 범위 제한",
            description="## 상태\n- ❌ /일정 채널확인 명령은 서버 내부에서만 사용할 수 있습니다.",
        )
        return

    channel_id = get_schedule_channel_id(ctx.guild_id)
    message_id = get_schedule_message_id(ctx.guild_id)

    if channel_id is None:
        await respond_ephemeral(
            ctx,
            title="❌ 대시보드 비활성 상태",
            description="## 상태\n- ❌ 현재 실시간 대시보드 채널이 설정되어 있지 않습니다.",
        )
        return

    channel = ctx.guild.get_channel(channel_id)
    channel_display = (
        channel.mention
        if isinstance(channel, discord.TextChannel)
        else f"알 수 없음 ({channel_id})"
    )
    message_display = str(message_id) if message_id is not None else "미생성"

    embed = build_schedule_embed(
        title="📡 실시간 대시보드 상태 리포트",
        description="\n".join(
            [
                "## 기동 상태",
                SECTION_DIVIDER,
                f"- 연동 채널: {channel_display}",
                f"- 대시보드 메시지 ID: {message_display}",
                "- 상태: 자동 동기화 엔진 준비 완료",
            ]
        ),
        guild=ctx.guild,
    )
    await ctx.respond(embed=embed, ephemeral=True)


@schedule_channel_check.error
async def schedule_channel_check_error(
    ctx: discord.ApplicationContext,
    error: Exception,
) -> None:
    await handle_command_error(ctx, error, command_name="/일정 채널확인")


def setup(bot: discord.Bot) -> None:
    logger.info(
        "Schedule check subcommand module mounted. bot_class=%s",
        bot.__class__.__name__,
    )
