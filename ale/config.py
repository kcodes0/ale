"""Runtime configuration for Ale."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


DEFAULT_STATE_DIR = Path.home() / ".local" / "state" / "ale"
DEFAULT_MEMORY_DIR = Path.home() / ".claude" / "projects" / "-home-claude" / "memory"


@dataclass(frozen=True)
class Settings:
    discord_token: str | None
    anthropic_api_key: str | None
    allowed_user_ids: frozenset[int] = field(default_factory=frozenset)
    state_dir: Path = DEFAULT_STATE_DIR
    memory_dir: Path = DEFAULT_MEMORY_DIR
    work_dir: Path = Path.cwd()
    actor_model: str = "claude-haiku-4-5"
    linguist_model: str = "claude-opus-4-6"
    engineer_model: str = "claude-opus-4-6"
    router_model: str = "claude-haiku-4-5"
    enable_llm_router: bool = False
    max_recent_turns: int = 10
    recap_every_turns: int = 6
    max_turns: int = 12
    max_budget_usd: float | None = 2.0
    discord_reply_limit: int = 1900
    discord_message_content_intent: bool = False
    discord_sync_commands: str = "auto"
    discord_artifact_threshold: int = 2400
    artifact_publish_command: str | None = None
    artifact_publish_timeout_seconds: int = 120
    enable_pdf_artifacts: bool = True
    enable_actor_summary: bool = True
    actor_summary_max_chars: int = 1400
    actor_summary_max_budget_usd: float = 0.05
    specialist_progress_min_seconds: float = 15.0
    enable_bash_tool: bool = False
    enable_codex_exec: bool = False
    heartbeat_command: str = "claude-heartbeat"
    codex_command: str = "codex"
    codex_timeout_seconds: int = 1800
    # Watchdog auto-reload (file-system → staged reload) is opt-in. The Lead
    # Engineer drives reloads manually via the reload_* MCP tools from inside
    # a Workspace; that gives them deliberate control instead of a watcher
    # firing mid-turn. Set ALE_HOT_RELOAD_ENABLED=true to bring the watchdog
    # back for local development.
    hot_reload_enabled: bool = False
    hot_reload_tools: bool = True
    hot_reload_candidate: bool = False
    enable_reload_tools: bool = True
    enable_self_promote: bool = False
    reload_debounce_seconds: float = 1.5
    reload_poll_interval_seconds: float = 1.0
    reload_watch_paths: tuple[str, ...] = ("ale",)
    reload_health_check_timeout_seconds: int = 60
    candidate_python_executable: str | None = None
    log_max_bytes: int = 10_000_000
    log_backup_count: int = 5

    @property
    def threads_dir(self) -> Path:
        return self.state_dir / "threads"

    @property
    def logs_dir(self) -> Path:
        return self.state_dir / "logs"

    @property
    def turns_log_path(self) -> Path:
        return self.logs_dir / "turns.jsonl"

    @property
    def all_events_log_path(self) -> Path:
        return self.logs_dir / "all-events.jsonl"

    @property
    def workspaces_dir(self) -> Path:
        return self.state_dir / "workspaces"

    @property
    def artifacts_dir(self) -> Path:
        return self.state_dir / "artifacts"

    @property
    def access_path(self) -> Path:
        return self.state_dir / "access.json"


def _read_secret_file(path: str | None) -> str | None:
    if not path:
        return None
    secret_path = Path(path).expanduser()
    if not secret_path.exists():
        return None
    return secret_path.read_text(encoding="utf-8").strip() or None


def _csv_ints(value: str | None) -> frozenset[int]:
    if not value:
        return frozenset()
    out: set[int] = set()
    for raw in value.split(","):
        raw = raw.strip()
        if raw:
            out.add(int(raw))
    return frozenset(out)


def _load_access_ids(path: Path) -> frozenset[int]:
    if not path.exists():
        return frozenset()
    data: Any = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return frozenset(int(item) for item in data)
    if isinstance(data, dict):
        ids = data.get("allowed_user_ids") or data.get("users") or []
        return frozenset(int(item) for item in ids)
    raise ValueError(f"Unsupported access file format: {path}")


def load_settings(env_file: Path | None = None) -> Settings:
    """Load settings from `.env`, environment variables, and optional access.json."""

    if env_file:
        load_dotenv(env_file)
    else:
        load_dotenv()

    state_dir = Path(os.getenv("ALE_STATE_DIR", str(DEFAULT_STATE_DIR))).expanduser()
    access_path = Path(os.getenv("ALE_ACCESS_FILE", str(state_dir / "access.json"))).expanduser()

    env_ids = _csv_ints(os.getenv("ALE_ALLOWED_USER_IDS"))
    file_ids = _load_access_ids(access_path)

    anthropic_api_key = os.getenv("ANTHROPIC_API_KEY") or _read_secret_file(
        os.getenv("ANTHROPIC_API_KEY_FILE")
    )
    discord_token = os.getenv("DISCORD_TOKEN") or _read_secret_file(os.getenv("DISCORD_TOKEN_FILE"))

    return Settings(
        discord_token=discord_token,
        anthropic_api_key=anthropic_api_key,
        allowed_user_ids=env_ids | file_ids,
        state_dir=state_dir,
        memory_dir=Path(os.getenv("ALE_MEMORY_DIR", str(DEFAULT_MEMORY_DIR))).expanduser(),
        work_dir=Path(os.getenv("ALE_WORK_DIR", str(Path.cwd()))).expanduser(),
        actor_model=os.getenv("ALE_ACTOR_MODEL", "claude-haiku-4-5"),
        linguist_model=os.getenv("ALE_LINGUIST_MODEL", "claude-opus-4-6"),
        engineer_model=os.getenv("ALE_ENGINEER_MODEL", "claude-opus-4-6"),
        router_model=os.getenv("ALE_ROUTER_MODEL", "claude-haiku-4-5"),
        enable_llm_router=os.getenv("ALE_ENABLE_LLM_ROUTER", "true").lower()
        in {"1", "true", "yes"},
        max_recent_turns=int(os.getenv("ALE_MAX_RECENT_TURNS", "10")),
        recap_every_turns=int(os.getenv("ALE_RECAP_EVERY_TURNS", "6")),
        max_turns=int(os.getenv("ALE_MAX_TURNS", "12")),
        max_budget_usd=(
            None
            if os.getenv("ALE_MAX_BUDGET_USD", "2.0").lower() == "none"
            else float(os.getenv("ALE_MAX_BUDGET_USD", "2.0"))
        ),
        discord_reply_limit=int(os.getenv("ALE_DISCORD_REPLY_LIMIT", "1900")),
        discord_message_content_intent=os.getenv(
            "ALE_DISCORD_MESSAGE_CONTENT_INTENT", "false"
        ).lower()
        in {"1", "true", "yes"},
        discord_sync_commands=os.getenv("ALE_DISCORD_SYNC_COMMANDS", "auto").lower(),
        discord_artifact_threshold=int(os.getenv("ALE_DISCORD_ARTIFACT_THRESHOLD", "2400")),
        artifact_publish_command=os.getenv("ALE_ARTIFACT_PUBLISH_COMMAND") or None,
        artifact_publish_timeout_seconds=int(os.getenv("ALE_ARTIFACT_PUBLISH_TIMEOUT_SECONDS", "120")),
        enable_pdf_artifacts=os.getenv("ALE_ENABLE_PDF_ARTIFACTS", "true").lower()
        in {"1", "true", "yes"},
        enable_actor_summary=os.getenv("ALE_ENABLE_ACTOR_SUMMARY", "true").lower()
        in {"1", "true", "yes"},
        actor_summary_max_chars=int(os.getenv("ALE_ACTOR_SUMMARY_MAX_CHARS", "1400")),
        actor_summary_max_budget_usd=float(os.getenv("ALE_ACTOR_SUMMARY_MAX_BUDGET_USD", "0.05")),
        specialist_progress_min_seconds=float(os.getenv("ALE_SPECIALIST_PROGRESS_MIN_SECONDS", "15")),
        enable_bash_tool=os.getenv("ALE_ENABLE_BASH_TOOL", "false").lower() in {"1", "true", "yes"},
        enable_codex_exec=os.getenv("ALE_ENABLE_CODEX_EXEC", "false").lower()
        in {"1", "true", "yes"},
        heartbeat_command=os.getenv("ALE_HEARTBEAT_COMMAND", "claude-heartbeat"),
        codex_command=os.getenv("ALE_CODEX_COMMAND", "codex"),
        codex_timeout_seconds=int(os.getenv("ALE_CODEX_TIMEOUT_SECONDS", "1800")),
        hot_reload_enabled=os.getenv("ALE_HOT_RELOAD_ENABLED", "false").lower()
        in {"1", "true", "yes"},
        hot_reload_tools=os.getenv("ALE_HOT_RELOAD_TOOLS", "true").lower() in {"1", "true", "yes"},
        hot_reload_candidate=os.getenv("ALE_HOT_RELOAD_CANDIDATE", "false").lower()
        in {"1", "true", "yes"},
        enable_reload_tools=os.getenv("ALE_ENABLE_RELOAD_TOOLS", "true").lower()
        in {"1", "true", "yes"},
        enable_self_promote=os.getenv("ALE_ENABLE_SELF_PROMOTE", "false").lower()
        in {"1", "true", "yes"},
        reload_debounce_seconds=float(os.getenv("ALE_RELOAD_DEBOUNCE_SECONDS", "1.5")),
        reload_poll_interval_seconds=float(os.getenv("ALE_RELOAD_POLL_INTERVAL_SECONDS", "1.0")),
        reload_watch_paths=tuple(
            part.strip()
            for part in os.getenv("ALE_RELOAD_WATCH_PATHS", "ale").split(",")
            if part.strip()
        )
        or ("ale",),
        reload_health_check_timeout_seconds=int(
            os.getenv("ALE_RELOAD_HEALTH_CHECK_TIMEOUT_SECONDS", "60")
        ),
        candidate_python_executable=os.getenv("ALE_CANDIDATE_PYTHON") or None,
        log_max_bytes=int(os.getenv("ALE_LOG_MAX_BYTES", "10000000")),
        log_backup_count=int(os.getenv("ALE_LOG_BACKUP_COUNT", "5")),
    )
