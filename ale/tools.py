"""Ale's narrow in-process MCP tool surface."""

from __future__ import annotations

import asyncio
import html
import json
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlparse

import httpx

from ale.codex_harness import (
    CodexExecRequest,
    CodexPermissionMode,
    CodexSandboxMode,
    build_codex_command,
)
from ale.config import Settings
from ale.memory import MemoryStore
from ale.observability import EventLogger
from ale.threads import ThreadStore
from ale.workspace import WorkspaceStore

if TYPE_CHECKING:  # pragma: no cover
    from ale.reload import ReloadManager

try:
    from claude_agent_sdk import ToolAnnotations, create_sdk_mcp_server, tool
except Exception:  # pragma: no cover - tests can run without SDK installed.
    ToolAnnotations = None  # type: ignore[assignment]
    create_sdk_mcp_server = None  # type: ignore[assignment]
    tool = None  # type: ignore[assignment]


def text_result(text: str, *, is_error: bool = False) -> dict[str, Any]:
    payload: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if is_error:
        payload["is_error"] = True
    return payload


def _missing_sdk_server() -> object:
    raise RuntimeError("claude-agent-sdk is required to create Ale's MCP tools")


def _looks_like_json(line: str) -> bool:
    line = line.strip()
    return line.startswith("{") and line.endswith("}")


