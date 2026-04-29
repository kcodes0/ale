import dataclasses
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from ale.agent import _fallback_summary
from ale.artifacts import build_publish_command, should_send_as_artifact, write_markdown_artifact
from ale.config import Settings
from ale.discord_app import DISCORD_COMMAND_SCHEMA_VERSION, AleDiscordClient, split_for_discord
from ale.models import RouteDecision
from ale.reports import render_report_pdf


def test_split_for_discord_keeps_chunks_under_limit():
    chunks = list(split_for_discord("a" * 12, 5))

    assert chunks == ["aaaaa", "aaaaa", "aa"]


def test_should_send_as_artifact_only_for_long_specialist_outputs():
    assert should_send_as_artifact("x" * 2500, "linguist", 2400)
    assert should_send_as_artifact("x" * 2500, "engineer", 2400)
    assert not should_send_as_artifact("x" * 2500, "actor", 2400)
    assert not should_send_as_artifact("short", "linguist", 2400)


def test_write_markdown_artifact(tmp_path):
    path = write_markdown_artifact(
        artifacts_dir=tmp_path,
        thread_id="research",
        discord_message_id="123",
        title="Research",
        content="Body",
    )

    assert path.name == "123.md"
    assert path.read_text(encoding="utf-8") == "# Research\n\nBody\n"


def test_build_publish_command_replaces_path_placeholder(tmp_path):
    artifact = tmp_path / "out.md"

    command = build_publish_command("publish-doc --file {path}", artifact)

    assert command == ["publish-doc", "--file", str(artifact)]


def test_build_publish_command_appends_path_without_placeholder(tmp_path):
    artifact = tmp_path / "out.md"

    command = build_publish_command("publish-doc --public", artifact)

    assert command == ["publish-doc", "--public", str(artifact)]


def test_render_report_pdf_handles_markdown_and_unicode(tmp_path: Path):
    output = tmp_path / "report.pdf"

    result = render_report_pdf(
        title="Hot Reload Smoke",
        body=(
            "# Summary\n\n"
            "Pass — all six checkpoints clean.\n\n"
            "## Bullets\n\n"
            "- Tier 1 reload_id: abc, duration_ms: 5\n"
            "- Tier 2 reload_id: def, duration_ms: 4\n\n"
            "## Code\n\n"
            "```python\nmanager.reload_tools()\n```\n"
        ),
        output_path=output,
        metadata={"thread": "smoke", "ts": "2026-04-28T06:30Z"},
    )

    assert result.path == output
    assert output.exists()
    assert result.bytes_written > 0
    assert result.page_count >= 1
    header = output.read_bytes()[:5]
    assert header.startswith(b"%PDF-")


def test_fallback_summary_truncates_and_marks_unavailable():
    text = "First paragraph.\n\nSecond paragraph that goes on and on " * 50

    summary = _fallback_summary(text, max_chars=400)

    assert len(summary) <= 400
    assert "Actor summary unavailable" in summary


def test_fallback_summary_handles_empty_text():
    assert "PDF is empty" in _fallback_summary("")


def test_route_decision_replace_overrides_persona():
    """Guards the /engineer slash command path: RouteDecision is frozen, so
    the handler must use dataclasses.replace, not in-place mutation, to force
    the engineer persona after routing."""

    route = RouteDecision(
        thread_id="t1", persona="actor", confidence=0.6, reason="x", is_new=False
    )
    overridden = dataclasses.replace(route, persona="engineer")

    assert overridden.persona == "engineer"
    assert route.persona == "actor"
    with pytest.raises(dataclasses.FrozenInstanceError):
        route.persona = "engineer"  # type: ignore[misc]


async def test_engineer_slash_command_rejects_disallowed_user():
    """Allowlist must block before the handler defers or runs the agent."""

    from ale.discord_app import AleDiscordClient

    client = AleDiscordClient.__new__(AleDiscordClient)
    client.settings = Settings(
        discord_token=None,
        anthropic_api_key=None,
        allowed_user_ids=frozenset({42}),
    )
    client.runtime = MagicMock()
    client.runtime.logger = MagicMock()

    interaction = MagicMock()
    interaction.user.id = 999
    interaction.channel_id = 1
    interaction.response.send_message = AsyncMock()
    interaction.response.defer = AsyncMock()

    await AleDiscordClient._handle_engineer_command(client, interaction, "do a thing")

    interaction.response.send_message.assert_awaited_once()
    assert interaction.response.send_message.await_args is not None
    args, kwargs = interaction.response.send_message.await_args
    assert "Not authorized" in args[0]
    assert kwargs.get("ephemeral") is True
    interaction.response.defer.assert_not_awaited()


async def test_slash_command_sync_auto_skips_when_marker_is_current(tmp_path):
    client = AleDiscordClient.__new__(AleDiscordClient)
    client.settings = Settings(
        discord_token=None,
        anthropic_api_key=None,
        state_dir=tmp_path,
        discord_sync_commands="auto",
    )
    client.runtime = MagicMock()
    client.runtime.logger = MagicMock()
    client.tree = MagicMock()
    client.tree.sync = AsyncMock()
    (tmp_path / "discord-commands.json").write_text(
        f'{{"schema_version": "{DISCORD_COMMAND_SCHEMA_VERSION}"}}\n',
        encoding="utf-8",
    )

    await AleDiscordClient._sync_slash_commands_if_needed(client)

    client.tree.sync.assert_not_awaited()


async def test_slash_command_sync_auto_writes_marker_when_missing(tmp_path):
    client = AleDiscordClient.__new__(AleDiscordClient)
    client.settings = Settings(
        discord_token=None,
        anthropic_api_key=None,
        state_dir=tmp_path,
        discord_sync_commands="auto",
    )
    client.runtime = MagicMock()
    client.runtime.logger = MagicMock()
    client.tree = MagicMock()
    client.tree.sync = AsyncMock(return_value=[object()])

    await AleDiscordClient._sync_slash_commands_if_needed(client)

    client.tree.sync.assert_awaited_once()
    assert DISCORD_COMMAND_SCHEMA_VERSION in (tmp_path / "discord-commands.json").read_text(
        encoding="utf-8"
    )
