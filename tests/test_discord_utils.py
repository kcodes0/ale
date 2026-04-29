from ale.artifacts import build_publish_command, should_send_as_artifact, write_markdown_artifact
from ale.discord_app import split_for_discord


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
