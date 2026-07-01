from __future__ import annotations

import logging
from datetime import datetime, time
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils.embeds import SUCCESS_COLOR, WARNING_COLOR, base_embed


KST = ZoneInfo("Asia/Seoul")
SCHEDULE_BOARD_STATE_KEY = "schedule_board"
SCHEDULE_BOARD_FOOTER = "이 대시보드는 디스코드 서버 이벤트와 실시간으로 동기화됩니다."
ACTIVE_EVENT_STATUSES = {discord.EventStatus.scheduled, discord.EventStatus.active}


class ScheduleCog(commands.Cog):
    """Discord Scheduled Events를 읽어 일정 대시보드만 동기화하는 Cog입니다."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        """Cog가 로드될 때 매일 자정 대시보드 갱신 루프를 시작합니다."""
        if not self.refresh_schedule_board_at_midnight.is_running():
            self.refresh_schedule_board_at_midnight.start()

    async def cog_unload(self) -> None:
        """Cog가 언로드될 때 백그라운드 루프를 안전하게 중단합니다."""
        self.refresh_schedule_board_at_midnight.cancel()

    async def fetch_schedule_board_state(self):
        """DB에서 일정 대시보드 메시지 위치를 가져옵니다."""
        return await self.bot.state_manager.get_dashboard_state(SCHEDULE_BOARD_STATE_KEY)

    async def save_schedule_board_state(self, channel_id: int, message_id: int) -> None:
        """일정 대시보드 메시지 위치를 DB에 저장하거나 덮어씁니다."""
        await self.bot.state_manager.save_dashboard_state(SCHEDULE_BOARD_STATE_KEY, channel_id, message_id)

    async def clear_schedule_board_state(self) -> None:
        """삭제된 채널/메시지를 가리키는 일정 대시보드 상태를 제거합니다."""
        await self.bot.state_manager.clear_dashboard_state(SCHEDULE_BOARD_STATE_KEY)

    async def resolve_board_channel(self, channel_id: int) -> discord.TextChannel | None:
        """저장된 채널 ID를 메시지 편집 가능한 텍스트 채널로 해석합니다."""
        try:
            channel = self.bot.get_channel(channel_id)
            if channel is None:
                channel = await self.bot.fetch_channel(channel_id)
        except discord.NotFound:
            await self.clear_schedule_board_state()
            return None
        except (discord.Forbidden, discord.HTTPException) as exc:
            logging.warning("Failed to resolve schedule board channel %s: %s", channel_id, exc)
            return None

        if isinstance(channel, discord.TextChannel):
            return channel

        logging.warning("Schedule board channel %s is not a text channel.", channel_id)
        return None

    def get_active_scheduled_events(self, guild: discord.Guild) -> list[discord.ScheduledEvent]:
        """Guild 캐시에 있는 예정/진행 중 서버 이벤트를 시작 시간순으로 반환합니다."""
        events = [
            event
            for event in guild.scheduled_events
            if event.status in ACTIVE_EVENT_STATUSES and event.start_time is not None
        ]
        return sorted(events, key=lambda event: event.start_time)

    def format_event_timestamp(self, value: datetime) -> str:
        """Discord absolute/relative timestamp 쌍을 반환합니다."""
        timestamp = int(value.timestamp())
        return f"<t:{timestamp}:F> (<t:{timestamp}:R>)"

    def format_event_range(self, event: discord.ScheduledEvent) -> str:
        """서버 이벤트의 시작/종료 시간을 대시보드 표기 형식으로 반환합니다."""
        start = self.format_event_timestamp(event.start_time)
        if event.end_time is None:
            return f"{start} ~ 종료 시간 미정"
        return f"{start} ~ {self.format_event_timestamp(event.end_time)}"

    def format_d_day(self, event: discord.ScheduledEvent) -> str:
        """자정 갱신 때 함께 새로 계산되는 D-Day 텍스트를 반환합니다."""
        event_date = event.start_time.astimezone(KST).date()
        days = (event_date - datetime.now(KST).date()).days
        if days == 0:
            return "D-Day"
        if days > 0:
            return f"D-{days}"
        return f"D+{abs(days)}"

    def format_event_status(self, event: discord.ScheduledEvent) -> str:
        """Discord 이벤트 상태를 한국어 대시보드 문구로 반환합니다."""
        if event.status is discord.EventStatus.active:
            return "진행 중"
        return "예정"

    def format_event_location(self, event: discord.ScheduledEvent) -> str | None:
        """이벤트 채널 또는 외부 장소를 사람이 읽기 쉬운 문자열로 반환합니다."""
        channel = getattr(event, "channel", None)
        if channel is not None:
            return channel.mention
        location = getattr(event, "location", None)
        if location:
            return str(location)
        return None

    def build_event_line(self, event: discord.ScheduledEvent) -> str:
        """대시보드 description에 들어갈 이벤트 한 줄 블록을 조립합니다."""
        parts = [
            f"• **{event.name}** `{self.format_d_day(event)}`",
            f"  {self.format_event_range(event)}",
            f"  상태: {self.format_event_status(event)}",
        ]
        location = self.format_event_location(event)
        if location:
            parts.append(f"  장소: {location}")
        return "\n".join(parts)

    def build_schedule_board_embed(self, guild: discord.Guild) -> discord.Embed:
        """Discord Scheduled Events 목록에서 일정 대시보드 Embed를 만듭니다."""
        events = self.get_active_scheduled_events(guild)
        if not events:
            embed = base_embed("Team 0x34 일정 대시보드", "예정 또는 진행 중인 디스코드 서버 이벤트가 없습니다.", color=WARNING_COLOR)
            embed.set_footer(text=SCHEDULE_BOARD_FOOTER)
            return embed

        lines: list[str] = []
        for event in events:
            next_line = self.build_event_line(event)
            candidate = "\n\n".join([*lines, next_line])
            if len(candidate) > 4000:
                lines.append("• 표시할 수 있는 길이를 넘어 이후 이벤트는 생략되었습니다.")
                break
            lines.append(next_line)

        embed = base_embed("Team 0x34 일정 대시보드", "\n\n".join(lines), color=SUCCESS_COLOR)
        embed.set_footer(text=SCHEDULE_BOARD_FOOTER)
        return embed

    async def update_schedule_board(self, guild: discord.Guild | None = None) -> None:
        """저장된 대시보드 메시지를 현재 Discord Scheduled Events 기준으로 갱신합니다."""
        state = await self.fetch_schedule_board_state()
        if state is None or state.board_channel_id is None or state.board_message_id is None:
            return

        channel = await self.resolve_board_channel(int(state.board_channel_id))
        if channel is None:
            return

        if guild is None:
            guild = channel.guild
        elif guild.id != channel.guild.id:
            return

        embed = self.build_schedule_board_embed(guild)
        try:
            message = await channel.fetch_message(int(state.board_message_id))
        except discord.NotFound:
            try:
                message = await channel.send(embed=embed)
            except discord.Forbidden as exc:
                logging.warning("Missing permission to recreate schedule board: %s", exc)
                return
            except discord.HTTPException as exc:
                logging.warning("Failed to recreate schedule board message: %s", exc)
                return
            await self.save_schedule_board_state(channel.id, message.id)
            return
        except discord.Forbidden as exc:
            logging.warning("Missing permission to fetch schedule board message: %s", exc)
            return
        except discord.HTTPException as exc:
            logging.warning("Failed to fetch schedule board message: %s", exc)
            return

        try:
            await message.edit(embed=embed)
        except discord.Forbidden as exc:
            logging.warning("Missing permission to update schedule board: %s", exc)
        except discord.HTTPException as exc:
            logging.warning("Failed to update schedule board message: %s", exc)

    @commands.Cog.listener()
    async def on_scheduled_event_create(self, event: discord.ScheduledEvent) -> None:
        """새 서버 이벤트가 생성되면 대시보드를 즉시 갱신합니다."""
        await self.update_schedule_board(event.guild)

    @commands.Cog.listener()
    async def on_scheduled_event_update(self, before: discord.ScheduledEvent, after: discord.ScheduledEvent) -> None:
        """서버 이벤트가 수정되면 대시보드를 즉시 갱신합니다."""
        await self.update_schedule_board(after.guild)

    @commands.Cog.listener()
    async def on_scheduled_event_delete(self, event: discord.ScheduledEvent) -> None:
        """서버 이벤트가 삭제되면 대시보드를 즉시 갱신합니다."""
        await self.update_schedule_board(event.guild)

    @tasks.loop(time=time(hour=0, minute=0, tzinfo=KST))
    async def refresh_schedule_board_at_midnight(self) -> None:
        """매일 자정 KST에 D-Day와 상대 시간 표기를 다시 동기화합니다."""
        await self.update_schedule_board()

    @refresh_schedule_board_at_midnight.before_loop
    async def before_refresh_schedule_board_at_midnight(self) -> None:
        """봇 준비가 끝난 뒤 자정 갱신 루프를 시작합니다."""
        try:
            await self.bot.wait_until_ready()
        except RuntimeError:
            self.refresh_schedule_board_at_midnight.stop()

    @app_commands.command(name="일정대시보드", description="디스코드 서버 이벤트 기반 일정 대시보드를 생성합니다.")
    @app_commands.describe(channel="일정 대시보드를 띄울 채널")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def create_schedule_dashboard(self, interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        """관리자가 지정한 채널에 새 일정 대시보드를 만들고 상태를 저장합니다."""
        if interaction.guild is None:
            await interaction.response.send_message("서버 안에서만 일정 대시보드를 만들 수 있습니다.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        embed = self.build_schedule_board_embed(interaction.guild)
        try:
            message = await channel.send(embed=embed)
        except discord.Forbidden:
            await interaction.followup.send("대시보드 메시지를 보낼 권한이 없습니다.", ephemeral=True)
            return
        except discord.HTTPException as exc:
            await interaction.followup.send(f"대시보드 메시지 전송에 실패했습니다: `{exc}`", ephemeral=True)
            return

        await self.save_schedule_board_state(channel.id, message.id)
        await self.update_schedule_board(interaction.guild)
        await interaction.followup.send(
            "✅ 대시보드 동기화 완료. 이제 일정 관리는 디스코드 기본 '이벤트' 기능을 이용하세요.",
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    """discord.py가 이 파일을 Cog로 로드할 때 호출하는 함수입니다."""
    await bot.add_cog(ScheduleCog(bot))