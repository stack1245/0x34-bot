from __future__ import annotations

import logging

import discord

from cogs.team_contest import handle_command_error, respond_ephemeral, 대회
from views.team_contest_modal import TeamContestModal

logger = logging.getLogger(__name__)


@대회.command(name="등록", description="새로운 팀 대회 참가 정보를 등록합니다.")
@discord.option(
    name="구분",
    input_type=str,
    choices=["교내", "교외"],
    description="대회 구분을 선택하세요.",
    required=True,
)
@discord.option(
    name="팀장여부",
    input_type=str,
    choices=["예", "아니요"],
    description="본인이 팀장인지 선택하세요.",
    required=True,
)
async def team_contest_register(
    ctx: discord.ApplicationContext,
    구분: str,
    팀장여부: str,
) -> None:
    logger.info(
        "Team contest register command invoked. guild_id=%s user_id=%s contest_scope=%s is_team_leader=%s",
        ctx.guild_id,
        getattr(getattr(ctx, "author", None), "id", None),
        구분,
        팀장여부,
    )
    if ctx.guild_id is None:
        await respond_ephemeral(
            ctx,
            title="❌ 대회 등록 사용 범위 제한",
            description="## 상태\n- ❌ /대회 등록 명령은 서버 내부에서만 사용할 수 있습니다.",
        )
        return

    await ctx.send_modal(TeamContestModal(contest_scope=구분, is_team_leader=팀장여부))


@team_contest_register.error
async def team_contest_register_error(
    ctx: discord.ApplicationContext,
    error: Exception,
) -> None:
    await handle_command_error(ctx, error, command_name="/대회 등록")


def setup(bot: discord.Bot) -> None:
    logger.info(
        "Team contest register subcommand module mounted. bot_class=%s",
        bot.__class__.__name__,
    )
