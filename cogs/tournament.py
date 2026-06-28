from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from utils.datetime import format_discord_timestamp, now_utc_iso, parse_datetime, to_storage_iso
from utils.embeds import SUCCESS_COLOR, base_embed


class TournamentModal(discord.ui.Modal, title="대회 등록"):
    """대회 정보를 한 번에 입력받는 Modal입니다."""

    title_input = discord.ui.TextInput(label="대회명", placeholder="예: 0x34 Internal CTF", max_length=100)
    platform_input = discord.ui.TextInput(label="플랫폼", placeholder="예: CTFtime, Dreamhack, Codeforces", max_length=80)
    starts_at_input = discord.ui.TextInput(label="일정", placeholder="예: 2026-07-01 19:00", max_length=40)
    link_input = discord.ui.TextInput(label="링크", placeholder="https://example.com", max_length=300)
    secret_input = discord.ui.TextInput(
        label="민감 정보 메모(선택)",
        placeholder="계정, 비밀번호, 토큰 등은 채널에 공개되지 않고 본인에게만 표시됩니다.",
        style=discord.TextStyle.long,
        required=False,
        max_length=1000,
    )

    def __init__(self, cog: "TournamentCog") -> None:
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        """대회 정보를 저장하고 알림 채널에 Embed를 보냅니다."""
        if interaction.guild is None:
            await interaction.response.send_message("서버 안에서만 대회를 등록할 수 있습니다.", ephemeral=True)
            return

        try:
            starts_at = parse_datetime(str(self.starts_at_input.value), self.cog.bot.settings.timezone)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        notice_channel = await self.cog.resolve_notice_channel(interaction)
        await self.cog.bot.database.execute(
            """
            INSERT INTO tournaments (guild_id, title, platform, starts_at, link, notice_channel_id, created_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                interaction.guild.id,
                str(self.title_input.value),
                str(self.platform_input.value),
                to_storage_iso(starts_at),
                str(self.link_input.value),
                notice_channel.id,
                interaction.user.id,
                now_utc_iso(),
            ),
        )

        embed = base_embed("새 대회가 등록되었습니다", color=SUCCESS_COLOR)
        embed.add_field(name="대회명", value=str(self.title_input.value), inline=False)
        embed.add_field(name="플랫폼", value=str(self.platform_input.value), inline=True)
        embed.add_field(name="일정", value=format_discord_timestamp(starts_at), inline=True)
        embed.add_field(name="링크", value=str(self.link_input.value), inline=False)
        embed.add_field(name="등록자", value=interaction.user.mention, inline=True)

        await notice_channel.send(embed=embed)

        secret = str(self.secret_input.value).strip()
        secret_message = "민감 정보 메모는 입력되지 않았습니다."
        if secret:
            secret_message = f"민감 정보 메모는 채널에 공개하지 않았습니다.\n```text\n{secret[:900]}\n```"

        await interaction.followup.send(
            f"대회를 등록하고 {notice_channel.mention}에 공지했습니다.\n{secret_message}",
            ephemeral=True,
        )


class TournamentCog(commands.Cog):
    """대회 등록과 알림 Embed 전송을 담당하는 Cog입니다."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def resolve_notice_channel(self, interaction: discord.Interaction) -> discord.abc.Messageable:
        """환경 변수에 알림 채널이 있으면 그 채널을, 없으면 현재 채널을 사용합니다."""
        channel_id = self.bot.settings.tournament_channel_id
        if interaction.guild is not None and channel_id is not None:
            channel = interaction.guild.get_channel(channel_id)
            if channel is None:
                channel = await self.bot.fetch_channel(channel_id)
            if isinstance(channel, discord.abc.Messageable):
                return channel

        if interaction.channel is None or not isinstance(interaction.channel, discord.abc.Messageable):
            raise RuntimeError("대회 알림을 보낼 채널을 찾을 수 없습니다.")
        return interaction.channel

    @app_commands.command(name="대회등록", description="Modal로 다가오는 CTF/대회를 등록합니다.")
    async def register_tournament(self, interaction: discord.Interaction) -> None:
        """대회 등록 Modal을 엽니다."""
        await interaction.response.send_modal(TournamentModal(self))


async def setup(bot: commands.Bot) -> None:
    """discord.py가 이 파일을 Cog로 로드할 때 호출하는 함수입니다."""
    await bot.add_cog(TournamentCog(bot))