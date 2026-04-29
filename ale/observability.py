"""Structured logging for Ale.

Engineer needs logs that are easy to parse after the fact. Every event is JSONL,
with a global all-events stream plus subsystem-specific streams.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from ale.models import utc_now_iso

SUBSYSTEM_LOGS = {
    "agent": "agent.log",
    "agent-team": "agent-team.log",
    "core": "core-system.log",
    "discord": "discord.log",
    "reload": "reload.log",
    "router": "router.log",
    "threads": "threads.log",
    "tools": "tools.log",
    "workspace": "workspace.log",
}


class EventLogger:
    def __init__(
        self,
        logs_dir: Path,
        *,
        max_bytes: int = 10_000_000,
        backup_count: int = 5,
    ) -> None:
        self.logs_dir = logs_dir
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.all_events_path = self.logs_dir / "all-events.jsonl"
        self.turns_path = self.logs_dir / "turns.jsonl"
        self.max_bytes = max(0, max_bytes)
        self.backup_count = max(0, backup_count)

    def event(self, subsystem: str, event: str, **fields: Any) -> None:
        payload = {
            "ts": utc_now_iso(),
            "subsystem": subsystem,
            "event": event,
            **fields,
        }
        self._append(self.all_events_path, payload)
        self._append(self.logs_dir / SUBSYSTEM_LOGS.get(subsystem, f"{subsystem}.log"), payload)

    def team(self, event: str, **fields: Any) -> None:
        """Emit a team-coordination event onto the agent-team log.

        Use this for cross-agent or workspace-coordination signals (handoffs,
        codex worker dispatch, lead/peer transitions, recap diffs) that benefit
        from a focused stream separate from per-tool agent.log noise.
        """

        self.event("agent-team", event, **fields)

    def turn(self, **fields: Any) -> None:
        payload = {"ts": utc_now_iso(), "subsystem": "agent", "event": "turn", **fields}
        self._append(self.turns_path, payload)
        self._append(self.all_events_path, payload)
        self._append(self.logs_dir / SUBSYSTEM_LOGS["agent"], payload)

    def exception(self, subsystem: str, event: str, exc: BaseException, **fields: Any) -> None:
        self.event(
            subsystem,
            event,
            error_type=type(exc).__name__,
            error=str(exc),
            **fields,
        )

    def _append(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(payload, ensure_ascii=False, default=str) + "\n"
        self._rotate_if_needed(path, len(line.encode("utf-8")))
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)

    def _rotate_if_needed(self, path: Path, incoming_bytes: int) -> None:
        if self.max_bytes <= 0 or self.backup_count <= 0 or not path.exists():
            return
        try:
            if path.stat().st_size + incoming_bytes <= self.max_bytes:
                return
        except OSError:
            return
        oldest = path.with_name(f"{path.name}.{self.backup_count}")
        try:
            if oldest.exists():
                oldest.unlink()
            for index in range(self.backup_count - 1, 0, -1):
                src = path.with_name(f"{path.name}.{index}")
                dst = path.with_name(f"{path.name}.{index + 1}")
                if src.exists():
                    os.replace(src, dst)
            os.replace(path, path.with_name(f"{path.name}.1"))
        except OSError:
            # Logging must never take down the Discord process. If rotation
            # fails, append to the current file and let operators inspect perms.
            return


class TurnLogger(EventLogger):
    """Backward-compatible name for older call sites."""

    def __init__(self, path: Path) -> None:
        super().__init__(path.parent)

    def write(self, event: dict[str, Any]) -> None:
        self.turn(**event)


class JsonLogHandler(logging.Handler):
    """Bridge stdlib logging into Ale's structured logs."""

    def __init__(self, logger: EventLogger, subsystem: str) -> None:
        super().__init__()
        self.event_logger = logger
        self.subsystem = subsystem

    def emit(self, record: logging.LogRecord) -> None:
        self.event_logger.event(
            self.subsystem,
            "python_log",
            level=record.levelname,
            logger=record.name,
            message=record.getMessage(),
        )


def configure_python_logging(logger: EventLogger) -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not any(isinstance(handler, JsonLogHandler) for handler in root.handlers):
        root.addHandler(JsonLogHandler(logger, "core"))
