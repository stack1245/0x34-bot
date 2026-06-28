from __future__ import annotations

from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands

from utils.datetime import format_discord_timestamp, from_storage_iso, now_utc_iso, parse_datetime, to_storage_iso
from utils.embeds import SUCCESS_COLOR, WARNING_COLOR, base_embed


class ScheduleModal(discord.ui.Modal, title="일정 추가"):
    """Slash Command에서 띄우는 입력 창입니다. Discord Modal은 짧은 폼 입력에 적합합니다."""

    title_input = discord.ui.TextInput(label="제목", placeholder="예: Team 0x34 정기 회의", max_length=100)
    starts_at_input = discord.ui.TextInput(label="날짜/시간", placeholder="예: 2026-07-01 19:00", max_length=40)
    body_input = discord.ui.TextInput(
        label="내용",
        placeholder="회의 안건, 준비물, 장소 등을 적어 주세요.",
        style=discord.TextStyle.long,
        max_length=1000,
    )

    def __init__(self, cog: "ScheduleCog") -> None:
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        """사용자가 Modal을 제출하면 일정을 DB에 저장하고 필요하면 서버 이벤트도 만듭니다."""
        if interaction.guild is None:
            await interaction.response.send_message("서버 안에서만 일정을 추가할 수 있습니다.", ephemeral=True)
            return

        try:
            starts_at = parse_datetime(str(self.starts_at_input.value), self.cog.bot.settings.timezone)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        cursor = await self.cog.bot.database.execute(
            """
            INSERT INTO schedules (guild_id, title, starts_at, body, created_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                interaction.guild.id,
                str(self.title_input.value),
                to_storage_iso(starts_at),
                str(self.body_input.value),
                interaction.user.id,
                now_utc_iso(),
            ),
        )

        event_id: int | None = None
        event_status = "서버 이벤트 생성은 비활성화되어 있습니다."
        if self.cog.bot.settings.enable_server_events:
            event_id, event_status = await self.cog.create_server_event(
                interaction.guild,
                str(self.title_input.value),
                starts_at,
                str(self.body_input.value),
            )

        if event_id is not None:
            await self.cog.bot.database.execute(
                "UPDATE schedules SET event_id = ? WHERE id = ?",
                (event_id, cursor.lastrowid),
            )

        notice_channel = await self.cog.resolve_schedule_channel(interaction)
        if notice_channel is not None:
            embed = base_embed("새 일정이 등록되었습니다", color=SUCCESS_COLOR)
            embed.add_field(name="제목", value=str(self.title_input.value), inline=False)
            embed.add_field(name="시간", value=format_discord_timestamp(starts_at), inline=True)
            embed.add_field(name="등록자", value=interaction.user.mention, inline=True)
            embed.add_field(name="내용", value=str(self.body_input.value)[:1024], inline=False)
            await notice_channel.send(embed=embed)

        await interaction.followup.send(
            f"일정이 등록되었습니다.\n- 시간: {format_discord_timestamp(starts_at)}\n- {event_status}",
            ephemeral=True,
        )


class ScheduleCog(commands.Cog):
    """일정 조회와 일정 추가 기능을 담당하는 Cog입니다."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def create_server_event(
        self,
        guild: discord.Guild,
        title: str,
        starts_at,
        description: str,
    ) -> tuple[int | None, str]:
        """Discord 서버 이벤트 API를 호출합니다. 권한이 없으면 실패 메시지만 돌려줍니다."""
        try:
            event = await guild.create_scheduled_event(
                name=title,
                start_time=starts_at,
                end_time=starts_at + timedelta(hours=1),
                description=description[:1000],
                entity_type=discord.EntityType.external,
                privacy_level=discord.PrivacyLevel.guild_only,
                location="Discord",
                reason="Team 0x34 일정 등록",
            )
        except discord.Forbidden:
            return None, "서버 이벤트 권한이 없어 DB에만 저장했습니다."
        except discord.HTTPException as exc:
            return None, f"서버 이벤트 생성에 실패해 DB에만 저장했습니다: {exc.text}"
        return event.id, "Discord 서버 이벤트도 함께 생성했습니다."

    async def resolve_schedule_channel(self, interaction: discord.Interaction) -> discord.abc.Messageable | None:
        """환경 변수에 일정 채널이 지정된 경우 공지할 채널을 찾습니다."""
        channel_id = self.bot.settings.schedule_channel_id
        if interaction.guild is None or channel_id is None:
            return None

        channel = interaction.guild.get_channel(channel_id)
        if channel is None:
            channel = await self.bot.fetch_channel(channel_id)
        if isinstance(channel, discord.abc.Messageable):
            return channel
        return None

    @app_commands.command(name="일정", description="Team 0x34의 등록된 일정을 Embed로 확인합니다.")
    async def list_schedules(self, interaction: discord.Interaction) -> None:
        """서버에 등록된 일정을 시간순으로 보여줍니다."""
        if interaction.guild is None:
            await interaction.response.send_message("서버 안에서만 일정을 확인할 수 있습니다.", ephemeral=True)
            return

        rows = await self.bot.database.fetch_all(
            """
            SELECT * FROM schedules
            WHERE guild_id = ?
            ORDER BY starts_at ASC
            LIMIT 20
            """,
            (interaction.guild.id,),
        )

        if not rows:
            embed = base_embed("Team 0x34 일정", "등록된 일정이 없습니다.", color=WARNING_COLOR)
            await interaction.response.send_message(embed=embed)
            return

        embed = base_embed("Team 0x34 일정", "가까운 일정부터 최대 20개까지 표시합니다.")
        for row in rows:
            starts_at = from_storage_iso(row["starts_at"], self.bot.settings.timezone)
            value = f"{format_discord_timestamp(starts_at)}\n{row['body']}\n등록자: <@{row['created_by']}>"
            if row["event_id"]:
                value += f"\n서버 이벤트 ID: `{row['event_id']}`"
            embed.add_field(name=row["title"], value=value[:1024], inline=False)

        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="일정추가", description="Modal로 새 일정을 등록합니다.")
    async def add_schedule(self, interaction: discord.Interaction) -> None:
        """Discord Modal을 열어 일정 정보를 입력받습니다."""
        await interaction.response.send_modal(ScheduleModal(self))


async def setup(bot: commands.Bot) -> None:
    """discord.py가 이 파일을 Cog로 로드할 때 호출하는 함수입니다."""
    await bot.add_cog(ScheduleCog(bot))