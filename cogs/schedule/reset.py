from __future__ import annotations

import logging

import discord
from discord.ext import commands

from cogs.schedule import (
    SECTION_DIVIDER,
    일정,
    handle_command_error,
    respond_ephemeral,
)
from database.connection import (
    get_schedule_channel_id,
    get_schedule_message_id,
    reset_schedule_channel,
)

logger = logging.getLogger(__name__)


@일정.command(
    name="채널초기화", description="실시간 일정 대시보드 채널 설정을 초기화합니다."
)
@commands.has_permissions(administrator=True)
async def schedule_channel_reset(ctx: discord.ApplicationContext) -> None:
    logger.info(
        "Schedule channel reset invoked. guild_id=%s user_id=%s",
        ctx.guild_id,
        getattr(getattr(ctx, "author", None), "id", None),
    )
    if ctx.guild is None or ctx.guild_id is None:
        await respond_ephemeral(
            ctx,
            title="❌ 채널초기화 사용 범위 제한",
            description="## 상태\n- ❌ /일정 채널초기화 명령은 서버 내부에서만 사용할 수 있습니다.",
        )
        return

    channel_id = get_schedule_channel_id(ctx.guild_id)
    message_id = get_schedule_message_id(ctx.guild_id)

    if channel_id is None and message_id is None:
        await respond_ephemeral(
            ctx,
            title="❌ 초기화 대상 없음",
            description="## 상태\n- ❌ 현재 정리할 대시보드 채널/메시지 설정이 없습니다.",
        )
        return

    deleted_dashboard_message = False
    channel = ctx.guild.get_channel(channel_id) if channel_id is not None else None
    if isinstance(channel, discord.TextChannel) and message_id is not None:
        try:
            dashboard_message = await channel.fetch_message(message_id)
            await dashboard_message.delete()
            deleted_dashboard_message = True
            logger.info(
                "Deleted schedule dashboard message during reset. guild_id=%s channel_id=%s message_id=%s",
                ctx.guild_id,
                channel.id,
                message_id,
            )
        except discord.NotFound:
            logger.warning(
                "Dashboard message not found during reset. guild_id=%s channel_id=%s message_id=%s",
                ctx.guild_id,
                channel.id,
                message_id,
            )
        except discord.Forbidden:
            logger.exception(
                "Forbidden while deleting dashboard message during reset. guild_id=%s channel_id=%s message_id=%s",
                ctx.guild_id,
                channel.id,
                message_id,
            )
        except discord.HTTPException as error:
            logger.exception(
                "HTTP exception while deleting dashboard message during reset. guild_id=%s channel_id=%s message_id=%s",
                ctx.guild_id,
                channel.id,
                message_id,
                exc_info=error,
            )

    try:
        reset_schedule_channel(ctx.guild_id)
    except Exception as error:
        logger.exception(
            "Failed to reset schedule dashboard mapping. guild_id=%s",
            ctx.guild_id,
            exc_info=error,
        )
        await respond_ephemeral(
            ctx,
            title="❌ 채널초기화 실패",
            description="## 상태\n- ❌ DB 설정 정리 중 오류가 발생했습니다.",
        )
        return

    await respond_ephemeral(
        ctx,
        title="✅ 채널초기화 완료",
        description="\n".join(
            [
                "## 실행 결과",
                SECTION_DIVIDER,
                "- 대시보드 채널 및 메시지 매핑이 정리되었습니다.",
                f"- 기존 대시보드 메시지 삭제: {'완료' if deleted_dashboard_message else '미대상 또는 미존재'}",
            ]
        ),
    )


@schedule_channel_reset.error
async def schedule_channel_reset_error(
    ctx: discord.ApplicationContext,
    error: Exception,
) -> None:
    await handle_command_error(ctx, error, command_name="/일정 채널초기화")


def setup(bot: discord.Bot) -> None:
    logger.info(
        "Schedule reset subcommand module mounted. bot_class=%s",
        bot.__class__.__name__,
    )
