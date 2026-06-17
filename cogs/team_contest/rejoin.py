from __future__ import annotations

import logging

import discord

from cogs.team_contest import (
    build_team_contest_embed,
    build_team_contest_error_embed,
    handle_command_error,
    대회,
)
from database.connection import is_team_leader_of_thread

logger = logging.getLogger(__name__)


@대회.command(
    name="재진입", description="팀장이 비공개 프로젝트 스레드에 다시 입장합니다."
)
@discord.option(
    name="스레드_id",
    input_type=str,
    description="재진입할 비공개 프로젝트 스레드 ID",
    required=True,
)
async def team_contest_rejoin(
    ctx: discord.ApplicationContext,
    스레드_id: str,
) -> None:
    logger.info(
        "Team contest rejoin command invoked. guild_id=%s user_id=%s thread_id_raw=%s",
        ctx.guild_id,
        getattr(getattr(ctx, "author", None), "id", None),
        스레드_id,
    )
    if ctx.guild is None or ctx.guild_id is None:
        embed = build_team_contest_error_embed(
            title="❌ 재진입 사용 범위 제한",
            description="## 상태\n- ❌ /대회 재진입 명령은 서버 내부에서만 사용할 수 있습니다.",
            guild=ctx.guild,
        )
        await ctx.respond(embed=embed, ephemeral=True)
        return

    try:
        thread_id = int(스레드_id.strip())
    except (ValueError, AttributeError):
        embed = build_team_contest_error_embed(
            title="❌ 스레드 ID 파싱 실패",
            description="## 상태\n- ❌ 스레드_id는 숫자 형식으로 입력해야 합니다.",
            guild=ctx.guild,
        )
        await ctx.respond(embed=embed, ephemeral=True)
        return

    author = ctx.author
    if not isinstance(author, discord.Member):
        embed = build_team_contest_error_embed(
            title="❌ 사용자 검증 실패",
            description="## 상태\n- ❌ 서버 멤버만 재진입 명령을 사용할 수 있습니다.",
            guild=ctx.guild,
        )
        await ctx.respond(embed=embed, ephemeral=True)
        return

    if not is_team_leader_of_thread(ctx.guild_id, thread_id, author.id):
        embed = build_team_contest_error_embed(
            title="❌ 권한 부족",
            description="❌ 권한 부족: 해당 비공개 프로젝트 스레드의 등록된 팀장이 아닙니다.",
            guild=ctx.guild,
        )
        await ctx.respond(embed=embed, ephemeral=True)
        return

    thread = ctx.guild.get_thread(thread_id)
    if not isinstance(thread, discord.Thread):
        embed = build_team_contest_error_embed(
            title="❌ 스레드 조회 실패",
            description="## 상태\n- ❌ 해당 스레드를 찾을 수 없습니다. 스레드 ID를 다시 확인해주세요.",
            guild=ctx.guild,
        )
        await ctx.respond(embed=embed, ephemeral=True)
        return

    try:
        await thread.add_user(author)
    except Exception as error:
        logger.exception(
            "Failed to rejoin thread. guild_id=%s thread_id=%s user_id=%s",
            ctx.guild_id,
            thread_id,
            author.id,
            exc_info=error,
        )
        embed = build_team_contest_error_embed(
            title="❌ 재진입 처리 실패",
            description="## 상태\n- ❌ 스레드 재진입 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요.",
            guild=ctx.guild,
        )
        await ctx.respond(embed=embed, ephemeral=True)
        return

    success_embed = build_team_contest_embed(
        title="✅ 스레드 재진입 성공",
        description="✅ 스레드 재진입 성공: 비공개 프로젝트 룸으로 복귀되었습니다.",
        guild=ctx.guild,
    )
    await ctx.respond(embed=success_embed, ephemeral=True)


@team_contest_rejoin.error
async def team_contest_rejoin_error(
    ctx: discord.ApplicationContext,
    error: Exception,
) -> None:
    await handle_command_error(ctx, error, command_name="/대회 재진입")


def setup(bot: discord.Bot) -> None:
    logger.info(
        "Team contest rejoin subcommand module mounted. bot_class=%s",
        bot.__class__.__name__,
    )
