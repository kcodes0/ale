"""Staged hot reload for Ale.

The harness is allowed to edit its own files only through staged, reversible
swaps. Three tiers, in increasing risk:

Tier 1 — config + prompt reload (live)
    Re-read .env into a fresh ``Settings`` and re-import ``ale.personas`` /
    ``ale.agent`` so the BASE_SYSTEM and persona prompts visible to the next
    SDK call reflect the file on disk. In-flight turns keep the snapshot they
    started with.

Tier 2 — tool-module reload (live)
    Re-import ``ale.tools`` and any auxiliary tool modules so ``build_ale_mcp_server``
    returns a fresh server on the next agent call. The MCP surface for in-flight
    turns is unaffected.

Tier 3 — candidate process + health check + promote/rollback
    Spawn ``ale health-check`` in a subprocess so the new code path runs end to
    end (settings load, persona import, tool server build). On success, the
    candidate may be promoted via ``os.execvp`` (gated by ALE_ENABLE_SELF_PROMOTE).
    On failure the parent stays on the last known-good snapshot.

Every tier records a :class:`ReloadResult` and exposes the last reload outcome
so Engineer can read it through the ``reload_plan`` MCP tool without taking any
action.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable

from ale.config import Settings, load_settings
from ale.observability import EventLogger


ReloadTier = str  # "config" | "tools" | "candidate"


@dataclass(frozen=True)
class ReloadResult:
    reload_id: str
    tier: ReloadTier
    ok: bool
    started_at: float
    finished_at: float
    reason: str = ""
    error: str | None = None
    error_type: str | None = None
    changed_files: tuple[str, ...] = ()
    rolled_back: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def duration_ms(self) -> int:
        return int((self.finished_at - self.started_at) * 1000)


@dataclass
class ReloadSnapshot:
    """An immutable point-in-time view of swappable harness state."""

    settings: Settings
    base_system_prompt: str
    persona_prompts: dict[str, str]
    sdk_subagents_factory: Callable[[], dict[str, object]]
    tools_module_id: int
    captured_at: float


def _scan_files(roots: Iterable[Path]) -> dict[str, float]:
    fingerprints: dict[str, float] = {}
    for root in roots:
        if not root.exists():
            continue
        if root.is_file():
            try:
                fingerprints[str(root.resolve())] = root.stat().st_mtime
            except OSError:
                continue
            continue
        for path in root.rglob("*.py"):
            if any(part in {"__pycache__", ".venv", ".pytest_cache"} for part in path.parts):
                continue
            try:
                fingerprints[str(path.resolve())] = path.stat().st_mtime
            except OSError:
                continue
    return fingerprints


class ReloadManager:
    """Owns the swappable harness snapshot and runs staged reloads.

    The :class:`AleAgent` and :class:`AleDiscordClient` always read settings,
    prompts, and the MCP tool server through this manager so a successful Tier
    1/Tier 2 reload becomes visible on the next turn without restart.
    """

    def __init__(self, settings: Settings, *, logger: EventLogger) -> None:
        self._lock = asyncio.Lock()
        self.logger = logger
        self._settings = settings
        self._listeners: list[Callable[[ReloadResult], Awaitable[None] | None]] = []
        self._in_flight_turns = 0
        self._turn_idle = asyncio.Event()
        self._turn_idle.set()
        self._snapshot = self._capture_snapshot(settings, reload_prompts=False)
        self._previous_snapshot: ReloadSnapshot | None = None
        self._history: list[ReloadResult] = []
        self._max_history = 64
        self._initial_fingerprints = _scan_files(self._watch_roots())

    # ------------------------------------------------------------------ public

    @property
    def settings(self) -> Settings:
        return self._snapshot.settings

    @property
    def snapshot(self) -> ReloadSnapshot:
        return self._snapshot

    @property
    def history(self) -> tuple[ReloadResult, ...]:
        return tuple(self._history)

    @property
    def last_result(self) -> ReloadResult | None:
        return self._history[-1] if self._history else None

    def add_listener(self, listener: Callable[[ReloadResult], Awaitable[None] | None]) -> None:
        self._listeners.append(listener)

    def turn_started(self) -> None:
        """Mark an agent turn as in flight so reloads defer until it ends.

        Reloading ``ale.tools`` or ``ale.personas`` while ``AleAgent.respond``
        is mid-iteration corrupts the closures and module globals the running
        coroutine depends on, and the bundled Claude CLI subprocess exits 1 a
        few tools later. Holding turns in a counter and gating reloads behind
        ``await_turns_idle`` removes the race.
        """

        self._in_flight_turns += 1
        if self._in_flight_turns == 1:
            self._turn_idle.clear()

    def turn_ended(self) -> None:
        if self._in_flight_turns > 0:
            self._in_flight_turns -= 1
        if self._in_flight_turns == 0:
            self._turn_idle.set()

    @property
    def in_flight_turns(self) -> int:
        return self._in_flight_turns

    async def await_turns_idle(self, timeout: float | None = None) -> bool:
        try:
            await asyncio.wait_for(self._turn_idle.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    def changed_files(self) -> list[str]:
        current = _scan_files(self._watch_roots())
        changed: list[str] = []
        for path, mtime in current.items():
            previous = self._initial_fingerprints.get(path)
            if previous is None or previous != mtime:
                changed.append(path)
        for path in self._initial_fingerprints.keys() - current.keys():
            changed.append(path)
        return sorted(changed)

    def plan(self) -> dict[str, Any]:
        """Read-only summary so Engineer can inspect reload state."""

        history = [
            {
                "reload_id": item.reload_id,
                "tier": item.tier,
                "ok": item.ok,
                "ts": item.started_at,
                "duration_ms": item.duration_ms,
                "reason": item.reason,
                "error_type": item.error_type,
                "rolled_back": item.rolled_back,
                "changed_file_count": len(item.changed_files),
            }
            for item in self._history[-12:]
        ]
        return {
            "settings_state_dir": str(self._snapshot.settings.state_dir),
            "tools_module_id": self._snapshot.tools_module_id,
            "captured_at": self._snapshot.captured_at,
            "watch_roots": [str(p) for p in self._watch_roots()],
            "changed_files_since_boot": self.changed_files()[:80],
            "history": history,
            "tiers": {
                "config_and_prompts": "always available",
                "tools": "enabled" if self._snapshot.settings.hot_reload_tools else "disabled",
                "candidate": "enabled" if self._snapshot.settings.hot_reload_candidate else "disabled",
                "self_promote": "enabled" if self._snapshot.settings.enable_self_promote else "disabled",
            },
        }

    # ----------------------------------------------------------------- reloads

    async def reload_config_and_prompts(self, *, reason: str = "manual") -> ReloadResult:
        return await self._reload(tier="config", reason=reason, runner=self._do_reload_config)

    async def reload_tools(self, *, reason: str = "manual") -> ReloadResult:
        if not self._snapshot.settings.hot_reload_tools:
            return self._record(
                ReloadResult(
                    reload_id=_new_id(),
                    tier="tools",
                    ok=False,
                    started_at=time.time(),
                    finished_at=time.time(),
                    reason=reason,
                    error="hot_reload_tools is disabled",
                    error_type="ConfigDisabled",
                )
            )
        # Tier 2 re-imports ale.tools. The MCP server built by the running turn
        # holds closures into the old module dict, and the SDK's tool registry
        # keys collide on a duplicate registration. Wait for turns to drain so
        # the next agent call gets a clean rebuild.
        await self.await_turns_idle(
            timeout=self._snapshot.settings.reload_health_check_timeout_seconds
        )
        return await self._reload(tier="tools", reason=reason, runner=self._do_reload_tools)

    async def health_check_candidate(self, *, reason: str = "manual") -> ReloadResult:
        return await self._reload(
            tier="candidate", reason=reason, runner=self._do_candidate_health_check
        )

    async def promote_candidate(self, *, reason: str = "manual") -> ReloadResult:
        """Re-exec the current process so the new code path becomes live.

        Gated behind ``ALE_ENABLE_SELF_PROMOTE`` because it tears down all
        in-memory state. The candidate health check should be run first.
        """

        started = time.time()
        reload_id = _new_id()
        if not self._snapshot.settings.enable_self_promote:
            return self._record(
                ReloadResult(
                    reload_id=reload_id,
                    tier="candidate",
                    ok=False,
                    started_at=started,
                    finished_at=time.time(),
                    reason=reason,
                    error="enable_self_promote is disabled",
                    error_type="ConfigDisabled",
                )
            )
        last = self.last_result
        if last is None or last.tier != "candidate" or not last.ok:
            return self._record(
                ReloadResult(
                    reload_id=reload_id,
                    tier="candidate",
                    ok=False,
                    started_at=started,
                    finished_at=time.time(),
                    reason=reason,
                    error="no successful candidate health check on record",
                    error_type="HealthCheckMissing",
                )
            )
        argv = list(sys.argv)
        executable = sys.executable
        self.logger.event(
            "reload",
            "self_promote_starting",
            reload_id=reload_id,
            reason=reason,
            executable=executable,
            argv=argv,
        )
        result = self._record(
            ReloadResult(
                reload_id=reload_id,
                tier="candidate",
                ok=True,
                started_at=started,
                finished_at=time.time(),
                reason=reason,
                extra={"action": "execvp"},
            )
        )
        os.execvp(executable, [executable, *argv])  # noqa: S606 — intentional re-exec
        return result  # pragma: no cover - execvp does not return

    async def rollback(self, *, reason: str = "manual") -> ReloadResult:
        """Restore the previous snapshot if one exists."""

        started = time.time()
        reload_id = _new_id()
        async with self._lock:
            if self._previous_snapshot is None:
                return self._record(
                    ReloadResult(
                        reload_id=reload_id,
                        tier="config",
                        ok=False,
                        started_at=started,
                        finished_at=time.time(),
                        reason=reason,
                        error="no previous snapshot to roll back to",
                        error_type="RollbackUnavailable",
                    )
                )
            current = self._snapshot
            self._snapshot = self._previous_snapshot
            self._previous_snapshot = current
            result = self._record(
                ReloadResult(
                    reload_id=reload_id,
                    tier="config",
                    ok=True,
                    started_at=started,
                    finished_at=time.time(),
                    reason=reason,
                    rolled_back=True,
                )
            )
        await self._fan_out(result)
        self.logger.event("reload", "rolled_back", reload_id=reload_id, reason=reason)
        return result

    # ----------------------------------------------------------------- private

    async def _reload(
        self,
        *,
        tier: ReloadTier,
        reason: str,
        runner: Callable[[], tuple[ReloadSnapshot, dict[str, Any]]],
    ) -> ReloadResult:
        started = time.time()
        reload_id = _new_id()
        self.logger.event("reload", "tier_started", reload_id=reload_id, tier=tier, reason=reason)
        async with self._lock:
            try:
                new_snapshot, extra = await asyncio.to_thread(runner)
            except Exception as exc:
                self.logger.exception(
                    "reload",
                    "tier_failed",
                    exc,
                    reload_id=reload_id,
                    tier=tier,
                    reason=reason,
                )
                result = self._record(
                    ReloadResult(
                        reload_id=reload_id,
                        tier=tier,
                        ok=False,
                        started_at=started,
                        finished_at=time.time(),
                        reason=reason,
                        error=str(exc),
                        error_type=type(exc).__name__,
                    )
                )
                await self._fan_out(result)
                return result
            previous = self._snapshot
            self._previous_snapshot = previous
            self._snapshot = new_snapshot
            self._initial_fingerprints = _scan_files(self._watch_roots())
            self._settings = new_snapshot.settings
            result = self._record(
                ReloadResult(
                    reload_id=reload_id,
                    tier=tier,
                    ok=True,
                    started_at=started,
                    finished_at=time.time(),
                    reason=reason,
                    changed_files=tuple(extra.pop("changed_files", ()) or ()),
                    extra=extra,
                )
            )
        self.logger.event(
            "reload",
            "tier_completed",
            reload_id=reload_id,
            tier=tier,
            duration_ms=result.duration_ms,
            reason=reason,
        )
        await self._fan_out(result)
        return result

    def _do_reload_config(self) -> tuple[ReloadSnapshot, dict[str, Any]]:
        changed = self.changed_files()
        new_settings = load_settings()
        snapshot = self._capture_snapshot(new_settings)
        return snapshot, {"changed_files": changed, "tools_reimported": False}

    def _do_reload_tools(self) -> tuple[ReloadSnapshot, dict[str, Any]]:
        changed = self.changed_files()
        import ale.tools as tools_module

        importlib.reload(tools_module)
        new_settings = load_settings()
        snapshot = self._capture_snapshot(new_settings)
        return snapshot, {"changed_files": changed, "tools_reimported": True}

    def _do_candidate_health_check(self) -> tuple[ReloadSnapshot, dict[str, Any]]:
        executable = (
            self._snapshot.settings.candidate_python_executable or sys.executable or "python3"
        )
        cmd = [executable, "-m", "ale.cli", "health-check"]
        proc = subprocess.run(  # noqa: S603 — args are not user-controlled
            cmd,
            text=True,
            capture_output=True,
            timeout=self._snapshot.settings.reload_health_check_timeout_seconds,
            check=False,
        )
        ok = proc.returncode == 0
        snapshot = self._snapshot if ok else self._snapshot
        if not ok:
            raise RuntimeError(
                f"candidate health check exit_code={proc.returncode}: "
                f"{(proc.stderr or proc.stdout)[-2000:]}"
            )
        return snapshot, {
            "changed_files": self.changed_files(),
            "stdout_tail": proc.stdout[-2000:],
            "stderr_tail": proc.stderr[-2000:],
        }

    def _capture_snapshot(self, settings: Settings, *, reload_prompts: bool = True) -> ReloadSnapshot:
        # Reload only modules that aren't owning a currently-running coroutine.
        # ``ale.agent`` is *not* reloaded — its respond() holds module globals
        # whose mid-flight rebinding crashes the bundled Claude CLI subprocess.
        import ale.personas as personas_module
        import ale.prompts as prompts_module
        import ale.tools as tools_module

        if reload_prompts:
            importlib.reload(prompts_module)
            importlib.reload(personas_module)

        return ReloadSnapshot(
            settings=settings,
            base_system_prompt=prompts_module.BASE_SYSTEM,
            persona_prompts={
                name: persona.system_prompt
                for name, persona in personas_module.PERSONAS.items()
            },
            sdk_subagents_factory=personas_module.sdk_subagents,
            tools_module_id=id(tools_module),
            captured_at=time.time(),
        )

    def _watch_roots(self) -> list[Path]:
        return [Path(p).expanduser().resolve() for p in self._snapshot.settings.reload_watch_paths]

    def _record(self, result: ReloadResult) -> ReloadResult:
        self._history.append(result)
        if len(self._history) > self._max_history:
            self._history = self._history[-self._max_history :]
        return result

    async def _fan_out(self, result: ReloadResult) -> None:
        for listener in list(self._listeners):
            try:
                outcome = listener(result)
                if asyncio.iscoroutine(outcome):
                    await outcome
            except Exception as exc:
                self.logger.exception(
                    "reload",
                    "listener_failed",
                    exc,
                    reload_id=result.reload_id,
                    tier=result.tier,
                )


class FileWatchdog:
    """Polling file watcher that triggers staged reloads on .py changes.

    Polling avoids new dependencies and is good enough for this workload — the
    harness is one process editing a few hundred files, not a build farm.
    """

    def __init__(
        self,
        manager: ReloadManager,
        *,
        logger: EventLogger,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        self.manager = manager
        self.logger = logger
        self.loop = loop
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._fingerprints: dict[str, float] = {}

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.running:
            return
        settings = self.manager.settings
        if not settings.hot_reload_enabled:
            self.logger.event("reload", "watchdog_disabled")
            return
        self._stop.clear()
        self._fingerprints = _scan_files(
            [Path(p).expanduser().resolve() for p in settings.reload_watch_paths]
        )
        self.logger.event(
            "reload",
            "watchdog_started",
            watch_roots=[str(Path(p).expanduser().resolve()) for p in settings.reload_watch_paths],
            poll_interval_seconds=settings.reload_poll_interval_seconds,
            debounce_seconds=settings.reload_debounce_seconds,
            tools_reload_enabled=settings.hot_reload_tools,
            candidate_health_check_enabled=settings.hot_reload_candidate,
        )
        self._task = asyncio.create_task(self._run(), name="ale.reload.watchdog")

    async def stop(self) -> None:
        if not self.running:
            return
        self._stop.set()
        assert self._task is not None
        try:
            await asyncio.wait_for(self._task, timeout=5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            self._task.cancel()
        finally:
            self._task = None
        self.logger.event("reload", "watchdog_stopped")

    async def _run(self) -> None:
        settings = self.manager.settings
        poll = max(0.2, settings.reload_poll_interval_seconds)
        debounce = max(0.0, settings.reload_debounce_seconds)
        last_change_seen: float | None = None
        pending: list[str] = []
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=poll)
                break
            except asyncio.TimeoutError:
                pass
            settings = self.manager.settings
            current = _scan_files(
                [Path(p).expanduser().resolve() for p in settings.reload_watch_paths]
            )
            diff = self._diff(self._fingerprints, current)
            if diff:
                pending = sorted(set(pending) | set(diff))
                last_change_seen = time.monotonic()
                self.logger.event(
                    "reload",
                    "watchdog_change_detected",
                    changed_count=len(diff),
                    sample=diff[:5],
                )
                self._fingerprints = current
                continue
            if last_change_seen is not None and (time.monotonic() - last_change_seen) >= debounce:
                files = pending
                pending = []
                last_change_seen = None
                await self._fire(files)

    async def _fire(self, changed: list[str]) -> None:
        settings = self.manager.settings
        in_flight = self.manager.in_flight_turns
        self.logger.event(
            "reload",
            "watchdog_fire",
            changed_count=len(changed),
            sample=changed[:5],
            will_reload_tools=settings.hot_reload_tools,
            will_health_check_candidate=settings.hot_reload_candidate,
            in_flight_turns=in_flight,
        )
        if in_flight:
            self.logger.event(
                "reload",
                "watchdog_awaiting_idle",
                in_flight_turns=in_flight,
                timeout_seconds=settings.reload_health_check_timeout_seconds,
            )
            await self.manager.await_turns_idle(
                timeout=settings.reload_health_check_timeout_seconds
            )
        config_result = await self.manager.reload_config_and_prompts(reason="watchdog")
        if not config_result.ok:
            return
        if settings.hot_reload_tools:
            await self.manager.reload_tools(reason="watchdog")
        if settings.hot_reload_candidate:
            await self.manager.health_check_candidate(reason="watchdog")

    @staticmethod
    def _diff(previous: dict[str, float], current: dict[str, float]) -> list[str]:
        changed: list[str] = []
        for path, mtime in current.items():
            if previous.get(path) != mtime:
                changed.append(path)
        for path in previous.keys() - current.keys():
            changed.append(path)
        return sorted(changed)


def _new_id() -> str:
    return uuid.uuid4().hex[:12]
