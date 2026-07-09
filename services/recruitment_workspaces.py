from __future__ import annotations

import logging

import discord
from discord.ext import commands

from services.recruitment import (
    PARTICIPANT_ACCEPTED,
    PARTICIPANT_OWNER,
    RecruitmentService,
)
from services.recruitment_models import RecruitmentRecord
from utils.ai_input import trim_text
from utils.embeds import SUCCESS_COLOR, base_embed


class RecruitmentWorkspaceService:
    """Coordinates Discord private-thread side effects for recruitment flows.

    The service owns Discord API operations that are domain side effects, while
    the Cog remains responsible for routing interactions and composing views.
    """

    def __init__(
        self,
        bot: commands.Bot,
        recruitment_service: RecruitmentService,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        """Initializes the Discord workspace service.

        Args:
            bot: Discord bot instance used for channel and user lookups.
            recruitment_service: Domain service used to persist thread ids.
            logger: Optional structured logger.
        """
        self.bot = bot
        self.recruitment_service = recruitment_service
        self.logger = logger or logging.getLogger(__name__)

    async def resolve_recruitment_channel(
        self, interaction: discord.Interaction
    ) -> discord.abc.Messageable:
        """Resolves the configured recruitment channel for a new post.

        Args:
            interaction: Source interaction for guild and fallback channel data.

        Returns:
            A Discord messageable channel where the recruitment post should go.

        Raises:
            RuntimeError: If no messageable channel can be resolved.
        """
        channel_id = getattr(self.bot, "settings").recruitment_channel_id
        if interaction.guild is not None and channel_id is not None:
            channel = interaction.guild.get_channel(channel_id)
            if channel is None:
                channel = await self.bot.fetch_channel(channel_id)
            if isinstance(channel, discord.abc.Messageable):
                return channel

        if interaction.channel is None or not isinstance(
            interaction.channel, discord.abc.Messageable
        ):
            raise RuntimeError("모집 글을 보낼 채널을 찾을 수 없습니다.")
        return interaction.channel

    async def create_private_workspace_thread(
        self,
        interaction: discord.Interaction,
        channel: discord.abc.Messageable,
        title: str,
        source_message: discord.Message,
    ) -> tuple[discord.Thread | None, str | None]:
        """Creates a private workspace thread for a recruitment post.

        Args:
            interaction: Interaction that initiated the recruitment creation.
            channel: Parent channel where the recruitment message lives.
            title: Recruitment title used for the thread name.
            source_message: Public recruitment message to link from the intro.

        Returns:
            A tuple of the created thread and a user-facing notice, if any.
        """
        if not isinstance(channel, discord.TextChannel):
            return None, "비공개 스레드는 일반 텍스트 채널에서만 생성할 수 있습니다."

        thread_name = trim_text(f"{title} 워크스페이스", 90)
        try:
            thread = await channel.create_thread(
                name=thread_name,
                type=discord.ChannelType.private_thread,
                auto_archive_duration=1440,
                invitable=False,
                reason="Team 0x34 private recruitment workspace",
            )
        except discord.Forbidden:
            return (
                None,
                "봇에게 비공개 스레드 생성 권한이 없습니다. Create Private Threads, Send Messages in Threads, Manage Threads 권한을 확인해 주세요.",
            )
        except discord.HTTPException as exc:
            return (
                None,
                f"비공개 스레드 생성에 실패했습니다. 서버 부스트 레벨 또는 Discord API 제한을 확인해 주세요: {exc.text}",
            )

        author_notice = await self.add_user_to_private_thread(thread, interaction.user)
        intro_embed = base_embed(
            "Team 0x34 비공개 워크스페이스",
            "Team 0x34의 비공개 워크스페이스가 생성되었습니다.",
            color=SUCCESS_COLOR,
        )
        intro_embed.add_field(
            name="모집 글", value=source_message.jump_url, inline=False
        )
        intro_embed.add_field(
            name="접근 안내",
            value="작성자와 승인된 신청자만 이 스레드에 초대됩니다.",
            inline=False,
        )

        try:
            await thread.send(embed=intro_embed)
        except discord.HTTPException as exc:
            return (
                thread,
                f"비공개 스레드는 만들었지만 안내 Embed 전송에 실패했습니다: {exc.text}",
            )

        return thread, author_notice

    async def ensure_private_workspace_thread(
        self,
        recruitment: RecruitmentRecord,
        interaction: discord.Interaction,
        source_message: discord.Message,
    ) -> tuple[discord.Thread | None, str | None]:
        """Finds or creates the private workspace for a recruitment.

        Args:
            recruitment: Recruitment record with the optional stored thread id.
            interaction: Interaction that triggered the state transition.
            source_message: Public recruitment message that owns the view.

        Returns:
            The usable thread and an optional user-facing notice.
        """
        thread_id = recruitment["thread_id"]
        if thread_id is not None:
            thread = await self.fetch_private_thread(int(thread_id))
            if thread is not None:
                return thread, await self.add_user_to_private_thread(
                    thread, interaction.user
                )

        thread, notice = await self.create_private_workspace_thread(
            interaction,
            source_message.channel,
            str(recruitment["title"]),
            source_message,
        )
        if thread is not None:
            await self.recruitment_service.update_thread_id(
                int(recruitment["id"]), thread.id
            )
        return thread, notice

    async def fetch_private_thread(self, thread_id: int) -> discord.Thread | None:
        """Fetches a private thread by id from cache or Discord API.

        Args:
            thread_id: Discord thread snowflake.

        Returns:
            The thread when it is available and accessible, otherwise None.
        """
        channel = self.bot.get_channel(thread_id)
        if isinstance(channel, discord.Thread):
            return channel

        try:
            fetched = await self.bot.fetch_channel(thread_id)
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            return None
        if isinstance(fetched, discord.Thread):
            return fetched
        return None

    async def get_recruitment_thread(
        self, recruitment: RecruitmentRecord, guild: discord.Guild | None
    ) -> discord.Thread | None:
        """Resolves the private workspace thread for a recruitment.

        Args:
            recruitment: Recruitment record with the optional thread id.
            guild: Guild cache to check before an API fetch.

        Returns:
            The workspace thread when it exists and is accessible.
        """
        thread_id = recruitment["thread_id"]
        if thread_id is None:
            return None

        if guild is not None:
            thread = guild.get_thread(int(thread_id))
            if thread is not None:
                return thread

        return await self.fetch_private_thread(int(thread_id))

    async def add_member_to_private_thread(
        self, guild: discord.Guild, thread: discord.Thread, user_id: int
    ) -> str | None:
        """Adds a guild member to a private workspace by user id.

        Args:
            guild: Guild that owns the private thread.
            thread: Private workspace thread.
            user_id: Discord user id to invite.

        Returns:
            None on success, otherwise a user-facing failure notice.
        """
        try:
            member = await guild.fetch_member(int(user_id))
        except discord.NotFound:
            return "신청자를 서버 멤버 목록에서 찾을 수 없어 비공개 워크스페이스에 초대하지 못했습니다."
        except discord.Forbidden:
            return "봇에게 서버 멤버 조회 권한이 없어 비공개 워크스페이스에 초대하지 못했습니다."
        except discord.HTTPException as exc:
            return f"서버 멤버 조회 중 오류가 발생해 비공개 워크스페이스에 초대하지 못했습니다: {exc.text}"

        try:
            await thread.add_user(member)
        except discord.Forbidden:
            return "비공개 스레드 초대 권한이 없어 워크스페이스에 자동 초대하지 못했습니다. Manage Threads 권한을 확인해 주세요."
        except discord.HTTPException as exc:
            return f"비공개 스레드 초대에 실패했습니다: {exc.text}"
        return None

    async def add_user_to_private_thread(
        self, thread: discord.Thread, user: discord.abc.Snowflake
    ) -> str | None:
        """Adds a Discord user-like object to a private workspace.

        Args:
            thread: Private workspace thread.
            user: Member or snowflake object to add.

        Returns:
            None on success, otherwise a user-facing failure notice.
        """
        if not isinstance(user, discord.Member):
            return await self.add_member_to_private_thread(
                thread.guild, thread, int(user.id)
            )

        try:
            await thread.add_user(user)
        except discord.Forbidden:
            return "비공개 스레드 초대 권한이 없어 워크스페이스에 자동 초대하지 못했습니다. Manage Threads 권한을 확인해 주세요."
        except discord.HTTPException as exc:
            return f"비공개 스레드 초대에 실패했습니다: {exc.text}"
        return None

    async def remove_user_from_private_thread(
        self, thread: discord.Thread, user: discord.abc.Snowflake
    ) -> str | None:
        """Removes a user-like object from a private workspace.

        Args:
            thread: Private workspace thread.
            user: Snowflake object to remove.

        Returns:
            None on success, otherwise a user-facing failure notice.
        """
        try:
            await thread.remove_user(user)
        except discord.Forbidden:
            return "비공개 스레드에서 사용자를 제거할 권한이 없습니다. Manage Threads 권한을 확인해 주세요."
        except discord.NotFound:
            return None
        except discord.HTTPException as exc:
            return f"비공개 스레드 멤버 제거에 실패했습니다: {exc.text}"
        return None

    async def sync_private_thread_membership(
        self, recruitment: RecruitmentRecord, user: discord.abc.Snowflake, status: str
    ) -> str | None:
        """Synchronizes participant status with private thread membership.

        Args:
            recruitment: Recruitment that owns the participant status.
            user: User-like snowflake to add or remove.
            status: Participant status after the domain mutation.

        Returns:
            None on silent success, otherwise a user-facing notice.
        """
        thread_id = recruitment["thread_id"]
        if thread_id is None:
            return "비공개 워크스페이스가 아직 없어 스레드 초대는 건너뛰었습니다."

        thread = await self.fetch_private_thread(int(thread_id))
        if thread is None:
            return "저장된 비공개 워크스페이스를 찾을 수 없습니다. 작성자가 모집 마감 시 다시 생성할 수 있습니다."

        if status in {PARTICIPANT_ACCEPTED, PARTICIPANT_OWNER}:
            notice = await self.add_user_to_private_thread(thread, user)
            return notice or f"비공개 워크스페이스에 초대했습니다: {thread.mention}"

        if int(user.id) == int(recruitment["author_id"]):
            return None
        return await self.remove_user_from_private_thread(thread, user)

    async def notify_recruitment_owner(
        self, recruitment: RecruitmentRecord, applicant: discord.abc.User, reason: str
    ) -> str | None:
        """Notifies the recruitment owner about a new application.

        Args:
            recruitment: Recruitment that received the application.
            applicant: Discord user who submitted the application.
            reason: Application reason text.

        Returns:
            None on success, otherwise a user-facing delivery notice.
        """
        message = f"새 모집 신청이 도착했습니다.\n신청자: {applicant.mention}\n신청 사유: {reason or '작성되지 않음'}"
        thread_id = recruitment["thread_id"]
        if thread_id is not None:
            thread = await self.fetch_private_thread(int(thread_id))
            if thread is not None:
                try:
                    await thread.send(message)
                    return None
                except discord.HTTPException:
                    self.logger.info(
                        "Failed to send recruitment application notice to thread %s",
                        thread_id,
                    )

        try:
            owner = self.bot.get_user(
                int(recruitment["author_id"])
            ) or await self.bot.fetch_user(int(recruitment["author_id"]))
            await owner.send(message)
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            return "작성자에게 DM 알림을 보내지 못했습니다."
        return None
