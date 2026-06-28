from __future__ import annotations

import asyncio
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands


class MaintenanceCog(commands.Cog):
    """운영 중 DB 백업과 고아 데이터 정리를 담당하는 관리자 전용 Cog입니다."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    def get_database_path(self) -> Path:
        """설정에서 실제 SQLite DB 파일 경로를 안전하게 가져옵니다.

        `config.py`는 Railway의 `DB_PATH`를 1순위로 읽고, 기존 `DATABASE_PATH`를 fallback으로
        지원합니다. 따라서 여기서는 환경 변수를 직접 다시 읽지 않고 봇이 이미 들고 있는
        `settings.database_path`만 사용하면 로컬과 Railway가 같은 방식으로 동작합니다.
        """
        return Path(self.bot.settings.database_path)

    async def is_admin(self, interaction: discord.Interaction) -> bool:
        """서버 관리자 또는 봇 소유자만 DB 정리 명령을 실행할 수 있게 합니다."""
        if await self.bot.is_owner(interaction.user):
            return True
        if isinstance(interaction.user, discord.Member):
            return interaction.user.guild_permissions.administrator
        return False

    async def resolve_guild(self, guild_id: int) -> discord.Guild | None:
        """캐시에서 Guild를 찾고, 없으면 API로 한 번 더 조회합니다."""
        guild = self.bot.get_guild(guild_id)
        if guild is not None:
            return guild
        try:
            return await self.bot.fetch_guild(guild_id)
        except discord.NotFound:
            return None
        except (discord.Forbidden, discord.HTTPException):
            return None

    async def resolve_channel(self, channel_id: int) -> discord.abc.GuildChannel | discord.Thread | None:
        """캐시와 API를 모두 사용해 채널 또는 스레드가 아직 존재하는지 확인합니다."""
        channel = self.bot.get_channel(channel_id)
        if channel is not None:
            return channel
        try:
            fetched = await self.bot.fetch_channel(channel_id)
        except discord.NotFound:
            return None
        except (discord.Forbidden, discord.HTTPException):
            return None
        if isinstance(fetched, (discord.abc.GuildChannel, discord.Thread)):
            return fetched
        return None

    async def message_exists(self, channel_id: int, message_id: int) -> bool:
        """저장된 모집 메시지가 Discord에 아직 존재하는지 확인합니다."""
        channel = await self.resolve_channel(channel_id)
        if not isinstance(channel, discord.abc.Messageable):
            return False

        try:
            await channel.fetch_message(message_id)
        except discord.NotFound:
            return False
        except (discord.Forbidden, discord.HTTPException):
            # 권한 문제나 일시적 API 오류는 데이터 삭제 근거로 삼지 않습니다.
            return True
        return True

    async def thread_exists(self, thread_id: int) -> bool:
        """저장된 비공개 스레드가 아직 존재하는지 확인합니다."""
        thread = self.bot.get_channel(thread_id)
        if isinstance(thread, discord.Thread):
            return True

        try:
            fetched = await self.bot.fetch_channel(thread_id)
        except discord.NotFound:
            return False
        except (discord.Forbidden, discord.HTTPException):
            return True
        return isinstance(fetched, discord.Thread)

    async def scheduled_event_exists(self, guild_id: int, event_id: int) -> bool:
        """일정에 연결된 Discord 서버 이벤트가 아직 존재하는지 확인합니다."""
        guild = await self.resolve_guild(guild_id)
        if guild is None:
            return False

        try:
            await guild.fetch_scheduled_event(event_id)
        except discord.NotFound:
            return False
        except (discord.Forbidden, discord.HTTPException):
            return True
        return True

    async def cleanup_schedules(self) -> int:
        """삭제된 Discord 서버 이벤트를 가리키는 일정 레코드를 정리합니다."""
        deleted_count = 0
        rows = await self.bot.database.fetch_all(
            """
            SELECT id, guild_id, event_id FROM schedules
            WHERE event_id IS NOT NULL
            """,
        )

        for row in rows:
            exists = await self.scheduled_event_exists(int(row["guild_id"]), int(row["event_id"]))
            if not exists:
                await self.bot.database.execute("DELETE FROM schedules WHERE id = ?", (row["id"],))
                deleted_count += 1
            await asyncio.sleep(0.5)

        return deleted_count

    async def cleanup_tournaments(self) -> int:
        """삭제된 알림 채널을 가리키는 대회 레코드를 정리합니다.

        현재 tournaments 테이블은 공지 메시지 ID를 저장하지 않으므로 메시지 삭제 여부는 검증할 수 없습니다.
        대신 저장된 notice_channel_id가 사라진 경우만 고아 데이터로 판단합니다.
        """
        deleted_count = 0
        rows = await self.bot.database.fetch_all("SELECT id, notice_channel_id FROM tournaments")

        for row in rows:
            channel = await self.resolve_channel(int(row["notice_channel_id"]))
            if channel is None:
                await self.bot.database.execute("DELETE FROM tournaments WHERE id = ?", (row["id"],))
                deleted_count += 1
            await asyncio.sleep(0.5)

        return deleted_count

    async def cleanup_recruitments(self) -> int:
        """삭제된 모집 메시지 또는 비공개 스레드를 가리키는 모집 레코드를 정리합니다."""
        deleted_count = 0
        rows = await self.bot.database.fetch_all(
            """
            SELECT id, channel_id, message_id, thread_id FROM recruitments
            """,
        )

        for row in rows:
            message_still_exists = await self.message_exists(int(row["channel_id"]), int(row["message_id"]))
            if not message_still_exists:
                await self.bot.database.execute("DELETE FROM recruitments WHERE id = ?", (row["id"],))
                deleted_count += 1
                await asyncio.sleep(0.5)
                continue

            thread_id = row["thread_id"]
            if thread_id is not None:
                thread_still_exists = await self.thread_exists(int(thread_id))
                if not thread_still_exists:
                    await self.bot.database.execute(
                        "UPDATE recruitments SET thread_id = NULL WHERE id = ?",
                        (row["id"],),
                    )
                    deleted_count += 1

            await asyncio.sleep(0.5)

        return deleted_count

    @app_commands.command(name="db정리", description="관리자 전용: 백업 후 삭제된 Discord 객체의 DB 고아 데이터를 정리합니다.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def cleanup_database(self, interaction: discord.Interaction) -> None:
        """현재 DB 파일을 먼저 백업 전송한 뒤 고아 데이터를 삭제합니다."""
        await interaction.response.defer(ephemeral=True)

        if not await self.is_admin(interaction):
            await interaction.followup.send("이 명령어는 서버 관리자만 사용할 수 있습니다.", ephemeral=True)
            return

        db_path = self.get_database_path()
        if not db_path.exists() or not db_path.is_file():
            await interaction.followup.send(f"SQLite DB 파일을 찾을 수 없습니다: `{db_path}`", ephemeral=True)
            return

        try:
            db_file = discord.File(db_path, filename=db_path.name)
            await interaction.followup.send(
                content="🧹 DB 청소를 시작하기 전 백업본을 전송합니다.",
                file=db_file,
                ephemeral=True,
            )
        except (OSError, discord.HTTPException) as exc:
            await interaction.followup.send(f"DB 백업 파일 전송에 실패해 정리를 중단합니다: `{exc}`", ephemeral=True)
            return

        deleted_count = 0
        deleted_count += await self.cleanup_schedules()
        deleted_count += await self.cleanup_tournaments()
        deleted_count += await self.cleanup_recruitments()

        await interaction.followup.send(
            f"✅ DB 정리가 완료되었습니다. (삭제된 쓰레기 데이터: 총 {deleted_count}개)",
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    """discord.py가 이 파일을 Cog로 로드할 때 호출하는 함수입니다."""
    await bot.add_cog(MaintenanceCog(bot))