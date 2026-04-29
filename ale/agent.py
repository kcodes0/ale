"""Claude Agent SDK orchestration for Ale."""

from __future__ import annotations

import asyncio
import importlib
import os
import time
import uuid
from dataclasses import dataclass
from collections.abc import Awaitable, Callable
from typing import Any

from ale.config import Settings
from ale.memory import MemoryStore
from ale.models import ChatMessage, RouteDecision, ThreadState
from ale.observability import EventLogger
from ale.reload import ReloadManager
from ale.threads import ThreadStore
from ale.workspace import WorkspaceStore

try:
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ResultMessage,
        TextBlock,
        ToolUseBlock,
        query,
    )
except Exception:  # pragma: no cover - tests can import without SDK installed.
    AssistantMessage = None  # type: ignore[assignment]
    ClaudeAgentOptions = None  # type: ignore[assignment]
    ResultMessage = None  # type: ignore[assignment]
    TextBlock = None  # type: ignore[assignment]
    ToolUseBlock = None  # type: ignore[assignment]
    query = None  # type: ignore[assignment]


@dataclass(frozen=True)
class AgentResponse:
    text: str
    session_id: str | None = None
    usage: dict[str, Any] | None = None
    cost_usd: float | None = None
    tool_calls: tuple[str, ...] = ()
    turn_id: str = ""


