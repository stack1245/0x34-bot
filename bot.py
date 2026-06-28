from __future__ import annotations

import asyncio
import logging

import discord
from discord.ext import commands

from config import Settings, load_settings
from utils.database import Database


# Cog는 기능 단위 모듈입니다. 새 기능을 추가할 때 여기에 모듈 경로만 더하면 됩니다.
EXTENSIONS: tuple[str, ...] = (
    "cogs.schedule",
    "cogs.tournament",
    "cogs.recruitment",
    "cogs.maintenance",
)


class Team0x34Bot(commands.Bot):
    """Team 0x34 봇의 설정, DB, Cog 로딩을 담당하는 Bot 클래스입니다."""

    def __init__(self, settings: Settings) -> None:
        # 기본 운영은 Slash Command, Button, Modal만 사용하므로 message_content 특권 인텐트가 필요 없습니다.
        # 단, 슬래시 커맨드가 꼬였을 때 쓰는 비상용 `!인증 ...` 텍스트 명령을 켜면
        # Discord Developer Portal에서도 Message Content Intent를 활성화해야 합니다.
        intents = discord.Intents.default()
        intents.message_content = settings.enable_admin_text_commands
        super().__init__(command_prefix="!", intents=intents)

        self.settings = settings
        self.database = Database(settings.database_path)
        if settings.enable_admin_text_commands:
            self._install_owner_sync_commands()

    async def setup_hook(self) -> None:
        """Discord에 로그인한 직후, on_ready보다 먼저 실행되는 초기화 지점입니다."""
        await self.database.connect()

        for extension in EXTENSIONS:
            await self.load_extension(extension)

        if self.settings.sync_commands:
            await self.sync_application_commands_on_start()

    async def clear_global_application_commands(self) -> None:
        """Discord API 서버에 등록된 전역 Slash Command를 전부 삭제합니다.

        `tree.clear_commands(guild=None)`는 로컬 CommandTree의 전역 명령 목록을 비웁니다.
        그 직후 `await tree.sync()`를 호출해야 Discord 서버 쪽 전역 명령도 실제로 삭제됩니다.

        주의: 전역 명령은 Discord 캐시 정책 때문에 삭제/재등록 반영에 최대 1시간이 걸릴 수 있습니다.
        그래서 개발 중에는 GUILD_ID를 지정하고 Guild 단위 동기화를 쓰는 편이 훨씬 빠릅니다.
        """
        current_global_commands = list(self.tree.get_commands(guild=None))
        self.tree.clear_commands(guild=None)
        await self.tree.sync()

        # clear_commands는 로컬 트리도 지우므로, 현재 코드의 명령 객체를 다시 넣어 둡니다.
        # 이렇게 해야 다음 sync에서 오래된 명령이 아니라 지금 코드에 있는 명령만 다시 등록됩니다.
        for command in current_global_commands:
            self.tree.add_command(command)
        logging.info("Cleared remote global application commands")

    async def clean_sync_global_application_commands(self) -> list[discord.app_commands.AppCommand]:
        """전역 명령을 완전히 지운 뒤 현재 코드에 있는 전역 명령만 다시 등록합니다."""
        await self.clear_global_application_commands()
        synced = await self.tree.sync()
        logging.info("Clean-synced %s global commands", len(synced))
        return synced

    async def clear_guild_application_commands(self, guild: discord.Object) -> None:
        """Discord API 서버에 등록된 특정 서버 Slash Command를 전부 삭제합니다.

        `tree.clear_commands(guild=guild)`로 로컬 Guild 명령 목록을 비운 뒤
        `await tree.sync(guild=guild)`를 호출해야 해당 서버에 남아 있는 고스트 명령이 삭제됩니다.
        Guild 명령은 보통 몇 초 안에 반영되므로 개발/테스트에는 이 경로를 추천합니다.
        """
        self.tree.clear_commands(guild=guild)
        await self.tree.sync(guild=guild)
        logging.info("Cleared remote guild application commands for %s", guild.id)

    async def clean_sync_guild_application_commands(self, guild: discord.Object) -> list[discord.app_commands.AppCommand]:
        """특정 서버 명령을 완전히 지운 뒤 현재 코드의 명령만 Guild 명령으로 다시 등록합니다."""
        await self.clear_guild_application_commands(guild)
        self.tree.copy_global_to(guild=guild)
        synced = await self.tree.sync(guild=guild)
        logging.info("Clean-synced %s guild commands to %s", len(synced), guild.id)
        return synced

    async def sync_application_commands_on_start(self) -> None:
        """환경 변수 조건에 따라 시작 시 Slash Command를 동기화합니다."""
        guild = discord.Object(id=self.settings.guild_id) if self.settings.guild_id is not None else None

        if self.settings.clear_commands_on_start:
            # 고스트 커맨드를 확실히 없애려면 전역과 테스트 서버 양쪽을 먼저 비웁니다.
            # GUILD_ID가 설정된 개발 환경에서는 이후 Guild 명령만 빠르게 재등록합니다.
            await self.clear_global_application_commands()
            if guild is not None:
                await self.clear_guild_application_commands(guild)

        if guild is not None:
            # 개발 중에는 GUILD_ID를 지정하면 커맨드가 거의 즉시 테스트 서버에 반영됩니다.
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            logging.info("Synced %s guild commands to %s", len(synced), guild.id)
            return

        # 전역 명령 동기화는 모든 서버에 배포할 때 사용합니다. Discord 반영은 최대 1시간 걸릴 수 있습니다.
        synced = await self.tree.sync()
        logging.info("Synced %s global commands", len(synced))

    def _resolve_command_guild(self, ctx: commands.Context["Team0x34Bot"]) -> discord.Object | None:
        """관리자 텍스트 명령에서 사용할 Guild를 현재 채널 또는 GUILD_ID로 결정합니다."""
        if ctx.guild is not None:
            return discord.Object(id=ctx.guild.id)
        if self.settings.guild_id is not None:
            return discord.Object(id=self.settings.guild_id)
        return None

    def _install_owner_sync_commands(self) -> None:
        """슬래시 커맨드가 꼬였을 때 봇 소유자만 쓸 수 있는 비상 텍스트 명령을 등록합니다."""

        @commands.group(name="인증", invoke_without_command=True)
        @commands.is_owner()
        async def auth_group(ctx: commands.Context[Team0x34Bot]) -> None:
            await ctx.reply("사용법: `!인증 sync [guild|global|all]` 또는 `!인증 clear [guild|global|all]`")

        @auth_group.command(name="clear")
        @commands.is_owner()
        async def clear_commands(ctx: commands.Context[Team0x34Bot], scope: str = "guild") -> None:
            scope = scope.lower()
            if scope not in {"guild", "global", "all"}:
                await ctx.reply("scope는 `guild`, `global`, `all` 중 하나여야 합니다.")
                return

            guild = self._resolve_command_guild(ctx)
            if scope in {"guild", "all"} and guild is None:
                await ctx.reply("Guild 명령을 지우려면 서버 채널에서 실행하거나 GUILD_ID를 설정해 주세요.")
                return

            if scope in {"global", "all"}:
                await self.clear_global_application_commands()
            if scope in {"guild", "all"} and guild is not None:
                await self.clear_guild_application_commands(guild)

            await ctx.reply(f"`{scope}` 범위의 Slash Command를 초기화했습니다.")

        @auth_group.command(name="sync")
        @commands.is_owner()
        async def sync_commands(ctx: commands.Context[Team0x34Bot], scope: str = "guild") -> None:
            scope = scope.lower()
            if scope not in {"guild", "global", "all"}:
                await ctx.reply("scope는 `guild`, `global`, `all` 중 하나여야 합니다.")
                return

            guild = self._resolve_command_guild(ctx)
            if scope in {"guild", "all"} and guild is None:
                await ctx.reply("Guild 명령을 동기화하려면 서버 채널에서 실행하거나 GUILD_ID를 설정해 주세요.")
                return

            messages: list[str] = []
            if scope in {"global", "all"}:
                synced = await self.clean_sync_global_application_commands()
                messages.append(f"global {len(synced)}개")
            if scope in {"guild", "all"} and guild is not None:
                synced = await self.clean_sync_guild_application_commands(guild)
                messages.append(f"guild({guild.id}) {len(synced)}개")

            await ctx.reply("Slash Command를 초기화 후 재동기화했습니다: " + ", ".join(messages))

        self.add_command(auth_group)

    async def close(self) -> None:
        """봇 종료 시 SQLite 연결도 같이 정리합니다."""
        await self.database.close()
        await super().close()

    async def on_ready(self) -> None:
        """봇이 Discord Gateway에 완전히 연결되면 현재 계정을 로그에 남깁니다."""
        if self.user is None:
            logging.info("Bot is ready")
            return
        logging.info("Logged in as %s (%s)", self.user, self.user.id)


async def main() -> None:
    """설정을 읽고 봇을 실행하는 진입점입니다."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()

    async with Team0x34Bot(settings) as bot:
        await bot.start(settings.token)


if __name__ == "__main__":
    asyncio.run(main())