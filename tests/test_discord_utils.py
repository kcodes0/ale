from pathlib import Path

from ale.agent import _fallback_summary
from ale.artifacts import build_publish_command, should_send_as_artifact, write_markdown_artifact
from ale.discord_app import split_for_discord
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
