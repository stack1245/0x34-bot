from __future__ import annotations

import logging

import discord
from discord.ext import commands

from cogs.schedule import (
    SECTION_DIVIDER,
    일정,
    handle_command_error,
    respond_ephemeral,
    update_live_dashboard,
)
from database.connection import set_schedule_channel_id, set_schedule_message_id

logger = logging.getLogger(__name__)


@일정.command(name="채널설정", description="실시간 일정 대시보드 채널을 설정합니다.")
@discord.option(
    name="채널",
    input_type=discord.TextChannel,
    description="실시간 일정 타임라인 대시보드를 표시할 채널",
    required=True,
)
@commands.has_permissions(administrator=True)
async def schedule_channel_setup(
    ctx: discord.ApplicationContext,
    채널: discord.TextChannel,
) -> None:
    logger.info(
        "Schedule channel setup invoked. guild_id=%s user_id=%s channel_id=%s",
        ctx.guild_id,
        getattr(getattr(ctx, "author", None), "id", None),
        채널.id,
    )
    if ctx.guild is None or ctx.guild_id is None:
        await respond_ephemeral(
            ctx,
            title="❌ 채널설정 사용 범위 제한",
            description="## 상태\n- ❌ /일정 채널설정 명령은 서버 내부에서만 사용할 수 있습니다.",
        )
        return

    if 채널.guild.id != ctx.guild_id:
        await respond_ephemeral(
            ctx,
            title="❌ 채널 검증 실패",
            description="## 상태\n- ❌ 현재 서버에 속한 텍스트 채널만 설정할 수 있습니다.",
        )
        return

    bot_member = ctx.guild.me
    if bot_member is None:
        await respond_ephemeral(
            ctx,
            title="❌ 봇 정보 확인 실패",
            description="## 상태\n- ❌ 봇 멤버 정보를 확인할 수 없습니다. 잠시 후 다시 시도해주세요.",
        )
        return

    permissions = 채널.permissions_for(bot_member)
    if not permissions.send_messages or not permissions.embed_links:
        await respond_ephemeral(
            ctx,
            title="❌ 채널 권한 부족",
            description="\n".join(
                [
                    "## 상태",
                    "- ❌ 선택한 채널에 대시보드를 전송할 권한이 부족합니다.",
                    "- 봇에게 메시지 전송 및 임베드 링크 권한을 부여해주세요.",
                ]
            ),
        )
        return

    try:
        set_schedule_channel_id(ctx.guild_id, 채널.id)
        set_schedule_message_id(ctx.guild_id, None)
        await update_live_dashboard(ctx.bot, ctx.guild_id)
    except Exception as error:
        logger.exception(
            "Failed to set schedule dashboard channel. guild_id=%s channel_id=%s",
            ctx.guild_id,
            채널.id,
            exc_info=error,
        )
        await respond_ephemeral(
            ctx,
            title="❌ 채널설정 실패",
            description="## 상태\n- ❌ 설정 저장 또는 대시보드 초기 생성 중 오류가 발생했습니다.",
        )
        return

    await respond_ephemeral(
        ctx,
        title="✅ 채널설정 완료",
        description="\n".join(
            [
                "## 실행 결과",
                SECTION_DIVIDER,
                f"- 대시보드 채널: {채널.mention}",
                "- 상태: 실시간 자동 동기화 타임라인 대시보드 활성화",
            ]
        ),
    )


@schedule_channel_setup.error
async def schedule_channel_setup_error(
    ctx: discord.ApplicationContext,
    error: Exception,
) -> None:
    await handle_command_error(ctx, error, command_name="/일정 채널설정")


def setup(bot: discord.Bot) -> None:
    logger.info(
        "Schedule setup subcommand module mounted. bot_class=%s",
        bot.__class__.__name__,
    )
