"""Shared data models for Ale."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


Role = Literal["user", "assistant", "tool", "system"]


@dataclass(frozen=True)
class ChatMessage:
    role: Role
    content: str
    ts: str = field(default_factory=utc_now_iso)
    discord_message_id: str | None = None
    author_id: int | None = None
    author_name: str | None = None
    attachments: list[dict[str, Any]] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "content": self.content,
            "ts": self.ts,
            "discord_message_id": self.discord_message_id,
            "author_id": self.author_id,
            "author_name": self.author_name,
            "attachments": self.attachments,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "ChatMessage":
        return cls(
            role=data["role"],
            content=data["content"],
            ts=data.get("ts") or utc_now_iso(),
            discord_message_id=data.get("discord_message_id"),
            author_id=data.get("author_id"),
            author_name=data.get("author_name"),
            attachments=list(data.get("attachments") or []),
        )


@dataclass
class ThreadMeta:
    thread_id: str
    title: str
    created_at: str
    last_active: str
    message_count: int = 0
    schema_version: int = 1
    discord_channel_id: str | None = None
    discord_user_id: int | None = None
    persona: str = "actor"
    archived: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "thread_id": self.thread_id,
            "title": self.title,
            "created_at": self.created_at,
            "last_active": self.last_active,
            "message_count": self.message_count,
            "schema_version": self.schema_version,
            "discord_channel_id": self.discord_channel_id,
            "discord_user_id": self.discord_user_id,
            "persona": self.persona,
            "archived": self.archived,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "ThreadMeta":
        version = int(data.get("schema_version", 1))
        if version > 1:
            raise ValueError(f"Unsupported thread schema version {version}")
        return cls(
            thread_id=data["thread_id"],
            title=data.get("title") or data["thread_id"],
            created_at=data.get("created_at") or utc_now_iso(),
            last_active=data.get("last_active") or utc_now_iso(),
            message_count=int(data.get("message_count", 0)),
            schema_version=version,
            discord_channel_id=data.get("discord_channel_id"),
            discord_user_id=data.get("discord_user_id"),
            persona=data.get("persona") or "actor",
            archived=bool(data.get("archived", False)),
        )


@dataclass
class ThreadState:
    meta: ThreadMeta
    recap: str = ""
    messages: list[ChatMessage] = field(default_factory=list)


@dataclass(frozen=True)
class RouteDecision:
    thread_id: str
    persona: str = "actor"
    confidence: float = 0.0
    reason: str = ""
    is_new: bool = False
