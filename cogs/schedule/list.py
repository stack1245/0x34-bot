from __future__ import annotations

import logging

import discord

from cogs.schedule import (
    SECTION_DIVIDER,
    build_schedule_embed,
    일정,
    handle_command_error,
    respond_ephemeral,
)
from database.connection import get_schedules

logger = logging.getLogger(__name__)


@일정.command(name="목록", description="다가오는 등록 일정을 조회합니다.")
async def schedule_list(ctx: discord.ApplicationContext) -> None:
    logger.info(
        "Schedule list command invoked. guild_id=%s user_id=%s",
        ctx.guild_id,
        getattr(getattr(ctx, "author", None), "id", None),
    )
    if ctx.guild_id is None:
        await respond_ephemeral(
            ctx,
            title="❌ 일정 목록 사용 범위 제한",
            description="## 상태\n- ❌ /일정 목록 명령은 서버 내부에서만 사용할 수 있습니다.",
        )
        return

    try:
        schedules = get_schedules(ctx.guild_id)
    except Exception as error:
        logger.exception(
            "Schedule query failed. guild_id=%s",
            ctx.guild_id,
            exc_info=error,
        )
        await respond_ephemeral(
            ctx,
            title="❌ 일정 조회 실패",
            description="## 상태\n- ❌ DB 트랜잭션 오류로 일정 목록을 조회하지 못했습니다.",
        )
        return

    if not schedules:
        embed = build_schedule_embed(
            title="📅 Team 0x34 일정 목록",
            description="\n".join(
                [
                    "## 상태",
                    SECTION_DIVIDER,
                    "- 등록된 일정이 없습니다.",
                ]
            ),
            guild=ctx.guild,
        )
        await ctx.respond(embed=embed, ephemeral=True)
        return

    lines: list[str] = []
    for schedule_id, _, title, start_ts, end_ts in schedules:
        lines.append(f"🆔 **ID: {schedule_id}**")
        lines.append(f"└ 📝  ```text\\n{title}\\n```")
        if start_ts is not None:
            lines.append(f"└ ⏳ 기간: <t:{start_ts}:F> ~ <t:{end_ts}:F>")
            lines.append(f"└ 📢 종료까지: <t:{end_ts}:R>")
        else:
            lines.append(f"└ 🏁 마감: <t:{end_ts}:F> (<t:{end_ts}:R>)")
        lines.append("")

    embed = build_schedule_embed(
        title="📅 Team 0x34 일정 목록",
        description="\n".join(
            [
                "## 교내·외 대회 및 프로젝트 일정",
                SECTION_DIVIDER,
                *lines,
            ]
        ).strip(),
        guild=ctx.guild,
    )
    await ctx.respond(embed=embed, ephemeral=True)


@schedule_list.error
async def schedule_list_error(
    ctx: discord.ApplicationContext,
    error: Exception,
) -> None:
    await handle_command_error(ctx, error, command_name="/일정 목록")


def setup(bot: discord.Bot) -> None:
    logger.info(
        "Schedule list subcommand module mounted. bot_class=%s",
        bot.__class__.__name__,
    )
