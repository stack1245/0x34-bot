from __future__ import annotations

import asyncio
import json
import logging
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands
import google.generativeai as genai

from utils.datetime import (
    fix_past_year,
    format_discord_timestamp,
    from_storage_iso,
    get_current_time_context,
    now_utc_iso,
    parse_datetime,
    to_storage_iso,
)
from utils.embeds import SUCCESS_COLOR, WARNING_COLOR, base_embed


SCHEDULE_GENERATION_PROMPT = """
제공된 텍스트에서 '참가 신청', '예선', '본선' 등 유의미한 모든 일정을 찾아내어 JSON 배열(Array) 형태로 반환해라.
각 일정의 제목(title) 앞에는 성격을 나타내는 태그(예: [참가신청], [예선], [본선])를 붙여라.
반드시 아래 JSON 스키마의 배열만 반환해라.
[
    {
        "title": "[태그] 행사/대회 이름",
        "start_time": "YYYY-MM-DD HH:MM:SS",
        "end_time": "YYYY-MM-DD HH:MM:SS",
        "location": "장소",
        "description": "해당 세부 일정에 대한 간단한 요약"
    }
]
텍스트에 명시되지 않은 일정은 추측하지 말고 제외해라.
날짜가 불완전해서 YYYY-MM-DD HH:MM:SS 형식으로 확정할 수 없는 항목도 제외해라.
""".strip()


def build_schedule_generation_prompt() -> str:
    """Gemini 일정 생성에 현재 한국 시간과 날짜 규칙을 주입합니다."""
    return f"""
{get_current_time_context()}
위 제공된 '현재 시간'을 기준으로 날짜를 계산해라. 본문에 연도가 생략되어 있다면 무조건 현재 연도를 사용하고, 절대로 지나간 과거 연도로 작성하지 마라.

{SCHEDULE_GENERATION_PROMPT}
""".strip()


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


class ScheduleDeleteSelect(discord.ui.Select):
    """등록된 일정 목록을 드롭다운으로 보여주고 선택된 일정을 삭제합니다."""

    def __init__(self, cog: "ScheduleCog", rows: list) -> None:
        self.cog = cog
        options: list[discord.SelectOption] = []

        for row in rows:
            starts_at = from_storage_iso(row["starts_at"], cog.bot.settings.timezone)
            label = str(row["title"])[:100]
            description = starts_at.strftime("%Y-%m-%d %H:%M")
            options.append(
                discord.SelectOption(
                    label=label,
                    description=description,
                    value=str(row["id"]),
                )
            )

        super().__init__(
            placeholder="삭제할 일정을 선택하세요.",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        """선택한 schedule id로 DB 레코드를 삭제하고 View를 제거합니다."""
        if interaction.guild is None:
            await interaction.response.edit_message(content="서버 안에서만 일정을 삭제할 수 있습니다.", embed=None, view=None)
            return

        schedule_id = int(self.values[0])
        row = await self.cog.bot.database.fetch_one(
            """
            SELECT * FROM schedules
            WHERE id = ? AND guild_id = ?
            """,
            (schedule_id, interaction.guild.id),
        )
        if row is None:
            await interaction.response.edit_message(content="이미 삭제되었거나 찾을 수 없는 일정입니다.", embed=None, view=None)
            return

        title = str(row["title"])
        event_notice = await self.cog.delete_linked_server_event(interaction.guild, row["event_id"])
        await self.cog.bot.database.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))

        message = f"✅ {title} 일정이 성공적으로 삭제되었습니다."
        if event_notice:
            message += f"\n{event_notice}"
        await interaction.response.edit_message(content=message, embed=None, view=None)


class ScheduleDeleteView(discord.ui.View):
    """일정 삭제 Select를 담는 Ephemeral View입니다."""

    def __init__(self, cog: "ScheduleCog", rows: list, user_id: int) -> None:
        super().__init__(timeout=120)
        self.user_id = user_id
        self.add_item(ScheduleDeleteSelect(cog, rows))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.user_id:
            return True
        await interaction.response.send_message("이 일정 삭제 메뉴는 명령어를 실행한 사람만 사용할 수 있습니다.", ephemeral=True)
        return False


