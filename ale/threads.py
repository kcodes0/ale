"""Filesystem-backed Discord thread store."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from ale.models import ChatMessage, ThreadMeta, ThreadState, utc_now_iso
from ale.observability import EventLogger


def slugify(value: str, fallback: str = "thread") -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.lower()).strip("-")
    return slug[:80] or fallback


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


@dataclass(frozen=True)
class ThreadSummary:
    thread_id: str
    title: str
    last_active: str
    message_count: int
    persona: str
    recap: str
    archived: bool = False


class ThreadStore:
    """Stores each conversation thread as meta.json, recap.md, and messages.jsonl."""

    def __init__(
        self, root: Path, max_recent_turns: int = 10, logger: EventLogger | None = None
    ) -> None:
        self.root = root
        self.max_recent_turns = max_recent_turns
        self.logger = logger
        self.root.mkdir(parents=True, exist_ok=True)
        self._locks: dict[str, asyncio.Lock] = {}

    def lock_for(self, thread_id: str) -> asyncio.Lock:
        lock = self._locks.get(thread_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[thread_id] = lock
        return lock

    def thread_dir(self, thread_id: str) -> Path:
        return self.root / slugify(thread_id)

    def meta_path(self, thread_id: str) -> Path:
        return self.thread_dir(thread_id) / "meta.json"

    def recap_path(self, thread_id: str) -> Path:
        return self.thread_dir(thread_id) / "recap.md"

    def messages_path(self, thread_id: str) -> Path:
        return self.thread_dir(thread_id) / "messages.jsonl"

    def exists(self, thread_id: str) -> bool:
        return self.meta_path(thread_id).exists()

    def create(
        self,
        title: str,
        *,
        thread_id: str | None = None,
        discord_channel_id: str | None = None,
        discord_user_id: int | None = None,
        persona: str = "actor",
    ) -> ThreadState:
        now = utc_now_iso()
        base = slugify(thread_id or title)
        candidate = base
        suffix = 2
        while self.exists(candidate):
            candidate = f"{base}-{suffix}"
            suffix += 1
        meta = ThreadMeta(
            thread_id=candidate,
            title=title.strip()[:120] or candidate,
            created_at=now,
            last_active=now,
            discord_channel_id=discord_channel_id,
            discord_user_id=discord_user_id,
            persona=persona,
        )
        state = ThreadState(meta=meta)
        self.save_meta(meta)
        self.save_recap(meta.thread_id, "")
        self.messages_path(meta.thread_id).touch(exist_ok=True)
        self._log(
            "thread_created",
            thread_id=meta.thread_id,
            title=meta.title,
            persona=meta.persona,
            discord_channel_id=discord_channel_id,
            discord_user_id=discord_user_id,
        )
        return state

    def load(self, thread_id: str, *, recent_limit: int | None = None) -> ThreadState:
        meta = ThreadMeta.from_json(json.loads(self.meta_path(thread_id).read_text(encoding="utf-8")))
        recap_path = self.recap_path(thread_id)
        recap = recap_path.read_text(encoding="utf-8") if recap_path.exists() else ""
        limit = self.max_recent_turns if recent_limit is None else recent_limit
        messages = self.read_messages(thread_id, limit=limit)
        self._log("thread_loaded", thread_id=thread_id, recent_limit=recent_limit)
        return ThreadState(meta=meta, recap=recap, messages=messages)

    def save_meta(self, meta: ThreadMeta) -> None:
        atomic_write(self.meta_path(meta.thread_id), json.dumps(meta.to_json(), indent=2) + "\n")
        self._log("meta_saved", thread_id=meta.thread_id, message_count=meta.message_count)

    def save_recap(self, thread_id: str, recap: str) -> None:
        atomic_write(self.recap_path(thread_id), recap.strip() + "\n" if recap.strip() else "")
        self._log("recap_saved", thread_id=thread_id, bytes=len(recap.encode("utf-8")))

    def append_message(self, thread_id: str, message: ChatMessage) -> None:
        path = self.messages_path(thread_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(message.to_json(), ensure_ascii=False) + "\n")
        meta = ThreadMeta.from_json(json.loads(self.meta_path(thread_id).read_text(encoding="utf-8")))
        meta.message_count += 1
        meta.last_active = message.ts
        self.save_meta(meta)
        self._log(
            "message_appended",
            thread_id=thread_id,
            role=message.role,
            discord_message_id=message.discord_message_id,
            message_count=meta.message_count,
        )

    def read_messages(self, thread_id: str, *, limit: int | None = None) -> list[ChatMessage]:
        path = self.messages_path(thread_id)
        if not path.exists():
            return []
        lines = path.read_text(encoding="utf-8").splitlines()
        if limit == 0:
            return []
        if limit is not None:
            lines = lines[-limit:]
        return [ChatMessage.from_json(json.loads(line)) for line in lines if line.strip()]

    def list_summaries(self, *, include_archived: bool = False) -> list[ThreadSummary]:
        summaries: list[ThreadSummary] = []
        for meta_path in self.root.glob("*/meta.json"):
            meta = ThreadMeta.from_json(json.loads(meta_path.read_text(encoding="utf-8")))
            if meta.archived and not include_archived:
                continue
            recap_path = meta_path.parent / "recap.md"
            recap = recap_path.read_text(encoding="utf-8") if recap_path.exists() else ""
            summaries.append(
                ThreadSummary(
                    thread_id=meta.thread_id,
                    title=meta.title,
                    last_active=meta.last_active,
                    message_count=meta.message_count,
                    persona=meta.persona,
                    recap=recap,
                    archived=meta.archived,
                )
            )
        return sorted(summaries, key=lambda item: item.last_active, reverse=True)

    def search(self, query: str, *, limit: int = 10) -> list[ThreadSummary]:
        needle = query.lower()
        matches = [
            summary
            for summary in self.list_summaries(include_archived=True)
            if needle in summary.title.lower()
            or needle in summary.thread_id.lower()
            or needle in summary.recap.lower()
        ]
        return matches[:limit]

    def format_recent(self, messages: Iterable[ChatMessage]) -> str:
        chunks = []
        for message in messages:
            author = message.author_name or message.role
            chunks.append(f"[{message.ts}] {author}: {message.content}")
        return "\n".join(chunks)

    def _log(self, event: str, **fields: object) -> None:
        if self.logger:
            self.logger.event("threads", event, **fields)
