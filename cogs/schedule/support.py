from __future__ import annotations

import logging

import discord
from discord.ext import commands

from core.config import MAX_EMBED_DESCRIPTION_LENGTH, build_theme_embed
from database.connection import (
    get_schedule_channel_id,
    get_schedule_message_id,
    get_schedules,
    set_schedule_message_id,
)

logger = logging.getLogger(__name__)
ERROR_EMBED_COLOR = 0xFF1744
SECTION_DIVIDER = "━━━━━━━━━━━━━━━━━━━━━━━━"


def _apply_guild_footer(
    embed: discord.Embed,
    guild: discord.Guild | None,
) -> discord.Embed:
    icon_url = guild.icon.url if guild and guild.icon else None
    footer_text = embed.footer.text or "Team 0x34"
    embed.set_footer(text=footer_text, icon_url=icon_url)
    return embed


def build_schedule_embed(
    *,
    title: str,
    description: str,
    guild: discord.Guild | None = None,
) -> discord.Embed:
    embed = build_theme_embed(title=title, description=description)
    return _apply_guild_footer(embed, guild)


def build_schedule_error_embed(
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
        build_schedule_error_embed(
            title=title,
            description=description,
            guild=ctx.guild,
        )
        if title.startswith("❌")
        else build_schedule_embed(
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
            "Missing permissions for schedule command. command=%s",
            command_name,
        )
        await respond_admin_permission_denied(ctx, command_name=command_name)
        return

    logger.exception(
        "Schedule command failed. command=%s",
        command_name,
        exc_info=error,
    )
    await respond_ephemeral(
        ctx,
        title="❌ 명령 처리 실패",
        description="\n".join(
            [
                "## 상태",
                f"- ❌ {command_name} 실행 중 예기치 않은 오류가 발생했습니다.",
                "- 로그를 확인해주세요.",
            ]
        ),
    )


async def update_live_dashboard(bot: discord.Bot, guild_id: int) -> None:
    logger.info("Updating live schedule dashboard. guild_id=%s", guild_id)

    schedule_channel_id = get_schedule_channel_id(guild_id)
    if schedule_channel_id is None:
        logger.info(
            "Live dashboard update skipped because schedule channel is not configured. guild_id=%s",
            guild_id,
        )
        return

    guild = bot.get_guild(guild_id)
    if guild is None:
        logger.warning(
            "Live dashboard update skipped because guild is unavailable. guild_id=%s",
            guild_id,
        )
        return

    channel = guild.get_channel(schedule_channel_id)
    if not isinstance(channel, discord.TextChannel):
        logger.warning(
            "Live dashboard update skipped because channel is invalid. guild_id=%s channel_id=%s",
            guild_id,
            schedule_channel_id,
        )
        return

    schedules = get_schedules(guild_id)
    intro = "\n".join(
        [
            "## 교내·외 대회 및 프로젝트 일정 타임라인",
            "일정이 등록되거나 정리될 때마다 실시간으로 알아서 자동 동기화됩니다.",
            SECTION_DIVIDER,
        ]
    )

    lines: list[str] = []
    if not schedules:
        lines.append("- 현재 등록된 활성 일정이 없습니다.")
    else:
        for schedule_id, _, title, start_ts, end_ts in schedules:
            lines.append(f"🆔 **ID: {schedule_id}**")
            lines.append(f"└ 📝  {title}")
            if start_ts is not None:
                lines.append(f"└ ⏳ 기간: <t:{start_ts}:F> ~ <t:{end_ts}:F>")
                lines.append(f"└ 📢 마감 카운트다운: <t:{end_ts}:R>")
            else:
                lines.append(f"└ 📢 마감 카운트다운: <t:{end_ts}:R>")
            lines.append("")

    body = "\n".join(lines).strip()
    description = "\n\n".join([intro, body]) if body else intro
    if len(description) > MAX_EMBED_DESCRIPTION_LENGTH:
        description = description[: MAX_EMBED_DESCRIPTION_LENGTH - 4].rstrip() + "..."

    embed = build_schedule_embed(
        title="📅 Team 0x34 LIVE SCHEDULE TIMELINE",
        description=description,
        guild=guild,
    )

    existing_message_id = get_schedule_message_id(guild_id)
    if existing_message_id is not None:
        try:
            message = await channel.fetch_message(existing_message_id)
            await message.edit(embed=embed)
            logger.info(
                "Live dashboard message updated. guild_id=%s channel_id=%s message_id=%s",
                guild_id,
                channel.id,
                existing_message_id,
            )
            return
        except discord.NotFound:
            logger.warning(
                "Existing dashboard message not found. guild_id=%s message_id=%s",
                guild_id,
                existing_message_id,
            )
        except discord.Forbidden:
            logger.exception(
                "Forbidden while editing live dashboard message. guild_id=%s channel_id=%s message_id=%s",
                guild_id,
                channel.id,
                existing_message_id,
            )
            return
        except discord.HTTPException:
            logger.exception(
                "HTTP exception while editing live dashboard message. guild_id=%s channel_id=%s message_id=%s",
                guild_id,
                channel.id,
                existing_message_id,
            )
            return

    try:
        new_message = await channel.send(embed=embed)
    except discord.Forbidden:
        logger.exception(
            "Forbidden while sending new live dashboard message. guild_id=%s channel_id=%s",
            guild_id,
            channel.id,
        )
        return
    except discord.HTTPException:
        logger.exception(
            "HTTP exception while sending new live dashboard message. guild_id=%s channel_id=%s",
            guild_id,
            channel.id,
        )
        return

    set_schedule_message_id(guild_id, new_message.id)
    logger.info(
        "Live dashboard message created and persisted. guild_id=%s channel_id=%s message_id=%s",
        guild_id,
        channel.id,
        new_message.id,
    )


__all__ = [
    "ERROR_EMBED_COLOR",
    "SECTION_DIVIDER",
    "build_schedule_embed",
    "build_schedule_error_embed",
    "respond_ephemeral",
    "respond_admin_permission_denied",
    "handle_command_error",
    "update_live_dashboard",
]
