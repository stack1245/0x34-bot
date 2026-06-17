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
    update_live_dashboard,
)
from database.connection import delete_schedule

logger = logging.getLogger(__name__)


@일정.command(name="삭제", description="일정 ID로 등록된 일정을 삭제합니다.")
@discord.option(
    name="일정_id",
    input_type=int,
    description="삭제할 일정 ID",
    required=True,
)
@commands.has_permissions(administrator=True)
async def schedule_delete(
    ctx: discord.ApplicationContext,
    일정_id: int,
) -> None:
    logger.info(
        "Schedule delete command invoked. guild_id=%s user_id=%s schedule_id=%s",
        ctx.guild_id,
        getattr(getattr(ctx, "author", None), "id", None),
        일정_id,
    )
    if ctx.guild_id is None:
        await respond_ephemeral(
            ctx,
            title="❌ 일정 삭제 사용 범위 제한",
            description="## 상태\n- ❌ /일정 삭제 명령은 서버 내부에서만 사용할 수 있습니다.",
        )
        return

    try:
        deleted = delete_schedule(ctx.guild_id, 일정_id)
    except Exception as error:
        logger.exception(
            "Schedule delete failed with transaction error. guild_id=%s schedule_id=%s",
            ctx.guild_id,
            일정_id,
            exc_info=error,
        )
        await respond_ephemeral(
            ctx,
            title="❌ 일정 삭제 실패",
            description="## 상태\n- ❌ DB 트랜잭션 오류로 일정을 삭제하지 못했습니다.",
        )
        return

    if not deleted:
        await respond_ephemeral(
            ctx,
            title="❌ 삭제 대상 없음",
            description=f"## 상태\n- ❌ 일정 ID {일정_id} 항목을 찾지 못했습니다.",
        )
        return

    try:
        await update_live_dashboard(ctx.bot, ctx.guild_id)
    except Exception as error:
        logger.exception(
            "Schedule deleted but live dashboard update failed. guild_id=%s schedule_id=%s",
            ctx.guild_id,
            일정_id,
            exc_info=error,
        )

    embed = build_schedule_embed(
        title="✅ 일정 삭제 완료",
        description="\n".join(
            [
                "## 실행 결과",
                SECTION_DIVIDER,
                f"- 삭제된 일정 ID: {일정_id}",
                "- 상태: guild_schedules 레코드 삭제 완료",
            ]
        ),
        guild=ctx.guild,
    )
    await ctx.respond(embed=embed, ephemeral=True)


@schedule_delete.error
async def schedule_delete_error(
    ctx: discord.ApplicationContext,
    error: Exception,
) -> None:
    await handle_command_error(ctx, error, command_name="/일정 삭제")


def setup(bot: discord.Bot) -> None:
    logger.info(
        "Schedule delete subcommand module mounted. bot_class=%s",
        bot.__class__.__name__,
    )
