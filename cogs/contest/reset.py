from __future__ import annotations

import logging

import discord

from cogs.contest import (
    handle_command_error,
    is_administrator,
    respond_admin_permission_denied,
    respond_ephemeral,
    대회공고,
)
from database.connection import clear_contest_channel_id, get_contest_channel_id

logger = logging.getLogger(__name__)


@대회공고.command(name="채널초기화", description="저장된 대회 공고 채널을 삭제합니다.")
async def contest_reset(ctx: discord.ApplicationContext) -> None:
    logger.info(
        "Contest reset command invoked. guild_id=%s user_id=%s",
        ctx.guild_id,
        getattr(getattr(ctx, "author", None), "id", None),
    )
    if ctx.guild is None or ctx.guild_id is None:
        await respond_ephemeral(
            ctx,
            title="채널초기화 사용 범위 제한",
            description="## 상태\n- /대회공고 채널초기화 명령은 서버 내부에서만 사용할 수 있습니다.",
        )
        return

    if not is_administrator(ctx):
        await respond_admin_permission_denied(ctx, command_name="/대회공고 채널초기화")
        return

    existing_channel_id = get_contest_channel_id(ctx.guild_id)
    if existing_channel_id is None:
        await respond_ephemeral(
            ctx,
            title="초기화할 설정 없음",
            description="## 상태\n- 현재 서버에는 삭제할 대회 공고 채널 설정이 없습니다.",
        )
        return

    clear_contest_channel_id(ctx.guild_id)
    logger.info("Contest channel cleared. guild_id=%s", ctx.guild_id)
    await respond_ephemeral(
        ctx,
        title="대회 공고 채널 초기화 완료",
        description="\n".join(
            [
                "## 실행 결과",
                f"- 서버: {ctx.guild.name}",
                f"- 제거된 채널 ID: {existing_channel_id}",
                "- 상태: guild_settings 레코드 삭제 완료",
            ]
        ),
    )


@contest_reset.error
async def contest_reset_error(
    ctx: discord.ApplicationContext,
    error: Exception,
) -> None:
    await handle_command_error(ctx, error, command_name="/대회공고 채널초기화")


def setup(bot: discord.Bot) -> None:
    logger.info(
        "Contest reset subcommand module mounted. bot_class=%s",
        bot.__class__.__name__,
    )
