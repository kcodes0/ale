"""Discord gateway entrypoint for Ale."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import signal
import time
from collections.abc import Iterable

import discord

from ale.artifacts import publish_artifact, should_send_as_artifact, write_markdown_artifact
from ale.config import Settings, load_settings
from ale.models import ChatMessage, RouteDecision
from ale.reload import FileWatchdog
from ale.reports import render_report_pdf
from ale.service import AleRuntime

LOG = logging.getLogger(__name__)


def split_for_discord(text: str, limit: int) -> Iterable[str]:
    text = text.strip()
    if not text:
        return []
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        split_at = remaining.rfind("\n", 0, limit)
        if split_at < limit // 2:
            split_at = limit
        chunks.append(remaining[:split_at].strip())
        remaining = remaining[split_at:].strip()
    if remaining:
        chunks.append(remaining)
    return chunks


class AleDiscordClient(discord.Client):
    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.default()
        intents.dm_messages = True
        intents.guild_messages = True
        intents.message_content = settings.discord_message_content_intent
        super().__init__(intents=intents)
        self.settings = settings
        self.runtime = AleRuntime(settings, discord_sender=self.send_dm)
        self._seen_message_ids: set[int] = set()
        self._watchdog: FileWatchdog | None = None
        self._sighup_handler_installed = False
        self.tree = discord.app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
        self.runtime.logger.event("discord", "setup_complete")
        LOG.info("Ale Discord client setup complete")
        if self.settings.hot_reload_enabled:
            self._watchdog = FileWatchdog(self.runtime.reload_manager, logger=self.runtime.logger)
            await self._watchdog.start()
        self._install_sighup_handler()
        self._register_slash_commands()
        await self.tree.sync()
        self.runtime.logger.event("discord", "slash_commands_synced")

    async def close(self) -> None:
        if self._watchdog is not None:
            await self._watchdog.stop()
            self._watchdog = None
        await super().close()

    def _install_sighup_handler(self) -> None:
        if self._sighup_handler_installed or not hasattr(signal, "SIGHUP"):
            return
        loop = asyncio.get_running_loop()

        def _trigger() -> None:
            self.runtime.logger.event("reload", "sighup_received")
            asyncio.create_task(self._reload_via_signal())

        try:
            loop.add_signal_handler(signal.SIGHUP, _trigger)
            self._sighup_handler_installed = True
            self.runtime.logger.event("reload", "sighup_handler_installed")
        except (NotImplementedError, RuntimeError):
            self.runtime.logger.event("reload", "sighup_handler_unavailable")

    async def _reload_via_signal(self) -> None:
        manager = self.runtime.reload_manager
        result = await manager.reload_config_and_prompts(reason="sighup")
        if result.ok and self.settings.hot_reload_tools:
            await manager.reload_tools(reason="sighup")
        if result.ok and self.settings.hot_reload_candidate:
            await manager.health_check_candidate(reason="sighup")

    async def on_ready(self) -> None:
        self.runtime.logger.event(
            "discord",
            "ready",
            bot_user=str(self.user),
            bot_user_id=getattr(self.user, "id", None),
        )
        LOG.info("Ale connected as %s (%s)", self.user, getattr(self.user, "id", "?"))

    def _register_slash_commands(self) -> None:
        """Register Discord application (slash) commands."""

        @self.tree.command(name="engineer", description="Invoke Ale's Engineer agent")
        @discord.app_commands.describe(prompt="What you want the Engineer to do")
        async def engineer(interaction: discord.Interaction, prompt: str) -> None:
            await self._handle_engineer_command(interaction, prompt)

    async def _handle_engineer_command(
        self, interaction: discord.Interaction, prompt: str
    ) -> None:
        """Handle the /engineer slash command."""
        self.runtime.logger.event(
            "discord",
            "slash_command_received",
            command="engineer",
            author_id=interaction.user.id,
            channel_id=interaction.channel_id,
            prompt_chars=len(prompt),
        )

        if self.settings.allowed_user_ids and interaction.user.id not in self.settings.allowed_user_ids:
            await interaction.response.send_message("Not authorized.", ephemeral=True)
            return

        await interaction.response.defer()
        content = prompt.strip()

        # Use router for thread continuity, then force engineer persona.
        # RouteDecision is a frozen dataclass — replace() instead of mutating.
        route = await self.runtime.router.route(
            content,
            discord_channel_id=str(interaction.channel_id),
            discord_user_id=interaction.user.id,
        )
        route = dataclasses.replace(route, persona="engineer")

        self.runtime.logger.event(
            "discord",
            "slash_command_routed",
            command="engineer",
            thread_id=route.thread_id,
            route_reason=route.reason,
            is_new=route.is_new,
        )

        async with self.runtime.threads.lock_for(route.thread_id):
            if route.is_new or not self.runtime.threads.exists(route.thread_id):
                state = self.runtime.threads.create(
                    content.splitlines()[0][:80] if content else "Engineer task",
                    thread_id=route.thread_id,
                    discord_channel_id=str(interaction.channel_id),
                    discord_user_id=interaction.user.id,
                    persona="engineer",
                )
            else:
                state = self.runtime.threads.load(route.thread_id)
                state.meta.persona = "engineer"
                self.runtime.threads.save_meta(state.meta)

            chat_message = ChatMessage(
                role="user",
                content=content,
                discord_message_id=str(interaction.id),
                author_id=interaction.user.id,
                author_name=interaction.user.display_name,
                attachments=[],
            )
            self.runtime.threads.append_message(state.meta.thread_id, chat_message)
            state = self.runtime.threads.load(state.meta.thread_id)

            progress_last_sent = 0.0
            progress_sent: set[str] = set()

            async def send_progress(update: str) -> None:
                nonlocal progress_last_sent
                if update in progress_sent:
                    return
                now = time.monotonic()
                if progress_last_sent and (
                    now - progress_last_sent < self.settings.specialist_progress_min_seconds
                ):
                    return
                progress_last_sent = now
                progress_sent.add(update)
                await interaction.followup.send(update)
                self.runtime.logger.event(
                    "discord",
                    "progress_sent",
                    thread_id=state.meta.thread_id,
                    persona="engineer",
                    update=update,
                )

            try:
                response = await self.runtime.agent.respond(
                    state=state,
                    user_message=chat_message,
                    route=route,
                    progress_callback=send_progress,
                )
            except Exception as exc:
                await interaction.followup.send(
                    "I hit an internal error while running the Engineer."
                )
                self.runtime.logger.event(
                    "discord",
                    "slash_command_failed",
                    command="engineer",
                    thread_id=state.meta.thread_id,
                    error_type=type(exc).__name__,
                )
                LOG.exception("Engineer slash command failed")
                return

            if response.text and response.text != "NO_REPLY":
                for chunk in split_for_discord(
                    response.text, self.settings.discord_reply_limit
                ):
                    await interaction.followup.send(chunk)
                    self.runtime.logger.event(
                        "discord",
                        "message_sent",
                        thread_id=state.meta.thread_id,
                        chunk_chars=len(chunk),
                    )
                self.runtime.threads.append_message(
                    state.meta.thread_id,
                    ChatMessage(role="assistant", content=response.text),
                )
            else:
                await interaction.followup.send("Done — nothing to report.")
                self.runtime.logger.event(
                    "discord", "no_reply", thread_id=state.meta.thread_id
                )

        asyncio.create_task(self.runtime.refresh_if_needed(route.thread_id))
        self.runtime.logger.event(
            "discord", "recap_refresh_scheduled", thread_id=route.thread_id
        )

    async def send_dm(self, user_id: int, message: str) -> None:
        self.runtime.logger.event("discord", "dm_started", user_id=user_id, chars=len(message))
        user = await self.fetch_user(user_id)
        for chunk in split_for_discord(message, self.settings.discord_reply_limit):
            await user.send(chunk)
            self.runtime.logger.event(
                "discord", "dm_chunk_sent", user_id=user_id, chunk_chars=len(chunk)
            )

    async def on_message(self, message: discord.Message) -> None:
        self.runtime.logger.event(
            "discord",
            "message_received",
            discord_message_id=str(message.id),
            author_id=getattr(message.author, "id", None),
            channel_id=getattr(message.channel, "id", None),
            author_is_bot=message.author.bot,
            content_chars=len(message.content or ""),
            attachment_count=len(message.attachments),
        )
        if message.author.bot:
            self.runtime.logger.event(
                "discord", "message_ignored_bot", discord_message_id=str(message.id)
            )
            return
        if message.id in self._seen_message_ids:
            self.runtime.logger.event(
                "discord", "message_ignored_duplicate", discord_message_id=str(message.id)
            )
            return
        self._seen_message_ids.add(message.id)
        if len(self._seen_message_ids) > 5000:
            self._seen_message_ids = set(list(self._seen_message_ids)[-1000:])

        if self.settings.allowed_user_ids and message.author.id not in self.settings.allowed_user_ids:
            self.runtime.logger.event(
                "discord",
                "message_ignored_not_allowed",
                discord_message_id=str(message.id),
                author_id=message.author.id,
            )
            LOG.warning("Ignoring message from non-allowed user_id=%s", message.author.id)
            return

        content = message.content.strip()
        if not content and not message.attachments:
            self.runtime.logger.event(
                "discord", "message_ignored_empty", discord_message_id=str(message.id)
            )
            return

        route = await self.runtime.router.route(
            content,
            discord_channel_id=str(message.channel.id),
            discord_user_id=message.author.id,
        )
        self.runtime.logger.event(
            "discord",
            "message_routed",
            discord_message_id=str(message.id),
            thread_id=route.thread_id,
            persona=route.persona,
            route_reason=route.reason,
            route_confidence=route.confidence,
            is_new=route.is_new,
        )
        async with self.runtime.threads.lock_for(route.thread_id):
            if route.is_new or not self.runtime.threads.exists(route.thread_id):
                state = self.runtime.threads.create(
                    content.splitlines()[0][:80] if content else "Attachment",
                    thread_id=route.thread_id,
                    discord_channel_id=str(message.channel.id),
                    discord_user_id=message.author.id,
                    persona=route.persona,
                )
            else:
                state = self.runtime.threads.load(route.thread_id)
                state.meta.persona = route.persona
                self.runtime.threads.save_meta(state.meta)

            chat_message = ChatMessage(
                role="user",
                content=content,
                discord_message_id=str(message.id),
                author_id=message.author.id,
                author_name=message.author.display_name,
                attachments=[
                    {"filename": attachment.filename, "url": attachment.url}
                    for attachment in message.attachments
                ],
            )
            self.runtime.threads.append_message(state.meta.thread_id, chat_message)
            state = self.runtime.threads.load(state.meta.thread_id)
            progress_last_sent = 0.0
            progress_sent: set[str] = set()

            async def send_progress(update: str) -> None:
                nonlocal progress_last_sent
                if update in progress_sent:
                    return
                now = time.monotonic()
                if progress_last_sent and (
                    now - progress_last_sent < self.settings.specialist_progress_min_seconds
                ):
                    return
                progress_last_sent = now
                progress_sent.add(update)
                await message.channel.send(update)
                self.runtime.logger.event(
                    "discord",
                    "progress_sent",
                    thread_id=state.meta.thread_id,
                    persona=route.persona,
                    update=update,
                )

            async with message.channel.typing():
                try:
                    response = await self.runtime.agent.respond(
                        state=state,
                        user_message=chat_message,
                        route=route,
                        progress_callback=send_progress,
                    )
                except Exception as exc:
                    error_message = "I hit an internal error. I’m asking Engineer to inspect it."
                    self.runtime.logger.event(
                        "discord",
                        "response_failed",
                        phase="discord.agent_response",
                        recovery="engineer_incident_handoff",
                        discord_message_id=str(message.id),
                        thread_id=state.meta.thread_id,
                    )
                    LOG.exception("Ale failed to respond")
                    await message.channel.send(error_message)
                    asyncio.create_task(
                        self._handoff_error_to_engineer(
                            original_message=message,
                            thread_id=state.meta.thread_id,
                            user_content=content,
                            error=exc,
                        )
                    )
                    return

            if response.text != "NO_REPLY":
                if should_send_as_artifact(
                    response.text, route.persona, self.settings.discord_artifact_threshold
                ):
                    await self._send_long_specialist_response(
                        message=message,
                        response_text=response.text,
                        route=route,
                        state=state,
                        original_request=content,
                        parent_turn_id=response.turn_id,
                    )
                else:
                    for chunk in split_for_discord(response.text, self.settings.discord_reply_limit):
                        await message.channel.send(chunk)
                        self.runtime.logger.event(
                            "discord",
                            "message_sent",
                            thread_id=state.meta.thread_id,
                            chunk_chars=len(chunk),
                        )
                self.runtime.threads.append_message(
                    state.meta.thread_id,
                    ChatMessage(role="assistant", content=response.text),
                )
            else:
                self.runtime.logger.event(
                    "discord", "no_reply", thread_id=state.meta.thread_id
                )

        asyncio.create_task(self.runtime.refresh_if_needed(route.thread_id))
        self.runtime.logger.event("discord", "recap_refresh_scheduled", thread_id=route.thread_id)

    async def _send_long_specialist_response(
        self,
        *,
        message: discord.Message,
        response_text: str,
        route: RouteDecision,
        state,
        original_request: str,
        parent_turn_id: str | None,
    ) -> None:
        """Long Engineer/Linguist replies → PDF + Actor summary in Discord.

        The full text always lands as a markdown artifact (auditable, diffable)
        and as a PDF (user-friendly). The Discord-visible message is a short
        Actor-voiced summary so the user gets a human read instead of a wall
        of technical text.
        """

        artifact_path = write_markdown_artifact(
            artifacts_dir=self.settings.artifacts_dir,
            thread_id=state.meta.thread_id,
            discord_message_id=str(message.id),
            title=state.meta.title,
            content=response_text,
        )

        pdf_path = None
        if self.settings.enable_pdf_artifacts:
            try:
                pdf_path = artifact_path.with_suffix(".pdf")
                render_result = render_report_pdf(
                    title=f"{route.persona.title()} report — {state.meta.title}",
                    body=response_text,
                    output_path=pdf_path,
                    metadata={
                        "thread_id": state.meta.thread_id,
                        "discord_message_id": str(message.id),
                        "persona": route.persona,
                    },
                )
                self.runtime.logger.event(
                    "discord",
                    "pdf_rendered",
                    thread_id=state.meta.thread_id,
                    persona=route.persona,
                    pdf_path=str(pdf_path),
                    page_count=render_result.page_count,
                    bytes_written=render_result.bytes_written,
                )
            except Exception as exc:
                self.runtime.logger.exception(
                    "discord",
                    "pdf_render_failed",
                    exc,
                    thread_id=state.meta.thread_id,
                    persona=route.persona,
                )
                pdf_path = None

        summary = await self.runtime.agent.summarize_for_discord(
            source_persona=route.persona,
            full_text=response_text,
            thread_id=state.meta.thread_id,
            original_request=original_request,
            parent_turn_id=parent_turn_id,
        )

        publish_result = await publish_artifact(
            command_template=self.settings.artifact_publish_command,
            artifact_path=artifact_path,
            timeout_seconds=self.settings.artifact_publish_timeout_seconds,
        )

        attachments: list[discord.File] = []
        if pdf_path and pdf_path.exists():
            attachments.append(discord.File(pdf_path))
        elif not (publish_result and publish_result.url):
            attachments.append(discord.File(artifact_path))

        body = summary.strip()
        if publish_result and publish_result.url:
            body = f"{body}\n\nFull report: {publish_result.url}"
        elif pdf_path and pdf_path.exists():
            body = f"{body}\n\nFull report attached as `{pdf_path.name}`."
        else:
            body = f"{body}\n\nFull report attached as `{artifact_path.name}`."
        body = body[: self.settings.discord_reply_limit]

        await message.channel.send(body, files=attachments or None)
        self.runtime.logger.event(
            "discord",
            "specialist_summary_sent",
            thread_id=state.meta.thread_id,
            persona=route.persona,
            md_path=str(artifact_path),
            pdf_path=str(pdf_path) if pdf_path else None,
            publish_url=publish_result.url if publish_result else None,
            publish_exit_code=publish_result.exit_code if publish_result else None,
            response_chars=len(response_text),
            summary_chars=len(summary),
        )

    async def _handoff_error_to_engineer(
        self,
        *,
        original_message: discord.Message,
        thread_id: str,
        user_content: str,
        error: BaseException,
    ) -> None:
        incident_title = f"Internal error while handling {thread_id}"
        try:
            workspace_id = self.runtime.workspaces.create(
                incident_title,
                kind="engineer",
                lead="Engineer",
            )
            self.runtime.logger.team(
                "incident_handoff_started",
                team="engineer",
                workspace_id=workspace_id,
                origin_thread_id=thread_id,
                discord_message_id=str(original_message.id),
                error_type=type(error).__name__,
                error=str(error),
            )
            self.runtime.workspaces.write_file(
                workspace_id,
                "incident.md",
                "\n".join(
                    [
                        "# Ale Internal Incident",
                        "",
                        f"- Discord message id: {original_message.id}",
                        f"- Original thread id: {thread_id}",
                        f"- Channel id: {original_message.channel.id}",
                        f"- Author id: {original_message.author.id}",
                        f"- Error type: {type(error).__name__}",
                        f"- Error: {error}",
                        "",
                        "## User Message",
                        "",
                        user_content,
                        "",
                        "## Task",
                        "",
                        "Review Ale's logs and code paths for this incident. "
                        "Identify the likely root cause and propose a minimal fix. Do not edit files.",
                    ]
                ),
            )
            incident_thread_id = "internal-engineer-incidents"
            async with self.runtime.threads.lock_for(incident_thread_id):
                if not self.runtime.threads.exists(incident_thread_id):
                    state = self.runtime.threads.create(
                        "Internal Engineer Incidents",
                        thread_id=incident_thread_id,
                        discord_channel_id=str(original_message.channel.id),
                        discord_user_id=original_message.author.id,
                        persona="engineer",
                    )
                else:
                    state = self.runtime.threads.load(incident_thread_id)
                prompt = (
                    f"Engineer incident handoff. Workspace: {workspace_id}. "
                    f"Original thread: {thread_id}. Review the incident file and relevant logs. "
                    "Return a concise diagnosis and a safe next step. Do not edit files."
                )
                engineer_message = ChatMessage(
                    role="user",
                    content=prompt,
                    discord_message_id=str(original_message.id),
                    author_id=original_message.author.id,
                    author_name="Ale internal recovery",
                )
                self.runtime.threads.append_message(incident_thread_id, engineer_message)
                state = self.runtime.threads.load(incident_thread_id)
                response = await self.runtime.agent.respond(
                    state=state,
                    user_message=engineer_message,
                    route=RouteDecision(
                        thread_id=incident_thread_id,
                        persona="engineer",
                        confidence=1.0,
                        reason="internal error recovery",
                        is_new=False,
                    ),
                )
                if response.text and response.text != "NO_REPLY":
                    await original_message.channel.send(
                        f"Engineer reviewed the incident:\n{response.text[:1600]}"
                    )
                    self.runtime.threads.append_message(
                        incident_thread_id,
                        ChatMessage(role="assistant", content=response.text),
                    )
                self.runtime.logger.team(
                    "incident_handoff_completed",
                    team="engineer",
                    workspace_id=workspace_id,
                    origin_thread_id=thread_id,
                    discord_message_id=str(original_message.id),
                    response_chars=len(response.text or ""),
                )
        except Exception as exc:
            self.runtime.logger.exception(
                "discord",
                "engineer_incident_handoff_failed",
                exc,
                phase="discord.engineer_incident_handoff",
                original_thread_id=thread_id,
                discord_message_id=str(original_message.id),
            )


def run() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = load_settings()
    if not settings.discord_token:
        raise RuntimeError("Set DISCORD_TOKEN or DISCORD_TOKEN_FILE before starting Ale.")
    client = AleDiscordClient(settings)
    client.run(settings.discord_token)
