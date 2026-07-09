from __future__ import annotations

import logging
from collections.abc import Callable

import discord
from discord.ext import commands

from services.recruitment import RecruitmentService
from services.recruitment_embeds import RecruitmentEmbedFactory
from services.recruitment_models import RecruitmentRecord

RecruitmentViewFactory = Callable[[RecruitmentRecord | None], discord.ui.View]


class RecruitmentMessageService:
    """Builds and edits public Discord recruitment messages.

    Message fetching and editing is isolated here so Cogs do not need to own
    Discord persistence details after domain state changes.
    """

    def __init__(
        self,
        bot: commands.Bot,
        recruitment_service: RecruitmentService,
        embed_factory: RecruitmentEmbedFactory,
        view_factory: RecruitmentViewFactory,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        """Initializes the recruitment message service.

        Args:
            bot: Discord bot instance used for channel lookups.
            recruitment_service: Domain service used to hydrate message state.
            embed_factory: Embed builder for recruitment surfaces.
            view_factory: Factory that creates a state-aware recruitment view.
            logger: Optional structured logger.
        """
        self.bot = bot
        self.recruitment_service = recruitment_service
        self.embed_factory = embed_factory
        self.view_factory = view_factory
        self.logger = logger or logging.getLogger(__name__)

    async def build_recruitment_embed(self, message_id: int) -> discord.Embed:
        """Builds the current public recruitment embed for a message id.

        Args:
            message_id: Discord message id stored with the recruitment row.

        Returns:
            A Discord embed reflecting the current DB state.
        """
        recruitment = await self.recruitment_service.get_recruitment_by_message_id(
            message_id
        )
        if recruitment is None:
            return self.embed_factory.build_missing_recruitment_embed()

        confirmed = await self.recruitment_service.get_confirmed_participants(
            int(recruitment["id"])
        )
        pending_count = await self.recruitment_service.get_pending_participant_count(
            int(recruitment["id"])
        )
        return self.embed_factory.build_recruitment_embed(
            recruitment, confirmed, pending_count
        )

    async def edit_recruitment_message(
        self, channel_id: int, message_id: int
    ) -> str | None:
        """Edits a stored public recruitment message in place.

        Args:
            channel_id: Discord channel id that contains the message.
            message_id: Discord message id to edit.

        Returns:
            None on success, otherwise a user-facing notice explaining that only
            the DB mutation completed.
        """
        try:
            channel = self.bot.get_channel(int(channel_id))
            if channel is None:
                channel = await self.bot.fetch_channel(int(channel_id))
            if not hasattr(channel, "fetch_message"):
                return "저장된 채널에서 모집 메시지를 가져올 수 없어 Discord 메시지는 수정하지 못했습니다."

            message = await channel.fetch_message(int(message_id))
            embed = await self.build_recruitment_embed(int(message_id))
            recruitment = await self.recruitment_service.get_recruitment_by_message_id(
                int(message_id)
            )
            await message.edit(embed=embed, view=self.view_factory(recruitment))
        except (TypeError, ValueError):
            return "저장된 채널 또는 메시지 ID가 올바르지 않아 Discord 메시지는 수정하지 못했습니다."
        except discord.NotFound:
            return "기존 모집 메시지를 찾을 수 없어 DB만 수정했습니다."
        except discord.Forbidden:
            return "모집 메시지를 수정할 권한이 없어 DB만 수정했습니다."
        except discord.HTTPException as exc:
            return f"모집 메시지 수정 중 오류가 발생해 DB만 수정했습니다: {exc.text}"
        return None
