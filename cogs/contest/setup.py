import logging

import discord
from discord.ext import commands

from cogs.contest import (
    handle_command_error,
    respond_ephemeral,
    대회공고,
)
from database.connection import set_contest_channel_id

logger = logging.getLogger(__name__)


@대회공고.command(name="채널설정", description="대회 공고 채널을 저장합니다.")
@discord.option(
    name="채널",
    input_type=discord.TextChannel,
    description="대회 공고가 게시될 텍스트 채널",
    required=True,
)
@commands.has_permissions(administrator=True)
async def contest_setup(
    ctx: discord.ApplicationContext,
    채널: discord.TextChannel,
) -> None:
    logger.info(
        "Contest setup command invoked. guild_id=%s user_id=%s channel_id=%s",
        ctx.guild_id,
        getattr(getattr(ctx, "author", None), "id", None),
        채널.id,
    )
    if ctx.guild is None or ctx.guild_id is None:
        await respond_ephemeral(
            ctx,
            title="채널설정 사용 범위 제한",
            description="## 상태\n- /대회공고 채널설정 명령은 서버 내부에서만 사용할 수 있습니다.",
        )
        return

    if 채널.guild.id != ctx.guild_id:
        await respond_ephemeral(
            ctx,
            title="채널 검증 실패",
            description="## 상태\n- 현재 서버에 속한 텍스트 채널만 설정할 수 있습니다.",
        )
        return

    set_contest_channel_id(ctx.guild_id, 채널.id)
    logger.info(
        "Contest channel saved. guild_id=%s channel_id=%s", ctx.guild_id, 채널.id
    )
    await respond_ephemeral(
        ctx,
        title="대회 공고 채널 설정 완료",
        description="\n".join(
            [
                "## 실행 결과",
                f"- 서버: {ctx.guild.name}",
                f"- 공고 채널: {채널.mention}",
                "- 상태: guild_settings 업서트 완료",
            ]
        ),
    )


@contest_setup.error
async def contest_setup_error(
    ctx: discord.ApplicationContext,
    error: Exception,
) -> None:
    await handle_command_error(ctx, error, command_name="/대회공고 채널설정")


def setup(bot: discord.Bot) -> None:
    logger.info(
        "Contest setup subcommand module mounted. bot_class=%s",
        bot.__class__.__name__,
    )
