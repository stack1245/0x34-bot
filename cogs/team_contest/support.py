from __future__ import annotations

import logging

import discord
from discord.ext import commands

from core.config import build_theme_embed

logger = logging.getLogger(__name__)
ERROR_EMBED_COLOR = 0xFF1744


def _apply_guild_footer(
    embed: discord.Embed,
    guild: discord.Guild | None,
) -> discord.Embed:
    icon_url = guild.icon.url if guild and guild.icon else None
    footer_text = embed.footer.text or "Team 0x34 | IT Operations"
    embed.set_footer(text=footer_text, icon_url=icon_url)
    return embed


def build_team_contest_embed(
    *,
    title: str,
    description: str,
    guild: discord.Guild | None = None,
) -> discord.Embed:
    embed = build_theme_embed(title=title, description=description)
    return _apply_guild_footer(embed, guild)


def build_team_contest_error_embed(
    *,
    title: str,
    description: str,
    guild: discord.Guild | None = None,
) -> discord.Embed:
    embed = build_theme_embed(title=title, description=description)
    embed.color = discord.Colour(ERROR_EMBED_COLOR)
    return _apply_guild_footer(embed, guild)


async def respond_ephemeral(
    ctx: discord.ApplicationContext,
    *,
    title: str,
    description: str,
) -> None:
    embed = (
        build_team_contest_error_embed(
            title=title,
            description=description,
            guild=ctx.guild,
        )
        if title.startswith("❌")
        else build_team_contest_embed(
            title=title,
            description=description,
            guild=ctx.guild,
        )
    )
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


async def handle_command_error(
    ctx: discord.ApplicationContext,
    error: Exception,
    *,
    command_name: str,
) -> None:
    if isinstance(error, commands.MissingPermissions):
        logger.warning(
            "Missing permissions for team contest command. command=%s",
            command_name,
        )
        await respond_admin_permission_denied(ctx, command_name=command_name)
        return

    logger.exception(
        "Team contest command failed. command=%s",
        command_name,
        exc_info=error,
    )
    await respond_ephemeral(
        ctx,
        title="❌ 명령 처리 실패",
        description="## 상태\n- ❌ 명령 처리 중 예기치 않은 오류가 발생했습니다. 로그를 확인해주세요.",
    )


__all__ = [
    "ERROR_EMBED_COLOR",
    "build_team_contest_embed",
    "build_team_contest_error_embed",
    "respond_ephemeral",
    "respond_admin_permission_denied",
    "handle_command_error",
]
