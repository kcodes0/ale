"""Structured logging for Ale.

Engineer needs logs that are easy to parse after the fact. Every event is JSONL,
with a global all-events stream plus subsystem-specific streams.
"""

from __future__ import annotations

import json
import logging
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
    def __init__(self, logs_dir: Path) -> None:
        self.logs_dir = logs_dir
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.all_events_path = self.logs_dir / "all-events.jsonl"
        self.turns_path = self.logs_dir / "turns.jsonl"

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
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")


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