class AleAgent:
    def __init__(
        self,
        *,
        reload_manager: ReloadManager,
        threads: ThreadStore,
        memory: MemoryStore,
        workspaces: WorkspaceStore,
        logger: EventLogger,
        discord_sender: Any | None = None,
    ) -> None:
        self.reload_manager = reload_manager
        self.threads = threads
        self.memory = memory
        self.workspaces = workspaces
        self.logger = logger
        self.discord_sender = discord_sender

    @property
    def settings(self) -> Settings:
        return self.reload_manager.settings

    async def respond(
        self,
        *,
        state: ThreadState,
        user_message: ChatMessage,
        route: RouteDecision,
        progress_callback: Callable[[str], Awaitable[None]] | None = None,
        parent_turn_id: str | None = None,
    ) -> AgentResponse:
        if query is None or ClaudeAgentOptions is None:
            raise RuntimeError("claude-agent-sdk is not installed. Run `uv sync` first.")

        snapshot = self.reload_manager.snapshot
        settings = snapshot.settings
        turn_id = uuid.uuid4().hex[:12]
        started_at = time.monotonic()

        if settings.anthropic_api_key:
            os.environ["ANTHROPIC_API_KEY"] = settings.anthropic_api_key

        # Re-import personas via the snapshot so prompt edits show up here too.
        import ale.personas as personas_module

        personas = personas_module.PERSONAS
        persona = personas.get(route.persona, personas["actor"])
        agent_role = self._infer_role(persona.name)
        team_label = self._team_for(persona.name)

        self.logger.event(
            "agent",
            "turn_started",
            turn_id=turn_id,
            parent_turn_id=parent_turn_id,
            thread_id=state.meta.thread_id,
            persona=persona.name,
            agent_role=agent_role,
            team=team_label,
            model=self._model_for(persona.name),
            discord_message_id=user_message.discord_message_id,
            recap_chars=len(state.recap or ""),
            recent_message_count=len(state.messages),
            content_chars=len(user_message.content),
            attachment_count=len(user_message.attachments),
            reload_snapshot_at=snapshot.captured_at,
        )
        if persona.name in {"linguist", "engineer"}:
            self.logger.team(
                "turn_started",
                turn_id=turn_id,
                parent_turn_id=parent_turn_id,
                thread_id=state.meta.thread_id,
                persona=persona.name,
                agent_role=agent_role,
                team=team_label,
                model=self._model_for(persona.name),
                lead=persona.name.title(),
                content_preview=user_message.content[:240],
            )

        prompt = self._build_prompt(
            state=state,
            user_message=user_message,
            persona_name=persona.name,
            snapshot_personas=personas,
        )
        options = self._build_options(persona.name, snapshot=snapshot)
        tool_calls: list[str] = []
        tool_started_at: dict[str, float] = {}
        text_chunks: list[str] = []
        result: Any | None = None

        self.reload_manager.turn_started()
        try:
            try:
                async for message in query(prompt=prompt, options=options):
                    if AssistantMessage is not None and isinstance(message, AssistantMessage):
                        for block in message.content:
                            if TextBlock is not None and isinstance(block, TextBlock):
                                text_chunks.append(block.text)
                            elif ToolUseBlock is not None and isinstance(block, ToolUseBlock):
                                tool_calls.append(block.name)
                                tool_started_at[f"{block.name}#{len(tool_calls)}"] = time.monotonic()
                                args_summary = _summarize_tool_args(getattr(block, "input", None))
                                if progress_callback and persona.name in {"linguist", "engineer"}:
                                    await progress_callback(
                                        specialist_progress_message(
                                            persona.name, block.name, len(tool_calls)
                                        )
                                    )
                                self.logger.event(
                                    "agent",
                                    "tool_requested",
                                    turn_id=turn_id,
                                    thread_id=state.meta.thread_id,
                                    persona=persona.name,
                                    agent_role=agent_role,
                                    team=team_label,
                                    tool_index=len(tool_calls),
                                    tool_name=block.name,
                                    tool_args_summary=args_summary,
                                )
                                if persona.name in {"linguist", "engineer"}:
                                    self.logger.team(
                                        "tool_requested",
                                        turn_id=turn_id,
                                        thread_id=state.meta.thread_id,
                                        team=team_label,
                                        tool_name=block.name,
                                        tool_index=len(tool_calls),
                                        tool_args_summary=args_summary,
                                    )
                    if ResultMessage is not None and isinstance(message, ResultMessage):
                        result = message
            except Exception as exc:
                duration_ms = int((time.monotonic() - started_at) * 1000)
                self.logger.exception(
                    "agent",
                    "turn_failed",
                    exc,
                    turn_id=turn_id,
                    thread_id=state.meta.thread_id,
                    persona=persona.name,
                    agent_role=agent_role,
                    team=team_label,
                    duration_ms=duration_ms,
                    tool_call_count=len(tool_calls),
                    in_flight_turns=self.reload_manager.in_flight_turns,
                    last_reload_id=(
                        self.reload_manager.last_result.reload_id
                        if self.reload_manager.last_result
                        else None
                    ),
                    last_reload_tier=(
                        self.reload_manager.last_result.tier
                        if self.reload_manager.last_result
                        else None
                    ),
                )
                if persona.name in {"linguist", "engineer"}:
                    self.logger.team(
                        "turn_failed",
                        turn_id=turn_id,
                        thread_id=state.meta.thread_id,
                        team=team_label,
                        error_type=type(exc).__name__,
                        error=str(exc),
                        duration_ms=duration_ms,
                    )
                raise

            response_text = (getattr(result, "result", None) or "\n".join(text_chunks)).strip()
            duration_ms = int((time.monotonic() - started_at) * 1000)
            response = AgentResponse(
                text=response_text,
                session_id=getattr(result, "session_id", None),
                usage=getattr(result, "usage", None),
                cost_usd=getattr(result, "total_cost_usd", None),
                tool_calls=tuple(tool_calls),
                turn_id=turn_id,
            )
            self.logger.turn(
                turn_id=turn_id,
                parent_turn_id=parent_turn_id,
                thread_id=state.meta.thread_id,
                discord_message_id=user_message.discord_message_id,
                persona=persona.name,
                agent_role=agent_role,
                team=team_label,
                model=self._model_for(persona.name),
                session_id=response.session_id,
                tool_calls=list(response.tool_calls),
                tool_call_count=len(response.tool_calls),
                usage=response.usage,
                cost_usd_estimate=response.cost_usd,
                route_confidence=route.confidence,
                route_reason=route.reason,
                response_chars=len(response.text),
                duration_ms=duration_ms,
            )
            self.logger.event(
                "agent",
                "turn_completed",
                turn_id=turn_id,
                thread_id=state.meta.thread_id,
                persona=persona.name,
                agent_role=agent_role,
                team=team_label,
                tool_call_count=len(tool_calls),
                response_chars=len(response.text),
                duration_ms=duration_ms,
                cost_usd_estimate=response.cost_usd,
            )
            if persona.name in {"linguist", "engineer"}:
                self.logger.team(
                    "turn_completed",
                    turn_id=turn_id,
                    thread_id=state.meta.thread_id,
                    team=team_label,
                    lead=persona.name.title(),
                    tool_call_count=len(tool_calls),
                    response_chars=len(response.text),
                    duration_ms=duration_ms,
                    cost_usd_estimate=response.cost_usd,
                    tool_sequence=list(response.tool_calls),
                )
            return response
        finally:
            self.reload_manager.turn_ended()

    def _build_options(self, persona: str, *, snapshot: Any) -> Any:
        from ale.tools import build_ale_mcp_server

        # If hot_reload_tools is on, the module may have been reloaded since boot.
        # Fetch a fresh callable from the live module to ensure we use the new code.
        if snapshot.settings.hot_reload_tools:
            import ale.tools as tools_module

            build_ale_mcp_server = importlib.import_module(tools_module.__name__).build_ale_mcp_server

        mcp_server = build_ale_mcp_server(
            settings=snapshot.settings,
            threads=self.threads,
            memory=self.memory,
            workspaces=self.workspaces,
            logger=self.logger,
            discord_sender=self.discord_sender,
            reload_manager=self.reload_manager,
        )
        base_allowed = [
            "mcp__ale__read_thread",
            "mcp__ale__search_threads",
            "mcp__ale__read_memory",
            "mcp__ale__edit_memory",
            "mcp__ale__web_fetch",
            "mcp__ale__dm_user",
            "mcp__ale__fire_heartbeat",
            "mcp__ale__bash",
            "mcp__ale__create_workspace",
            "mcp__ale__append_workspace_chat",
            "mcp__ale__write_workspace_file",
            "mcp__ale__read_workspace_file",
            "mcp__ale__list_workspace_files",
            "mcp__ale__list_workspaces",
            "mcp__ale__codex_exec",
        ]
        if snapshot.settings.enable_reload_tools:
            base_allowed.extend(
                [
                    "mcp__ale__reload_plan",
                    "mcp__ale__reload_config_and_prompts",
                    "mcp__ale__reload_tools",
                    "mcp__ale__candidate_health_check",
                    "mcp__ale__promote_candidate",
                    "mcp__ale__rollback_reload",
                ]
            )
        if persona == "engineer":
            tools = ["Read", "Write", "Edit", "Glob", "Grep", "Bash", "Agent"]
            allowed = [*base_allowed, "Read", "Write", "Edit", "Glob", "Grep", "Bash", "Agent"]
            permission_mode = "acceptEdits"
        elif persona == "linguist":
            tools = ["WebFetch", "WebSearch", "Agent"]
            allowed = [*base_allowed, "WebFetch", "WebSearch", "Agent"]
            permission_mode = "dontAsk"
        else:
            tools = ["WebFetch", "WebSearch", "Agent"]
            allowed = [*base_allowed, "WebFetch", "WebSearch", "Agent"]
            permission_mode = "dontAsk"

        return ClaudeAgentOptions(
            tools=tools,
            allowed_tools=allowed,
            permission_mode=permission_mode,
            mcp_servers={"ale": mcp_server},
            agents=snapshot.sdk_subagents_factory(),
            model=self._model_for(persona),
            cwd=snapshot.settings.work_dir,
            max_turns=snapshot.settings.max_turns,
            max_budget_usd=snapshot.settings.max_budget_usd,
            setting_sources=[],
            system_prompt=snapshot.base_system_prompt,
        )

    def _model_for(self, persona: str) -> str:
        settings = self.settings
        if persona == "engineer":
            return settings.engineer_model
        if persona == "linguist":
            return settings.linguist_model
        return settings.actor_model

    def _team_for(self, persona: str) -> str:
        if persona in {"linguist", "engineer"}:
            return persona
        return "actor"

    def _infer_role(self, persona: str) -> str:
        if persona == "linguist":
            return "lead-linguist"
        if persona == "engineer":
            return "lead-engineer"
        return "actor"

    def _build_prompt(
        self,
        *,
        state: ThreadState,
        user_message: ChatMessage,
        persona_name: str,
        snapshot_personas: dict[str, Any],
    ) -> str:
        memory_index = self.memory.index()
        recent = self.threads.format_recent(state.messages)
        attachments = "\n".join(
            f"- {item.get('filename') or item.get('url')}: {item.get('url')}"
            for item in user_message.attachments
        )
        persona = snapshot_personas[persona_name]
        return f"""\
<persona>
{persona.system_prompt}
</persona>

<thread>
id: {state.meta.thread_id}
title: {state.meta.title}
persona: {state.meta.persona}
message_count: {state.meta.message_count}
</thread>

<recap>
{state.recap or "No recap yet."}
</recap>

<recent_turns>
{recent or "No previous turns in this thread."}
</recent_turns>

<memory_index>
{memory_index}
</memory_index>

<incoming_message>
author: {user_message.author_name or user_message.author_id or "user"}
timestamp: {user_message.ts}
content:
{user_message.content}
</incoming_message>

<attachments>
{attachments or "None"}
</attachments>
"""


