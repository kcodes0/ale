"""Service wiring for Ale."""

from __future__ import annotations

from ale.agent import AleAgent, maybe_refresh_recap
from ale.config import Settings
from ale.memory import MemoryStore
from ale.observability import EventLogger, configure_python_logging
from ale.reload import ReloadManager
from ale.router import Router
from ale.threads import ThreadStore
from ale.workspace import WorkspaceStore


class AleRuntime:
    def __init__(self, settings: Settings, discord_sender=None) -> None:
        self.settings = settings
        settings.state_dir.mkdir(parents=True, exist_ok=True)
        settings.logs_dir.mkdir(parents=True, exist_ok=True)
        self.logger = EventLogger(settings.logs_dir)
        configure_python_logging(self.logger)
        self.logger.event(
            "core",
            "runtime_init",
            state_dir=str(settings.state_dir),
            work_dir=str(settings.work_dir),
            memory_dir=str(settings.memory_dir),
            hot_reload_enabled=settings.hot_reload_enabled,
            hot_reload_tools=settings.hot_reload_tools,
            hot_reload_candidate=settings.hot_reload_candidate,
            enable_reload_tools=settings.enable_reload_tools,
        )
        self.reload_manager = ReloadManager(settings, logger=self.logger)
        self.threads = ThreadStore(
            settings.threads_dir,
            max_recent_turns=settings.max_recent_turns,
            logger=self.logger,
        )
        self.memory = MemoryStore(settings.memory_dir, logger=self.logger)
        self.workspaces = WorkspaceStore(settings.workspaces_dir, logger=self.logger)
        self.router = Router(settings, self.threads, logger=self.logger)
        self.agent = AleAgent(
            reload_manager=self.reload_manager,
            threads=self.threads,
            memory=self.memory,
            workspaces=self.workspaces,
            logger=self.logger,
            discord_sender=discord_sender,
        )
        self.reload_manager.add_listener(self._on_reload)

    async def refresh_if_needed(self, thread_id: str) -> None:
        self.logger.event("core", "recap_refresh_check", thread_id=thread_id)
        state = self.threads.load(thread_id)
        await maybe_refresh_recap(
            route_refresh=self.router.refresh_recap,
            settings=self.settings,
            state=state,
        )

    def _on_reload(self, result) -> None:
        # Settings refs held by Router/AleRuntime are best-effort updated so
        # subsystems that read self.settings see the new snapshot too. Subsystems
        # that read through reload_manager.settings already see it.
        if result.ok and not result.rolled_back:
            self.settings = self.reload_manager.settings
            self.router.settings = self.reload_manager.settings
        self.logger.event(
            "core",
            "reload_observed",
            reload_id=result.reload_id,
            tier=result.tier,
            ok=result.ok,
            rolled_back=result.rolled_back,
            duration_ms=result.duration_ms,
        )
