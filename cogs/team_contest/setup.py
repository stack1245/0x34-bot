from __future__ import annotations

import logging

import discord
from discord.ext import commands

from cogs.team_contest import handle_command_error, respond_ephemeral, 대회
from database.connection import set_team_contest_channel_id

logger = logging.getLogger(__name__)


@대회.command(name="채널설정", description="팀 대회 알림 채널을 저장합니다.")
@discord.option(
    name="채널",
    input_type=discord.TextChannel,
    description="대회 임베드와 비공개 프로젝트 스레드가 생성될 텍스트 채널",
    required=True,
)
@commands.has_permissions(administrator=True)
async def team_contest_setup(
    ctx: discord.ApplicationContext,
    채널: discord.TextChannel,
) -> None:
    logger.info(
        "Team contest setup command invoked. guild_id=%s user_id=%s channel_id=%s",
        ctx.guild_id,
        getattr(getattr(ctx, "author", None), "id", None),
        채널.id,
    )
    if ctx.guild is None or ctx.guild_id is None:
        await respond_ephemeral(
            ctx,
            title="❌ 채널설정 사용 범위 제한",
            description="## 상태\n- ❌ /대회 채널설정 명령은 서버 내부에서만 사용할 수 있습니다.",
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
    if (
        not permissions.send_messages
        or not permissions.embed_links
        or not permissions.create_private_threads
        or not permissions.send_messages_in_threads
    ):
        await respond_ephemeral(
            ctx,
            title="❌ 채널 권한 부족",
            description="\n".join(
                [
                    "## 상태",
                    "- ❌ 선택한 채널에 대회 알림 또는 비공개 스레드를 생성할 권한이 부족합니다.",
                    "- 메시지 전송, 임베드 링크, 비공개 스레드 생성, 스레드 메시지 전송 권한을 확인해주세요.",
                ]
            ),
        )
        return

    set_team_contest_channel_id(ctx.guild_id, 채널.id)
    await respond_ephemeral(
        ctx,
        title="📡 팀 대회 채널 설정 완료",
        description="\n".join(
            [
                "## 실행 결과",
                f"- 서버: {ctx.guild.name}",
                f"- 연동 채널: {채널.mention}",
                "- 상태: 팀 대회 공고 및 비공개 스레드 라우팅 활성화",
            ]
        ),
    )


@team_contest_setup.error
async def team_contest_setup_error(
    ctx: discord.ApplicationContext,
    error: Exception,
) -> None:
    await handle_command_error(ctx, error, command_name="/대회 채널설정")


def setup(bot: discord.Bot) -> None:
    logger.info(
        "Team contest setup subcommand module mounted. bot_class=%s",
        bot.__class__.__name__,
    )