class ScheduleEditModal(discord.ui.Modal, title="일정 수정"):
    """기존 일정 값을 채운 상태로 열리는 수정 Modal입니다."""

    def __init__(self, cog: "ScheduleCog", row, source_message: discord.Message | None, user_id: int) -> None:
        super().__init__()
        self.cog = cog
        self.schedule_id = int(row["id"])
        self.event_id = row["event_id"]
        self.source_message = source_message
        self.user_id = user_id

        starts_at = from_storage_iso(row["starts_at"], cog.bot.settings.timezone)
        self.title_input = discord.ui.TextInput(
            label="제목",
            placeholder="예: Team 0x34 정기 회의",
            default=str(row["title"])[:100],
            max_length=100,
        )
        self.starts_at_input = discord.ui.TextInput(
            label="날짜/시간",
            placeholder="예: 2026-07-01 19:00",
            default=starts_at.strftime("%Y-%m-%d %H:%M"),
            max_length=40,
        )
        self.body_input = discord.ui.TextInput(
            label="내용",
            placeholder="회의 안건, 준비물, 장소 등을 적어 주세요.",
            default=str(row["body"])[:1000],
            style=discord.TextStyle.long,
            max_length=1000,
        )
        self.add_item(self.title_input)
        self.add_item(self.starts_at_input)
        self.add_item(self.body_input)

    async def disable_source_view(self) -> None:
        if self.source_message is None:
            return
        try:
            await self.source_message.edit(view=None)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("서버 안에서만 일정을 수정할 수 있습니다.", ephemeral=True)
            return
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("이 일정 수정 창은 명령어를 실행한 사람만 제출할 수 있습니다.", ephemeral=True)
            return

        try:
            starts_at = parse_datetime(str(self.starts_at_input.value), self.cog.bot.settings.timezone)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        row = await self.cog.bot.database.fetch_one(
            """
            SELECT * FROM schedules
            WHERE id = ? AND guild_id = ?
            """,
            (self.schedule_id, interaction.guild.id),
        )
        if row is None:
            await self.disable_source_view()
            await interaction.followup.send("수정할 일정을 찾을 수 없습니다.", ephemeral=True)
            return

        title = str(self.title_input.value).strip()
        body = str(self.body_input.value).strip()
        await self.cog.bot.database.execute(
            """
            UPDATE schedules
            SET title = ?, starts_at = ?, body = ?
            WHERE id = ? AND guild_id = ?
            """,
            (title, to_storage_iso(starts_at), body, self.schedule_id, interaction.guild.id),
        )

        event_notice = await self.cog.edit_linked_server_event(interaction.guild, row["event_id"], title, starts_at, body)
        await self.disable_source_view()

        message = "✅ 성공적으로 수정되었습니다."
        if event_notice:
            message += f"\n{event_notice}"
        await interaction.followup.send(message, ephemeral=True)