def _required_str(args: dict[str, Any], key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value.strip()


def _public_http_url(raw: str) -> str:
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("url must be an http(s) URL")
    return raw


def _optional_str(args: dict[str, Any], key: str) -> str | None:
    value = args.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    return value


def _enum_arg(args: dict[str, Any], key: str, default: str, allowed: set[str]) -> str:
    value = args.get(key, default)
    if value not in allowed:
        raise ValueError(f"{key} must be one of {', '.join(sorted(allowed))}")
    return str(value)


def build_ale_mcp_server(
    *,
    settings: Settings,
    threads: ThreadStore,
    memory: MemoryStore,
    workspaces: WorkspaceStore,
    logger: EventLogger | None = None,
    discord_sender: Any | None = None,
    reload_manager: "ReloadManager | None" = None,
) -> object:
    """Build the in-process MCP server exposed to Claude."""

    if tool is None or create_sdk_mcp_server is None:
        return _missing_sdk_server()

    read_only = ToolAnnotations(readOnlyHint=True, destructiveHint=False) if ToolAnnotations else None
    write_tool = (
        ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False)
        if ToolAnnotations
        else None
    )
    open_world = ToolAnnotations(readOnlyHint=True, openWorldHint=True) if ToolAnnotations else None

    def log(event: str, **fields: Any) -> None:
        if logger:
            logger.event("tools", event, **fields)

    def team(event: str, **fields: Any) -> None:
        if logger:
            logger.team(event, **fields)

    @tool(
        "read_thread",
        "Read exact prior Discord turns from one known Ale thread. Use when recap is insufficient.",
        {
            "type": "object",
            "properties": {
                "thread_id": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
            "required": ["thread_id"],
        },
        annotations=read_only,
    )
    async def read_thread(args: dict[str, Any]) -> dict[str, Any]:
        log("read_thread_called", thread_id=args.get("thread_id"), limit=args.get("limit"))
        try:
            limit = int(args.get("limit", 20))
            messages = threads.read_messages(args["thread_id"], limit=limit)
            return text_result(threads.format_recent(messages) or "Thread exists but has no messages.")
        except Exception as exc:
            log("read_thread_failed", error_type=type(exc).__name__, error=str(exc))
            return text_result(f"Could not read thread: {exc}", is_error=True)

    @tool(
        "search_threads",
        "Search Ale thread titles and recaps. Use before read_thread when the thread is unknown.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20},
            },
            "required": ["query"],
        },
        annotations=read_only,
    )
    async def search_threads(args: dict[str, Any]) -> dict[str, Any]:
        log("search_threads_called", query=args.get("query"), limit=args.get("limit"))
        try:
            limit = int(args.get("limit", 10))
            matches = threads.search(args["query"], limit=limit)
            if not matches:
                return text_result("No matching threads.")
            return text_result(
                "\n".join(
                    f"- {item.thread_id}: {item.title} ({item.last_active})\n  {item.recap[:300]}"
                    for item in matches
                )
            )
        except Exception as exc:
            log("search_threads_failed", error_type=type(exc).__name__, error=str(exc))
            return text_result(f"Could not search threads: {exc}", is_error=True)

    @tool(
        "read_memory",
        "Read one durable Ale memory markdown file by name, or pass MEMORY.md if present.",
        {"name": str},
        annotations=read_only,
    )
    async def read_memory(args: dict[str, Any]) -> dict[str, Any]:
        log("read_memory_called", name=args.get("name"))
        try:
            return text_result(memory.read(args["name"]))
        except Exception as exc:
            log("read_memory_failed", name=args.get("name"), error_type=type(exc).__name__, error=str(exc))
            return text_result(f"Could not read memory: {exc}", is_error=True)

    @tool(
        "edit_memory",
        "Create or replace one durable Ale memory markdown file. Use only for stable facts or explicit user requests.",
        {"name": str, "content": str},
        annotations=write_tool,
    )
    async def edit_memory(args: dict[str, Any]) -> dict[str, Any]:
        log("edit_memory_called", name=args.get("name"), content_chars=len(str(args.get("content", ""))))
        try:
            path = memory.write(args["name"], args["content"])
            return text_result(f"Updated memory file {path.name}.")
        except Exception as exc:
            log("edit_memory_failed", name=args.get("name"), error_type=type(exc).__name__, error=str(exc))
            return text_result(f"Could not edit memory: {exc}", is_error=True)

    @tool(
        "create_workspace",
        "Create a persistent shared Workspace for Linguist or Engineer team work.",
        {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "kind": {"type": "string", "enum": ["linguist", "engineer", "mixed"]},
                "lead": {"type": "string"},
            },
            "required": ["title", "kind", "lead"],
        },
        annotations=write_tool,
    )
    async def create_workspace(args: dict[str, Any]) -> dict[str, Any]:
        log("create_workspace_called", title=args.get("title"), kind=args.get("kind"))
        try:
            workspace_id = workspaces.create(args["title"], kind=args["kind"], lead=args["lead"])
            team(
                "workspace_created",
                team=args.get("kind"),
                workspace_id=workspace_id,
                lead=args.get("lead"),
                title=args.get("title"),
            )
            return text_result(f"Created workspace {workspace_id}.")
        except Exception as exc:
            log("create_workspace_failed", error_type=type(exc).__name__, error=str(exc))
            return text_result(f"Could not create workspace: {exc}", is_error=True)

    @tool(
        "append_workspace_chat",
        "Append a message to a Workspace team-chat channel.",
        {"workspace_id": str, "author": str, "message": str},
        annotations=write_tool,
    )
    async def append_workspace_chat(args: dict[str, Any]) -> dict[str, Any]:
        log("append_workspace_chat_called", workspace_id=args.get("workspace_id"), author=args.get("author"))
        try:
            workspaces.append_chat(args["workspace_id"], author=args["author"], message=args["message"])
            team(
                "team_chat_message",
                workspace_id=args.get("workspace_id"),
                author=args.get("author"),
                message_chars=len(str(args.get("message", ""))),
                preview=str(args.get("message", ""))[:200],
            )
            return text_result("Workspace chat appended.")
        except Exception as exc:
            log("append_workspace_chat_failed", error_type=type(exc).__name__, error=str(exc))
            return text_result(f"Could not append workspace chat: {exc}", is_error=True)

    @tool(
        "write_workspace_file",
        "Write a file into a Workspace. Use for research notes, drafts, plans, diffs, and reviews.",
        {"workspace_id": str, "path": str, "content": str},
        annotations=write_tool,
    )
    async def write_workspace_file(args: dict[str, Any]) -> dict[str, Any]:
        log("write_workspace_file_called", workspace_id=args.get("workspace_id"), path=args.get("path"))
        try:
            path = workspaces.write_file(args["workspace_id"], args["path"], args["content"])
            return text_result(f"Wrote workspace file {path.name}.")
        except Exception as exc:
            log("write_workspace_file_failed", error_type=type(exc).__name__, error=str(exc))
            return text_result(f"Could not write workspace file: {exc}", is_error=True)

    @tool(
        "read_workspace_file",
        "Read a file from a Workspace.",
        {"workspace_id": str, "path": str},
        annotations=read_only,
    )
    async def read_workspace_file(args: dict[str, Any]) -> dict[str, Any]:
        log("read_workspace_file_called", workspace_id=args.get("workspace_id"), path=args.get("path"))
        try:
            return text_result(workspaces.read_file(args["workspace_id"], args["path"]))
        except Exception as exc:
            log("read_workspace_file_failed", error_type=type(exc).__name__, error=str(exc))
            return text_result(f"Could not read workspace file: {exc}", is_error=True)

    @tool(
        "list_workspace_files",
        "List files in a Workspace.",
        {"workspace_id": str},
        annotations=read_only,
    )
    async def list_workspace_files(args: dict[str, Any]) -> dict[str, Any]:
        log("list_workspace_files_called", workspace_id=args.get("workspace_id"))
        try:
            files = workspaces.list_files(args["workspace_id"])
            return text_result("\n".join(files) if files else "No workspace files.")
        except Exception as exc:
            log("list_workspace_files_failed", error_type=type(exc).__name__, error=str(exc))
            return text_result(f"Could not list workspace files: {exc}", is_error=True)

    @tool(
        "list_workspaces",
        "List active and historical Ale Workspaces.",
        {},
        annotations=read_only,
    )
    async def list_workspaces(args: dict[str, Any]) -> dict[str, Any]:
        log("list_workspaces_called")
        try:
            payload = workspaces.list_workspaces()
            return text_result(str(payload) if payload else "No workspaces.")
        except Exception as exc:
            log("list_workspaces_failed", error_type=type(exc).__name__, error=str(exc))
            return text_result(f"Could not list workspaces: {exc}", is_error=True)

    @tool(
        "web_fetch",
        "Fetch a web URL with a 32 KB text cap. Use for current external facts or cited research.",
        {"url": str},
        annotations=open_world,
    )
    async def web_fetch(args: dict[str, Any]) -> dict[str, Any]:
        log("web_fetch_called", url=args.get("url"))
        try:
            url = _public_http_url(_required_str(args, "url"))
            async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
                response = await client.get(url)
            response.raise_for_status()
            text = response.text[:32768]
            return text_result(
                f"URL: {response.url}\nStatus: {response.status_code}\n\n{html.unescape(text)}"
            )
        except (ValueError, httpx.HTTPStatusError, httpx.RequestError) as exc:
            log("web_fetch_failed", url=args.get("url"), error_type=type(exc).__name__, error=str(exc))
            return text_result(f"Could not fetch URL: {exc}", is_error=True)

    @tool(
        "dm_user",
        "Send a Discord DM through Ale's current Discord process. Use only when the user asked for it.",
        {"user_id": str, "message": str},
        annotations=write_tool,
    )
    async def dm_user(args: dict[str, Any]) -> dict[str, Any]:
        log("dm_user_called", user_id=args.get("user_id"), message_chars=len(str(args.get("message", ""))))
        if discord_sender is None:
            return text_result("Discord sender is unavailable in this process.", is_error=True)
        try:
            await discord_sender(int(args["user_id"]), str(args["message"]))
            return text_result("DM sent.")
        except Exception as exc:
            log("dm_user_failed", user_id=args.get("user_id"), error_type=type(exc).__name__, error=str(exc))
            return text_result(f"Could not send DM: {exc}", is_error=True)

    @tool(
        "fire_heartbeat",
        "Run one configured heartbeat by ID using ALE_HEARTBEAT_COMMAND. Use only on explicit request.",
        {"heartbeat_id": str},
        annotations=write_tool,
    )
    async def fire_heartbeat(args: dict[str, Any]) -> dict[str, Any]:
        log("fire_heartbeat_called", heartbeat_id=args.get("heartbeat_id"))
        command = [settings.heartbeat_command, args["heartbeat_id"]]
        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                command,
                cwd=settings.work_dir,
                text=True,
                capture_output=True,
                timeout=120,
                check=False,
            )
            output = (proc.stdout + proc.stderr)[-12000:]
            return text_result(f"Exit code: {proc.returncode}\n{output}")
        except Exception as exc:
            log("fire_heartbeat_failed", error_type=type(exc).__name__, error=str(exc))
            return text_result(f"Could not fire heartbeat: {exc}", is_error=True)

    @tool(
        "bash",
        "Restricted shell in ALE_WORK_DIR. Disabled unless ALE_ENABLE_BASH_TOOL=true. No sudo.",
        {"command": str},
        annotations=write_tool,
    )
    async def bash(args: dict[str, Any]) -> dict[str, Any]:
        log("bash_called", command=str(args.get("command", ""))[:500])
        if not settings.enable_bash_tool:
            return text_result("Ale bash tool is disabled by configuration.", is_error=True)
        try:
            command = _required_str(args, "command")
            if "sudo" in command.split():
                return text_result("sudo is not allowed in Ale's restricted bash tool.", is_error=True)
            proc = await asyncio.to_thread(
                subprocess.run,
                command,
                cwd=Path(settings.work_dir),
                shell=True,
                text=True,
                capture_output=True,
                timeout=60,
                check=False,
            )
            output = (proc.stdout + proc.stderr)[-12000:]
            return text_result(f"Exit code: {proc.returncode}\n{output}")
        except subprocess.TimeoutExpired as exc:
            log("bash_failed", error_type=type(exc).__name__, error=str(exc))
            return text_result("Bash timed out after 60 seconds.", is_error=True)
        except (OSError, ValueError) as exc:
            log("bash_failed", error_type=type(exc).__name__, error=str(exc))
            return text_result(f"Bash failed: {exc}", is_error=True)

    @tool(
        "codex_exec",
        "Run a tightly-scoped codex exec implementation worker. Disabled unless ALE_ENABLE_CODEX_EXEC=true.",
        {
            "type": "object",
            "properties": {
                "task": {"type": "string"},
                "mode": {
                    "type": "string",
                    "enum": ["read_only", "workspace_write", "full_auto"],
                },
                "sandbox": {
                    "type": "string",
                    "enum": ["read-only", "workspace-write", "danger-full-access"],
                },
                "workspace_id": {"type": "string"},
                "output_schema_path": {"type": "string"},
                "ephemeral": {"type": "boolean"},
            },
            "required": ["task"],
        },
        annotations=write_tool,
    )
    async def codex_exec(args: dict[str, Any]) -> dict[str, Any]:
        log(
            "codex_exec_called",
            task_chars=len(str(args.get("task", ""))),
            mode=args.get("mode", "workspace_write"),
            sandbox=args.get("sandbox", "workspace-write"),
            workspace_id=args.get("workspace_id"),
        )
        if not settings.enable_codex_exec:
            return text_result("codex_exec is disabled by configuration.", is_error=True)
        try:
            request = CodexExecRequest(
                task=_required_str(args, "task"),
                mode=cast(
                    CodexPermissionMode,
                    _enum_arg(
                        args,
                        "mode",
                        "workspace_write",
                        {"read_only", "workspace_write", "full_auto"},
                    ),
                ),
                sandbox=cast(
                    CodexSandboxMode,
                    _enum_arg(
                        args,
                        "sandbox",
                        "workspace-write",
                        {"read-only", "workspace-write", "danger-full-access"},
                    ),
                ),
                workspace_id=_optional_str(args, "workspace_id"),
                output_schema_path=_optional_str(args, "output_schema_path"),
                ephemeral=bool(args.get("ephemeral", True)),
            )
            team(
                "codex_worker_dispatched",
                team="engineer",
                mode=request.mode,
                sandbox=request.sandbox,
                workspace_id=request.workspace_id,
                task_preview=request.task[:240],
                task_chars=len(request.task),
                ephemeral=request.ephemeral,
            )
            transcript_workspace_id = request.workspace_id
            output_path = settings.logs_dir / "codex-last-message.md"
            output_path.parent.mkdir(parents=True, exist_ok=True)
            command = build_codex_command(settings.codex_command, request, output_path)
            proc = await asyncio.to_thread(
                subprocess.run,
                command,
                cwd=Path(settings.work_dir),
                text=True,
                capture_output=True,
                timeout=settings.codex_timeout_seconds,
                check=False,
            )
            stdout = proc.stdout[-40000:]
            stderr = proc.stderr[-40000:]
            final_message = output_path.read_text(encoding="utf-8") if output_path.exists() else ""
            event_count = sum(1 for line in stdout.splitlines() if _looks_like_json(line))
            log(
                "codex_exec_completed",
                exit_code=proc.returncode,
                event_count=event_count,
                final_message_chars=len(final_message),
                stderr_chars=len(stderr),
            )
            team(
                "codex_worker_completed",
                team="engineer",
                workspace_id=transcript_workspace_id,
                exit_code=proc.returncode,
                event_count=event_count,
                final_message_chars=len(final_message),
                stderr_chars=len(stderr),
            )
            if transcript_workspace_id:
                workspaces.write_file(
                    transcript_workspace_id,
                    "codex/latest-final-message.md",
                    final_message or "(no final message)",
                )
                workspaces.write_file(
                    transcript_workspace_id,
                    "codex/latest-events.jsonl",
                    stdout or "",
                )
                workspaces.write_file(
                    transcript_workspace_id,
                    "codex/latest-stderr.log",
                    stderr or "",
                )
            response = {
                "exit_code": proc.returncode,
                "event_count": event_count,
                "final_message": final_message,
                "stderr_tail": stderr[-12000:],
            }
            return text_result(json.dumps(response, ensure_ascii=False, indent=2), is_error=proc.returncode != 0)
        except subprocess.TimeoutExpired as exc:
            log("codex_exec_failed", error_type=type(exc).__name__, error=str(exc))
            team("codex_worker_failed", team="engineer", error_type=type(exc).__name__, error=str(exc))
            return text_result(
                f"codex_exec timed out after {settings.codex_timeout_seconds} seconds.",
                is_error=True,
            )
        except (OSError, ValueError) as exc:
            log("codex_exec_failed", error_type=type(exc).__name__, error=str(exc))
            team("codex_worker_failed", team="engineer", error_type=type(exc).__name__, error=str(exc))
            return text_result(f"codex_exec failed: {exc}", is_error=True)

    @tool(
        "reload_plan",
        "Read-only inspection of Ale's hot-reload state: changed files, last reload events, tier config.",
        {"type": "object", "properties": {}},
        annotations=read_only,
    )
    async def reload_plan(args: dict[str, Any]) -> dict[str, Any]:
        log("reload_plan_called")
        if reload_manager is None:
            return text_result("Reload manager is not available in this process.", is_error=True)
        try:
            plan = reload_manager.plan()
            return text_result(json.dumps(plan, indent=2, default=str))
        except Exception as exc:
            log("reload_plan_failed", error_type=type(exc).__name__, error=str(exc))
            return text_result(f"Could not build reload plan: {exc}", is_error=True)

    @tool(
        "reload_config_and_prompts",
        "Tier 1 hot reload. Re-read .env and persona/system prompts. Applies to next turn.",
        {
            "type": "object",
            "properties": {"reason": {"type": "string"}},
        },
        annotations=write_tool,
    )
    async def reload_config_and_prompts(args: dict[str, Any]) -> dict[str, Any]:
        log("reload_config_called", reason=args.get("reason"))
        if reload_manager is None:
            return text_result("Reload manager is not available in this process.", is_error=True)
        result = await reload_manager.reload_config_and_prompts(
            reason=str(args.get("reason") or "engineer_tool")
        )
        payload = {
            "reload_id": result.reload_id,
            "ok": result.ok,
            "duration_ms": result.duration_ms,
            "changed_file_count": len(result.changed_files),
            "error": result.error,
        }
        return text_result(json.dumps(payload, indent=2), is_error=not result.ok)

    @tool(
        "reload_tools",
        "Tier 2 hot reload. Re-import ale.tools so MCP tool definitions on next agent call are fresh.",
        {
            "type": "object",
            "properties": {"reason": {"type": "string"}},
        },
        annotations=write_tool,
    )
    async def reload_tools_handler(args: dict[str, Any]) -> dict[str, Any]:
        log("reload_tools_called", reason=args.get("reason"))
        if reload_manager is None:
            return text_result("Reload manager is not available in this process.", is_error=True)
        result = await reload_manager.reload_tools(
            reason=str(args.get("reason") or "engineer_tool")
        )
        payload = {
            "reload_id": result.reload_id,
            "ok": result.ok,
            "duration_ms": result.duration_ms,
            "error": result.error,
        }
        return text_result(json.dumps(payload, indent=2), is_error=not result.ok)

    @tool(
        "candidate_health_check",
        "Tier 3 hot reload. Spawn a candidate process to run `ale health-check` and capture the result.",
        {
            "type": "object",
            "properties": {"reason": {"type": "string"}},
        },
        annotations=write_tool,
    )
    async def candidate_health_check(args: dict[str, Any]) -> dict[str, Any]:
        log("candidate_health_check_called", reason=args.get("reason"))
        if reload_manager is None:
            return text_result("Reload manager is not available in this process.", is_error=True)
        result = await reload_manager.health_check_candidate(
            reason=str(args.get("reason") or "engineer_tool")
        )
        payload = {
            "reload_id": result.reload_id,
            "ok": result.ok,
            "duration_ms": result.duration_ms,
            "error": result.error,
            "extra": result.extra,
        }
        return text_result(json.dumps(payload, indent=2, default=str), is_error=not result.ok)

    @tool(
        "promote_candidate",
        "Re-exec the current process so the new code becomes live. Requires ALE_ENABLE_SELF_PROMOTE=true.",
        {
            "type": "object",
            "properties": {"reason": {"type": "string"}},
        },
        annotations=write_tool,
    )
    async def promote_candidate(args: dict[str, Any]) -> dict[str, Any]:
        log("promote_candidate_called", reason=args.get("reason"))
        if reload_manager is None:
            return text_result("Reload manager is not available in this process.", is_error=True)
        result = await reload_manager.promote_candidate(
            reason=str(args.get("reason") or "engineer_tool")
        )
        payload = {
            "reload_id": result.reload_id,
            "ok": result.ok,
            "error": result.error,
        }
        return text_result(json.dumps(payload, indent=2), is_error=not result.ok)

    @tool(
        "rollback_reload",
        "Restore the previous Settings/prompt/tool snapshot. Use after a regression.",
        {
            "type": "object",
            "properties": {"reason": {"type": "string"}},
        },
        annotations=write_tool,
    )
    async def rollback_reload(args: dict[str, Any]) -> dict[str, Any]:
        log("rollback_reload_called", reason=args.get("reason"))
        if reload_manager is None:
            return text_result("Reload manager is not available in this process.", is_error=True)
        result = await reload_manager.rollback(
            reason=str(args.get("reason") or "engineer_tool")
        )
        payload = {
            "reload_id": result.reload_id,
            "ok": result.ok,
            "rolled_back": result.rolled_back,
            "error": result.error,
        }
        return text_result(json.dumps(payload, indent=2), is_error=not result.ok)

    tool_list = [
        read_thread,
        search_threads,
        read_memory,
        edit_memory,
        create_workspace,
        append_workspace_chat,
        write_workspace_file,
        read_workspace_file,
        list_workspace_files,
        list_workspaces,
        web_fetch,
        dm_user,
        fire_heartbeat,
        bash,
        codex_exec,
    ]
    if settings.enable_reload_tools:
        tool_list.extend(
            [
                reload_plan,
                reload_config_and_prompts,
                reload_tools_handler,
                candidate_health_check,
                promote_candidate,
                rollback_reload,
            ]
        )
    return create_sdk_mcp_server(name="ale", version="0.1.0", tools=tool_list)
