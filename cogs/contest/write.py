from __future__ import annotations

import logging

import discord

from cogs.contest import handle_command_error, respond_ephemeral, 대회공고
from views.contest_modal import ContestModal

logger = logging.getLogger(__name__)


@대회공고.command(name="작성", description="새로운 대회 공고 모달을 엽니다.")
async def contest_write(ctx: discord.ApplicationContext) -> None:
    logger.info(
        "Contest write command invoked. guild_id=%s user_id=%s",
        ctx.guild_id,
        getattr(getattr(ctx, "author", None), "id", None),
    )
    if ctx.guild_id is None:
        await respond_ephemeral(
            ctx,
            title="대회 작성 사용 범위 제한",
            description="## 상태\n- /대회공고 작성 명령은 서버 내부에서만 사용할 수 있습니다.",
        )
        return

    await ctx.send_modal(ContestModal())
    logger.info("Contest modal dispatched. guild_id=%s", ctx.guild_id)


@contest_write.error
async def contest_write_error(
    ctx: discord.ApplicationContext,
    error: Exception,
) -> None:
    await handle_command_error(ctx, error, command_name="/대회공고 작성")


def setup(bot: discord.Bot) -> None:
    logger.info(
        "Contest write subcommand module mounted. bot_class=%s",
        bot.__class__.__name__,
    )
