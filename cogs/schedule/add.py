from __future__ import annotations

import logging

import discord

from cogs.schedule import 일정, handle_command_error, respond_ephemeral
from views.schedule_modal import ScheduleModal

logger = logging.getLogger(__name__)


@일정.command(name="등록", description="일정을 등록하는 모달을 엽니다.")
async def schedule_add(ctx: discord.ApplicationContext) -> None:
    logger.info(
        "Schedule add command invoked. guild_id=%s user_id=%s",
        ctx.guild_id,
        getattr(getattr(ctx, "author", None), "id", None),
    )
    if ctx.guild_id is None:
        await respond_ephemeral(
            ctx,
            title="❌ 일정 등록 사용 범위 제한",
            description="## 상태\n- ❌ /일정 등록 명령은 서버 내부에서만 사용할 수 있습니다.",
        )
        return

    await ctx.send_modal(ScheduleModal())
    logger.info("Schedule modal dispatched. guild_id=%s", ctx.guild_id)


@schedule_add.error
async def schedule_add_error(
    ctx: discord.ApplicationContext,
    error: Exception,
) -> None:
    await handle_command_error(ctx, error, command_name="/일정 등록")


def setup(bot: discord.Bot) -> None:
    logger.info(
        "Schedule add subcommand module mounted. bot_class=%s",
        bot.__class__.__name__,
    )
