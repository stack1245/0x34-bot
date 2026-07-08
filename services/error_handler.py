from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands


class BotErrorHandler:
    """명령어와 이벤트 오류 응답을 중앙에서 처리합니다."""

    def __init__(self, *, logger: logging.Logger | None = None) -> None:
        self.logger = logger or logging.getLogger(__name__)

    async def handle_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        original = getattr(error, "original", error)
        self.logger.exception("Application command failed: %s", original)

        if isinstance(error, app_commands.CommandOnCooldown):
            message = (
                f"잠시 후 다시 시도해 주세요. 남은 시간: {error.retry_after:.1f}초"
            )
        elif isinstance(error, app_commands.MissingPermissions):
            message = "이 명령어를 실행할 권한이 없습니다."
        elif isinstance(error, app_commands.BotMissingPermissions):
            message = "봇 권한이 부족해 작업을 완료하지 못했습니다."
        elif isinstance(original, discord.Forbidden):
            message = "Discord 권한이 부족해 작업을 완료하지 못했습니다."
        elif isinstance(original, discord.NotFound):
            message = (
                "대상 메시지 또는 채널을 찾을 수 없습니다. 상태를 새로고침해 주세요."
            )
        elif isinstance(original, discord.HTTPException):
            message = (
                "Discord API가 일시적으로 실패했습니다. 잠시 후 다시 시도해 주세요."
            )
        else:
            message = "예상치 못한 오류가 발생했습니다. 로그를 확인해 주세요."

        await self._send_interaction_error(interaction, message)

    async def handle_command_error(
        self, ctx: commands.Context, error: commands.CommandError
    ) -> None:
        original = getattr(error, "original", error)
        self.logger.exception("Text command failed: %s", original)

        if isinstance(error, commands.CommandNotFound):
            return
        if isinstance(error, commands.NotOwner):
            await ctx.reply("봇 소유자만 사용할 수 있는 명령어입니다.")
            return
        if isinstance(error, commands.MissingPermissions):
            await ctx.reply("이 명령어를 실행할 권한이 없습니다.")
            return
        await ctx.reply("명령어 처리 중 오류가 발생했습니다. 로그를 확인해 주세요.")

    async def handle_event_error(self, event_method: str, *args, **kwargs) -> None:
        self.logger.exception("Unhandled Discord event failed: %s", event_method)

    async def _send_interaction_error(
        self, interaction: discord.Interaction, message: str
    ) -> None:
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
            self.logger.warning("Failed to send error response: %s", exc)
