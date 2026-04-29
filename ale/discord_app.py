"""Discord gateway entrypoint for Ale."""

from __future__ import annotations

import asyncio
import logging
import signal
import time
from collections.abc import Iterable

import discord

from ale.artifacts import publish_artifact, should_send_as_artifact, write_markdown_artifact
from ale.config import Settings, load_settings
from ale.models import ChatMessage, RouteDecision
from ale.reload import FileWatchdog
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

    async def setup_hook(self) -> None:
        self.runtime.logger.event("discord", "setup_complete")
        LOG.info("Ale Discord client setup complete")
        if self.settings.hot_reload_enabled:
            self._watchdog = FileWatchdog(self.runtime.reload_manager, logger=self.runtime.logger)
            await self._watchdog.start()
        self._install_sighup_handler()

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
                    artifact_path = write_markdown_artifact(
                        artifacts_dir=self.settings.artifacts_dir,
                        thread_id=state.meta.thread_id,
                        discord_message_id=str(message.id),
                        title=state.meta.title,
                        content=response.text,
                    )
                    publish_result = await publish_artifact(
                        command_template=self.settings.artifact_publish_command,
                        artifact_path=artifact_path,
                        timeout_seconds=self.settings.artifact_publish_timeout_seconds,
                    )
                    if publish_result and publish_result.url:
                        await message.channel.send(
                            f"{route.persona.title()} finished. Full artifact: {publish_result.url}"
                        )
                        self.runtime.logger.event(
                            "discord",
                            "artifact_link_sent",
                            thread_id=state.meta.thread_id,
                            path=str(artifact_path),
                            url=publish_result.url,
                            publish_exit_code=publish_result.exit_code,
                            response_chars=len(response.text),
                        )
                    else:
                        summary = (
                            f"{route.persona.title()} finished. I attached the full Markdown artifact "
                            f"and kept the Discord thread readable."
                        )
                        await message.channel.send(summary, file=discord.File(artifact_path))
                        self.runtime.logger.event(
                            "discord",
                            "artifact_sent",
                            thread_id=state.meta.thread_id,
                            path=str(artifact_path),
                            publish_attempted=bool(publish_result),
                            publish_exit_code=publish_result.exit_code if publish_result else None,
                            response_chars=len(response.text),
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
