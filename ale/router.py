"""Thread and persona routing for incoming Discord messages."""

from __future__ import annotations

import json
import os
import re

import httpx

from ale.config import Settings
from ale.models import RouteDecision
from ale.observability import EventLogger
from ale.threads import ThreadStore, ThreadSummary, slugify

try:
    from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query
except Exception:  # pragma: no cover - tests can import without SDK installed.
    ClaudeAgentOptions = None  # type: ignore[assignment]
    ResultMessage = None  # type: ignore[assignment]
    query = None  # type: ignore[assignment]


PERSONA_MARKERS = {
    "actor": re.compile(r"(^|\s)@?(actor|chat)\b", re.IGNORECASE),
    "linguist": re.compile(r"(^|\s)@?(linguist|research|synthesis)\b", re.IGNORECASE),
}

EXPLICIT_PERSONA_MARKERS = {
    "actor": re.compile(r"^\s*@?(actor|chat)\s*:", re.IGNORECASE),
    "linguist": re.compile(r"^\s*@?(linguist|research|synthesis)\s*:", re.IGNORECASE),
}


class Router:
    """Cheap router for thread continuity and Actor/Linguist/Engineer selection.

    The first implementation is deterministic and local. It is intentionally shaped so an
    LLM/embedding router can replace `route` later without changing the Discord flow.
    """

    def __init__(
        self, settings: Settings, threads: ThreadStore, logger: EventLogger | None = None
    ) -> None:
        self.settings = settings
        self.threads = threads
        self.logger = logger

    def infer_persona(self, content: str) -> str:
        explicit = self.explicit_persona(content)
        if explicit:
            return explicit
        lowered = content.lower()
        if any(word in lowered for word in ("deep research", "sources", "cite", "literature review")):
            return "linguist"
        # Engineer is only invoked via /engineer slash command (or internal harness).
        for persona, pattern in PERSONA_MARKERS.items():
            if pattern.search(content):
                return persona
        return "actor"

    def explicit_persona(self, content: str) -> str | None:
        for persona, pattern in EXPLICIT_PERSONA_MARKERS.items():
            if pattern.search(content):
                return persona
        return None

    async def route(
        self,
        content: str,
        *,
        discord_channel_id: str | None,
        discord_user_id: int | None,
    ) -> RouteDecision:
        explicit_persona = self.explicit_persona(content)
        persona = explicit_persona or self.infer_persona(content)
        self._log(
            "route_started",
            persona=persona,
            explicit_persona=explicit_persona,
            discord_channel_id=discord_channel_id,
            discord_user_id=discord_user_id,
            content_chars=len(content),
        )
        explicit = self._route_by_explicit_marker(content, persona)
        if explicit:
            self._log("route_completed", **explicit.__dict__)
            return explicit

        summaries = self.threads.list_summaries()
        if not summaries:
            decision = self._new_decision(content, persona)
            self._log("route_completed", **decision.__dict__)
            return decision

        same_channel = [
            summary
            for summary in summaries
            if self._summary_channel(summary.thread_id) == discord_channel_id
        ]
        pool = same_channel or summaries[:12]
        if explicit_persona:
            self._log(
                "llm_route_skipped",
                reason="explicit_persona_marker",
                persona=explicit_persona,
                candidate_count=len(pool),
            )
        else:
            llm_decision = await self._route_with_llm(content, persona, pool)
            if llm_decision:
                self._log("route_completed", **llm_decision.__dict__)
                return llm_decision
        best = self._best_keyword_match(content, pool)
        if best:
            decision = RouteDecision(
                thread_id=best.thread_id,
                persona=persona or best.persona,
                confidence=0.62,
                reason="keyword/recency match",
                is_new=False,
            )
            self._log("route_completed", **decision.__dict__)
            return decision
        decision = self._new_decision(content, persona)
        self._log("route_completed", **decision.__dict__)
        return decision

    async def refresh_recap(self, thread_id: str) -> str:
        """Refresh a recap with Haiku when configured, otherwise use a local fallback."""

        messages = self.threads.read_messages(thread_id, limit=24)
        if not messages:
            recap = ""
            used_llm = False
        elif self.settings.enable_llm_router:
            recap = await self._recap_with_llm(thread_id)
            used_llm = True
        else:
            recap = self._local_recap(thread_id)
            used_llm = False
        self.threads.save_recap(thread_id, recap)
        self._log("recap_refreshed", thread_id=thread_id, chars=len(recap), used_llm=used_llm)
        return recap

    async def _route_with_llm(
        self, content: str, persona: str, candidates: list[ThreadSummary]
    ) -> RouteDecision | None:
        if query is None or ClaudeAgentOptions is None or not self.settings.enable_llm_router:
            return None
        if self.settings.anthropic_api_key:
            os.environ["ANTHROPIC_API_KEY"] = self.settings.anthropic_api_key
        candidate_payload = [
            {
                "thread_id": item.thread_id,
                "title": item.title,
                "last_active": item.last_active,
                "persona": item.persona,
                "recap": item.recap[:1200],
            }
            for item in candidates[:12]
        ]
        prompt = f"""\
Return JSON only with keys: thread_id, confidence, reason, is_new.
Choose an existing thread only if the incoming Discord message clearly continues it.
Otherwise set thread_id to "new" and is_new to true.

Incoming message:
{content}

Candidate threads:
{json.dumps(candidate_payload, ensure_ascii=False, indent=2)}
"""
        try:
            self._log("llm_route_started", candidate_count=len(candidate_payload))
            result_text = ""
            async for message in query(
                prompt=prompt,
                options=ClaudeAgentOptions(
                    tools=[],
                    allowed_tools=[],
                    permission_mode="dontAsk",
                    model=self.settings.router_model,
                    max_turns=1,
                    max_budget_usd=0.05,
                    setting_sources=[],
                    system_prompt="You are Ale's cheap thread router. Return strict JSON only.",
                ),
            ):
                if ResultMessage is not None and isinstance(message, ResultMessage):
                    result_text = message.result or ""
            data = json.loads(result_text.strip())
            thread_id = str(data.get("thread_id") or "new")
            is_new = bool(data.get("is_new")) or thread_id == "new"
            confidence = float(data.get("confidence") or 0.0)
            if is_new or confidence < 0.55 or not self.threads.exists(thread_id):
                self._log("llm_route_new_or_low_confidence", confidence=confidence, thread_id=thread_id)
                return self._new_decision(content, persona)
            decision = RouteDecision(
                thread_id=thread_id,
                persona=persona,
                confidence=confidence,
                reason=str(data.get("reason") or "llm router"),
                is_new=False,
            )
            self._log("llm_route_completed", **decision.__dict__)
            return decision
        except Exception as exc:
            self._log(
                "llm_route_failed",
                phase="router.llm_route.query_or_parse",
                fallback="deterministic_keyword_or_new_thread",
                model=self.settings.router_model,
                candidate_count=len(candidate_payload),
                error_type=type(exc).__name__,
                error=str(exc),
            )
            return None

    async def _recap_with_llm(self, thread_id: str) -> str:
        if query is None or ClaudeAgentOptions is None:
            return self._local_recap(thread_id)
        messages = self.threads.read_messages(thread_id, limit=80)
        transcript = self.threads.format_recent(messages)
        prompt = f"""\
Summarize this Ale Discord thread for future context.
Include stable decisions, open loops, user preferences, and exact details that are likely to matter.
Keep it under 500 words. Do not invent details.

Thread: {thread_id}

Transcript:
{transcript}
"""
        try:
            self._log("llm_recap_started", thread_id=thread_id, chars=len(transcript))
            result_text = ""
            async for message in query(
                prompt=prompt,
                options=ClaudeAgentOptions(
                    tools=[],
                    allowed_tools=[],
                    permission_mode="dontAsk",
                    model=self.settings.router_model,
                    max_turns=1,
                    max_budget_usd=0.10,
                    setting_sources=[],
                    system_prompt="You are Ale's recap writer. Produce concise Markdown only.",
                ),
            ):
                if ResultMessage is not None and isinstance(message, ResultMessage):
                    result_text = message.result or ""
            return result_text.strip() or self._local_recap(thread_id)
        except Exception as exc:
            self._log(
                "llm_recap_failed",
                phase="router.llm_recap.query",
                fallback="local_recap",
                thread_id=thread_id,
                model=self.settings.router_model,
                transcript_chars=len(transcript),
                error_type=type(exc).__name__,
                error=str(exc),
            )
            return self._local_recap(thread_id)

    def _local_recap(self, thread_id: str) -> str:
        messages = self.threads.read_messages(thread_id, limit=24)
        user_lines = [m.content.strip() for m in messages if m.role == "user" and m.content.strip()]
        assistant_lines = [
            m.content.strip() for m in messages if m.role == "assistant" and m.content.strip()
        ]
        return "\n".join(
            [
                f"# {thread_id}",
                "",
                "Recent user intents:",
                *[f"- {line[:240]}" for line in user_lines[-6:]],
                "",
                "Recent Ale outcomes:",
                *[f"- {line[:240]}" for line in assistant_lines[-4:]],
            ]
        ).strip()

    def _route_by_explicit_marker(self, content: str, persona: str) -> RouteDecision | None:
        match = re.search(r"(?:thread|topic):\s*([a-zA-Z0-9_.-]+)", content)
        if match and self.threads.exists(match.group(1)):
            return RouteDecision(
                thread_id=match.group(1),
                persona=persona,
                confidence=1.0,
                reason="explicit thread marker",
                is_new=False,
            )
        if re.search(r"\b(new thread|new topic)\b", content, re.IGNORECASE):
            return self._new_decision(content, persona)
        return None

    def _new_decision(self, content: str, persona: str) -> RouteDecision:
        title = content.strip().splitlines()[0][:80] or "Discord thread"
        return RouteDecision(
            thread_id=slugify(title),
            persona=persona,
            confidence=0.8,
            reason="new topic",
            is_new=True,
        )

    def _best_keyword_match(
        self, content: str, summaries: list[ThreadSummary]
    ) -> ThreadSummary | None:
        words = {word for word in re.findall(r"[a-zA-Z0-9]{4,}", content.lower())}
        if not words:
            return summaries[0] if summaries else None
        scored: list[tuple[int, ThreadSummary]] = []
        for summary in summaries:
            haystack = f"{summary.title} {summary.recap}".lower()
            score = sum(1 for word in words if word in haystack)
            if score:
                scored.append((score, summary))
        if not scored:
            return None
        scored.sort(key=lambda item: (item[0], item[1].last_active), reverse=True)
        return scored[0][1]

    def _summary_channel(self, thread_id: str) -> str | None:
        try:
            meta = self.threads.load(thread_id, recent_limit=0).meta
            return meta.discord_channel_id
        except (FileNotFoundError, json.JSONDecodeError, ValueError):
            return None

    def _log(self, event: str, **fields: object) -> None:
        if self.logger:
            self.logger.event("router", event, **fields)


async def classify_attachment(url: str) -> str:
    """Lightweight attachment classifier for future multimodal routing."""

    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.head(url, follow_redirects=True)
    return response.headers.get("content-type", "application/octet-stream")
