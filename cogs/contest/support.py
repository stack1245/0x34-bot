from __future__ import annotations

import logging

import discord
from discord.ext import commands

from core.config import build_theme_embed

logger = logging.getLogger(__name__)


def build_contest_embed(*, title: str, description: str) -> discord.Embed:
    return build_theme_embed(title=title, description=description)


async def respond_ephemeral(
    ctx: discord.ApplicationContext,
    *,
    title: str,
    description: str,
) -> None:
    embed = build_contest_embed(title=title, description=description)
    try:
        await ctx.respond(embed=embed, ephemeral=True)
    except discord.InteractionResponded:
        await ctx.followup.send(embed=embed, ephemeral=True)


async def respond_admin_permission_denied(
    ctx: discord.ApplicationContext,
    *,
    command_name: str,
) -> None:
    await respond_ephemeral(
        ctx,
        title="❌ 관리자 권한 필요",
        description="\n".join(
            [
                "## 상태",
                f"- ❌ {command_name} 명령은 관리자만 실행할 수 있습니다.",
            ]
        ),
    )


def is_administrator(ctx: discord.ApplicationContext) -> bool:
    member = getattr(ctx, "author", None)
    if not isinstance(member, discord.Member):
        return False

    return member.guild_permissions.administrator


async def handle_command_error(
    ctx: discord.ApplicationContext,
    error: Exception,
    *,
    command_name: str,
) -> None:
    if isinstance(error, commands.MissingPermissions):
        logger.warning(
            "Missing permissions for contest command. command=%s",
            command_name,
        )
        await respond_admin_permission_denied(ctx, command_name=command_name)
        return

    logger.exception(
        "Contest command failed. command=%s",
        command_name,
        exc_info=error,
    )
    await respond_ephemeral(
        ctx,
        title=f"{command_name} 처리 실패",
        description="## 상태\n- 명령 처리 중 예기치 않은 오류가 발생했습니다. 로그를 확인해주세요.",
    )


__all__ = [
    "build_contest_embed",
    "respond_ephemeral",
    "respond_admin_permission_denied",
    "is_administrator",
    "handle_command_error",
]