class ScheduleEditSelect(discord.ui.Select):
    """수정할 일정을 선택하는 드롭다운입니다."""

    def __init__(self, cog: "ScheduleCog", rows: list) -> None:
        self.cog = cog
        options: list[discord.SelectOption] = []
        for row in rows:
            starts_at = from_storage_iso(row["starts_at"], cog.bot.settings.timezone)
            options.append(
                discord.SelectOption(
                    label=str(row["title"])[:100],
                    description=starts_at.strftime("%Y-%m-%d %H:%M"),
                    value=str(row["id"]),
                )
            )

        super().__init__(
            placeholder="수정할 일정을 선택하세요.",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.edit_message(content="서버 안에서만 일정을 수정할 수 있습니다.", embed=None, view=None)
            return

        schedule_id = int(self.values[0])
        row = await self.cog.bot.database.fetch_one(
            """
            SELECT * FROM schedules
            WHERE id = ? AND guild_id = ?
            """,
            (schedule_id, interaction.guild.id),
        )
        if row is None:
            await interaction.response.edit_message(content="이미 삭제되었거나 찾을 수 없는 일정입니다.", embed=None, view=None)
            return

        await interaction.response.send_modal(ScheduleEditModal(self.cog, row, interaction.message, interaction.user.id))


class ScheduleEditView(discord.ui.View):
    """일정 수정 Select를 담는 Ephemeral View입니다."""

    def __init__(self, cog: "ScheduleCog", rows: list, user_id: int) -> None:
        super().__init__(timeout=120)
        self.user_id = user_id
        self.add_item(ScheduleEditSelect(cog, rows))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.user_id:
            return True
        await interaction.response.send_message("이 일정 수정 메뉴는 명령어를 실행한 사람만 사용할 수 있습니다.", ephemeral=True)
        return False


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
        *,
        end_time=None,
        location: str = "Discord",
    ) -> tuple[int | None, str]:
        """Discord 서버 이벤트 API를 호출합니다. 권한이 없으면 실패 메시지만 돌려줍니다."""
        try:
            event = await guild.create_scheduled_event(
                name=title,
                start_time=starts_at,
                end_time=end_time or starts_at + timedelta(hours=1),
                description=description[:1000],
                entity_type=discord.EntityType.external,
                privacy_level=discord.PrivacyLevel.guild_only,
                location=(location or "Discord")[:100],
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

    async def delete_linked_server_event(self, guild: discord.Guild, event_id) -> str | None:
        """DB 일정과 연결된 Discord 서버 이벤트가 있으면 함께 삭제합니다."""
        if event_id is None:
            return None

        try:
            event = await guild.fetch_scheduled_event(int(event_id))
            await event.delete(reason="Team 0x34 일정 삭제")
        except (TypeError, ValueError):
            return "저장된 서버 이벤트 ID가 올바르지 않아 Discord 이벤트는 삭제하지 못했습니다."
        except discord.NotFound:
            return "연결된 서버 이벤트는 이미 삭제되어 있었습니다."
        except discord.Forbidden:
            return "서버 이벤트 삭제 권한이 없어 Discord 이벤트는 삭제하지 못했습니다."
        except discord.HTTPException as exc:
            return f"서버 이벤트 삭제 중 오류가 발생했습니다: {exc.text}"
        return "연결된 Discord 서버 이벤트도 함께 삭제했습니다."

    async def edit_linked_server_event(self, guild: discord.Guild, event_id, title: str, starts_at, body: str) -> str | None:
        """DB 일정과 연결된 Discord 서버 이벤트가 있으면 수정 내용도 반영합니다."""
        if event_id is None:
            return None

        try:
            event = await guild.fetch_scheduled_event(int(event_id))
            await event.edit(
                name=title[:100],
                start_time=starts_at,
                end_time=starts_at + timedelta(hours=1),
                description=body[:1000],
                reason="Team 0x34 일정 수정",
            )
        except (TypeError, ValueError):
            return "저장된 서버 이벤트 ID가 올바르지 않아 Discord 이벤트는 수정하지 못했습니다."
        except discord.NotFound:
            return "연결된 서버 이벤트를 찾을 수 없어 Discord 이벤트는 수정하지 못했습니다."
        except discord.Forbidden:
            return "서버 이벤트 수정 권한이 없어 Discord 이벤트는 수정하지 못했습니다."
        except discord.HTTPException as exc:
            return f"서버 이벤트 수정 중 오류가 발생했습니다: {exc.text}"
        return "연결된 Discord 서버 이벤트도 함께 수정했습니다."

    def parse_generated_schedules(self, raw_text: str) -> list[dict]:
        """Gemini가 반환한 JSON 배열을 Python 리스트로 변환합니다."""
        text = raw_text.strip()
        if text.startswith("```"):
            text = text.strip("`")
            text = text.removeprefix("json").strip()

        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            array_match = text[text.find("[") : text.rfind("]") + 1]
            try:
                payload = json.loads(array_match)
            except json.JSONDecodeError:
                return []

        if not isinstance(payload, list):
            return []
        return [item for item in payload if isinstance(item, dict)]

    def generate_schedule_json_sync(self, source_text: str) -> str:
        """Gemini 동기 SDK 호출을 별도 스레드에서 실행하기 위한 함수입니다."""
        if self.bot.settings.gemini_api_key is None:
            raise RuntimeError("GEMINI_API_KEY가 설정되어 있지 않습니다. .env 또는 Railway Variables에 추가해 주세요.")

        genai.configure(api_key=self.bot.settings.gemini_api_key)
        model = genai.GenerativeModel(
            model_name=self.bot.settings.gemini_model,
            system_instruction=build_schedule_generation_prompt(),
        )
        response = model.generate_content(
            "다음 대회/행사 안내 텍스트에서 등록할 수 있는 모든 일정을 JSON 배열로 추출해 주세요.\n\n"
            f"{source_text}",
            generation_config={
                "temperature": 0.2,
                "response_mime_type": "application/json",
            },
        )
        return str(getattr(response, "text", "") or "")

    async def generate_schedule_items(self, source_text: str) -> list[dict]:
        """Gemini 호출을 이벤트 루프 밖 스레드로 넘기고 JSON 배열을 파싱합니다."""
        raw_text = await asyncio.to_thread(self.generate_schedule_json_sync, source_text)
        return self.parse_generated_schedules(raw_text)

    async def insert_generated_schedule(
        self,
        interaction: discord.Interaction,
        item: dict,
    ) -> tuple[str, object] | None:
        """Gemini JSON 항목 하나를 검증한 뒤 DB에 등록합니다. 실패한 항목은 None으로 건너뜁니다."""
        if interaction.guild is None:
            return None

        try:
            title = str(item["title"]).strip()
            start_time_text = str(item["start_time"]).strip()
            end_time_text = str(item["end_time"]).strip()
            location = str(item.get("location") or "Discord").strip()
            description = str(item["description"]).strip()
            if not title or not start_time_text or not end_time_text or not description:
                return None

            starts_at = fix_past_year(parse_datetime(start_time_text, self.bot.settings.timezone))
            ends_at = fix_past_year(parse_datetime(end_time_text, self.bot.settings.timezone))
        except (KeyError, TypeError, ValueError) as exc:
            logging.info("Skipping invalid generated schedule item %s: %s", item, exc)
            return None

        body = (
            f"**종료** {format_discord_timestamp(ends_at)}\n"
            f"**장소** {location or '공개된 정보 없음'}\n"
            f"{description}"
        )
        cursor = await self.bot.database.execute(
            """
            INSERT INTO schedules (guild_id, title, starts_at, body, created_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                interaction.guild.id,
                title[:100],
                to_storage_iso(starts_at),
                body[:1000],
                interaction.user.id,
                now_utc_iso(),
            ),
        )

        if self.bot.settings.enable_server_events:
            event_id, _ = await self.create_server_event(
                interaction.guild,
                title[:100],
                starts_at,
                description,
                end_time=ends_at,
                location=location or "Discord",
            )
            if event_id is not None:
                await self.bot.database.execute(
                    "UPDATE schedules SET event_id = ? WHERE id = ?",
                    (event_id, cursor.lastrowid),
                )

        return title[:100], starts_at

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

    @app_commands.command(name="일정삭제", description="드롭다운 메뉴로 등록된 일정을 삭제합니다.")
    async def delete_schedule(self, interaction: discord.Interaction) -> None:
        """등록된 일정 최대 25개를 Select Menu로 보여주고 선택한 일정을 삭제합니다."""
        if interaction.guild is None:
            await interaction.response.send_message("서버 안에서만 일정을 삭제할 수 있습니다.", ephemeral=True)
            return

        rows = await self.bot.database.fetch_all(
            """
            SELECT id, title, starts_at, event_id FROM schedules
            WHERE guild_id = ?
            ORDER BY starts_at ASC
            LIMIT 25
            """,
            (interaction.guild.id,),
        )

        if not rows:
            await interaction.response.send_message("등록된 일정이 없습니다.", ephemeral=True)
            return

        embed = base_embed(
            "삭제할 일정을 선택하세요",
            "드롭다운에는 가까운 일정부터 최대 25개까지 표시됩니다.",
            color=WARNING_COLOR,
        )
        await interaction.response.send_message(embed=embed, view=ScheduleDeleteView(self, rows, interaction.user.id), ephemeral=True)

    @app_commands.command(name="일정수정", description="드롭다운 메뉴와 Modal로 등록된 일정을 수정합니다.")
    async def edit_schedule(self, interaction: discord.Interaction) -> None:
        """내가 작성했거나 아직 지나지 않은 일정 최대 25개를 Select Menu로 보여줍니다."""
        if interaction.guild is None:
            await interaction.response.send_message("서버 안에서만 일정을 수정할 수 있습니다.", ephemeral=True)
            return

        rows = await self.bot.database.fetch_all(
            """
            SELECT id, title, starts_at, body, created_by, event_id FROM schedules
            WHERE guild_id = ? AND (created_by = ? OR starts_at >= ?)
            ORDER BY starts_at ASC
            LIMIT 25
            """,
            (interaction.guild.id, interaction.user.id, now_utc_iso()),
        )

        if not rows:
            await interaction.response.send_message("수정할 일정이 없습니다.", ephemeral=True)
            return

        embed = base_embed(
            "수정할 일정을 선택하세요",
            "내가 작성했거나 아직 지나지 않은 일정이 최대 25개까지 표시됩니다.",
            color=WARNING_COLOR,
        )
        await interaction.response.send_message(embed=embed, view=ScheduleEditView(self, rows, interaction.user.id), ephemeral=True)

    @app_commands.command(name="일정생성", description="Gemini로 안내 텍스트에서 여러 일정을 자동 등록합니다.")
    @app_commands.describe(target_info="참가 신청, 예선, 본선 등 일정을 추출할 대회/행사 안내 텍스트")
    async def create_schedules(self, interaction: discord.Interaction, target_info: str) -> None:
        """Gemini가 반환한 JSON 배열의 모든 일정을 검증 후 일괄 등록합니다."""
        await interaction.response.defer(ephemeral=True, thinking=True)

        if interaction.guild is None:
            await interaction.followup.send("서버 안에서만 일정을 생성할 수 있습니다.", ephemeral=True)
            return

        source_text = target_info.strip()
        if not source_text:
            await interaction.followup.send("일정을 추출할 텍스트를 입력해 주세요.", ephemeral=True)
            return

        try:
            items = await self.generate_schedule_items(source_text)
        except RuntimeError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        except Exception:
            logging.exception("Gemini schedule generation failed")
            await interaction.followup.send("Gemini API 호출 중 문제가 발생했습니다. API 키, 모델명, 할당량을 확인해 주세요.", ephemeral=True)
            return

        registered: list[tuple[str, object]] = []
        for item in items:
            result = await self.insert_generated_schedule(interaction, item)
            if result is None:
                continue
            registered.append(result)

        if not registered:
            await interaction.followup.send("Gemini 응답에서 등록 가능한 일정을 찾지 못했습니다.", ephemeral=True)
            return

        summary_lines = [f"- **{title}**: {format_discord_timestamp(starts_at)}" for title, starts_at in registered]
        embed = base_embed(
            f"✨ 총 {len(registered)}개의 일정이 자동 등록되었습니다!",
            "\n".join(summary_lines)[:4000],
            color=SUCCESS_COLOR,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    """discord.py가 이 파일을 Cog로 로드할 때 호출하는 함수입니다."""
    await bot.add_cog(ScheduleCog(bot))