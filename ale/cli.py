"""Command-line helpers for local Ale operation."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from ale.config import load_settings
from ale.discord_app import run as run_discord
from ale.models import ChatMessage
from ale.service import AleRuntime


async def ask_once(prompt: str, *, thread_id: str | None = None) -> None:
    settings = load_settings()
    runtime = AleRuntime(settings)
    route = await runtime.router.route(prompt, discord_channel_id="cli", discord_user_id=None)
    if thread_id:
        route = type(route)(
            thread_id=thread_id,
            persona=runtime.router.infer_persona(prompt),
            confidence=1.0,
            reason="cli override",
            is_new=not runtime.threads.exists(thread_id),
        )
    async with runtime.threads.lock_for(route.thread_id):
        if route.is_new or not runtime.threads.exists(route.thread_id):
            state = runtime.threads.create(
                prompt.splitlines()[0][:80] or "CLI thread",
                thread_id=route.thread_id,
                discord_channel_id="cli",
                persona=route.persona,
            )
        else:
            state = runtime.threads.load(route.thread_id)
        message = ChatMessage(role="user", content=prompt, author_name="cli")
        runtime.threads.append_message(state.meta.thread_id, message)
        state = runtime.threads.load(state.meta.thread_id)
        response = await runtime.agent.respond(state=state, user_message=message, route=route)
        if response.text != "NO_REPLY":
            print(response.text)
            runtime.threads.append_message(
                state.meta.thread_id,
                ChatMessage(role="assistant", content=response.text),
            )


def health_check() -> int:
    """Boot a fresh AleRuntime + MCP tool server and exit.

    Used by Tier 3 hot reload as a smoke test for a candidate process. Returns
    0 if the harness imports, configures, and registers tools cleanly; non-zero
    otherwise. Output is JSON so callers can capture structured detail.
    """

    payload: dict[str, object] = {"ok": False, "stage": "load_settings"}
    try:
        settings = load_settings()
        payload["stage"] = "instantiate_runtime"
        runtime = AleRuntime(settings)
        payload["stage"] = "build_mcp_server"
        from ale.tools import build_ale_mcp_server

        build_ale_mcp_server(
            settings=runtime.reload_manager.settings,
            threads=runtime.threads,
            memory=runtime.memory,
            workspaces=runtime.workspaces,
            logger=runtime.logger,
            reload_manager=runtime.reload_manager,
        )
        payload.update(
            {
                "ok": True,
                "stage": "complete",
                "state_dir": str(settings.state_dir),
                "hot_reload_enabled": settings.hot_reload_enabled,
                "hot_reload_tools": settings.hot_reload_tools,
                "hot_reload_candidate": settings.hot_reload_candidate,
                "enable_reload_tools": settings.enable_reload_tools,
            }
        )
        runtime.logger.event("reload", "health_check_ok", **{
            k: v for k, v in payload.items() if k != "ok"
        })
        print(json.dumps(payload))
        return 0
    except Exception as exc:
        payload.update({"error": str(exc), "error_type": type(exc).__name__})
        print(json.dumps(payload))
        return 1


def main() -> None:
    parser = argparse.ArgumentParser(prog="ale")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("discord", help="Run the Discord gateway service")

    ask_parser = sub.add_parser("ask", help="Send one local prompt through Ale")
    ask_parser.add_argument("prompt")
    ask_parser.add_argument("--thread-id")

    sub.add_parser(
        "health-check",
        help="Boot the harness and exit. Used by Tier 3 candidate health checks.",
    )

    args = parser.parse_args()
    if args.command == "discord":
        run_discord()
    elif args.command == "ask":
        asyncio.run(ask_once(args.prompt, thread_id=args.thread_id))
    elif args.command == "health-check":
        sys.exit(health_check())


if __name__ == "__main__":
    main()