async def maybe_refresh_recap(
    *,
    route_refresh: Any,
    settings: Settings,
    state: ThreadState,
) -> None:
    if settings.recap_every_turns <= 0:
        return
    if state.meta.message_count and state.meta.message_count % settings.recap_every_turns == 0:
        await asyncio.create_task(route_refresh(state.meta.thread_id))


def specialist_progress_message(persona: str, tool_name: str, tool_count: int) -> str:
    if persona == "linguist":
        if tool_count == 1:
            return "Linguist is checking sources now."
        if tool_count == 4:
            return "Linguist is cross-checking the useful bits."
        return f"Linguist is still working; latest step used `{tool_name}`."
    if tool_count == 1:
        return "Engineer is inspecting the system now."
    if tool_count == 4:
        return "Engineer is narrowing this into an actionable fix."
    return f"Engineer is still working; latest step used `{tool_name}`."


def _summarize_tool_args(args: Any, *, max_len: int = 200) -> str:
    """Compact preview of a tool input for the agent log."""

    if args is None:
        return ""
    if isinstance(args, dict):
        bits: list[str] = []
        for key, value in args.items():
            if isinstance(value, str):
                preview = value[:80] + ("…" if len(value) > 80 else "")
            elif isinstance(value, (int, float, bool)) or value is None:
                preview = repr(value)
            elif isinstance(value, (list, tuple)):
                preview = f"[{len(value)} items]"
            elif isinstance(value, dict):
                preview = f"{{{len(value)} keys}}"
            else:
                preview = type(value).__name__
            bits.append(f"{key}={preview}")
        rendered = ", ".join(bits)
        return rendered[:max_len] + ("…" if len(rendered) > max_len else "")
    return repr(args)[:max_len]
