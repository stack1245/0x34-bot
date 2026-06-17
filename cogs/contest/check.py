from __future__ import annotations

import logging

import discord

from cogs.contest import handle_command_error, respond_ephemeral, 대회공고
from database.connection import get_contest_channel_id

logger = logging.getLogger(__name__)


@대회공고.command(
    name="채널확인", description="현재 저장된 대회 공고 채널을 확인합니다."
)
async def contest_check(ctx: discord.ApplicationContext) -> None:
    logger.info(
        "Contest check command invoked. guild_id=%s user_id=%s",
        ctx.guild_id,
        getattr(getattr(ctx, "author", None), "id", None),
    )
    if ctx.guild is None or ctx.guild_id is None:
        await respond_ephemeral(
            ctx,
            title="채널확인 사용 범위 제한",
            description="## 상태\n- /대회공고 채널확인 명령은 서버 내부에서만 사용할 수 있습니다.",
        )
        return

    channel_id = get_contest_channel_id(ctx.guild_id)
    if channel_id is None:
        await respond_ephemeral(
            ctx,
            title="대회 공고 채널 미설정",
            description="## 상태\n- 등록된 채널이 없습니다. /대회공고 채널설정 으로 먼저 저장해주세요.",
        )
        return

    channel = ctx.guild.get_channel(channel_id)
    if not isinstance(channel, discord.TextChannel):
        logger.warning(
            "Stored contest channel missing. guild_id=%s channel_id=%s",
            ctx.guild_id,
            channel_id,
        )
        await respond_ephemeral(
            ctx,
            title="대회 공고 채널 확인 필요",
            description="## 상태\n- 저장된 채널을 찾을 수 없습니다. /대회공고 채널설정 으로 다시 등록해주세요.",
        )
        return

    await respond_ephemeral(
        ctx,
        title="대회 공고 채널 조회 결과",
        description="\n".join(
            [
                "## 현재 설정",
                f"- 서버: {ctx.guild.name}",
                f"- 공고 채널: {channel.mention}",
                f"- 채널 ID: {channel.id}",
            ]
        ),
    )


@contest_check.error
async def contest_check_error(
    ctx: discord.ApplicationContext,
    error: Exception,
) -> None:
    await handle_command_error(ctx, error, command_name="/대회공고 채널확인")


def setup(bot: discord.Bot) -> None:
    logger.info(
        "Contest check subcommand module mounted. bot_class=%s",
        bot.__class__.__name__,
    )
