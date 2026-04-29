from pathlib import Path

from ale.codex_harness import CodexExecRequest, build_codex_command, build_codex_prompt
from ale.agent import specialist_progress_message


def test_codex_prompt_wraps_task_with_worker_contract():
    prompt = build_codex_prompt(
        CodexExecRequest(task="Fix the failing router test.", workspace_id="router-fix")
    )

    assert "Ale Engineer's implementation worker" in prompt
    assert "Fix the failing router test." in prompt
    assert "Ale Workspace ID: router-fix" in prompt


def test_codex_command_uses_json_and_workspace_sandbox():
    command = build_codex_command(
        "codex",
        CodexExecRequest(task="Patch docs.", mode="workspace_write"),
        Path("/tmp/final.md"),
    )

    assert command[:3] == ["codex", "exec", "--json"]
    assert "-o" in command
    assert "--ephemeral" in command
    assert command[command.index("--sandbox") + 1] == "workspace-write"


def test_codex_full_auto_uses_full_auto_flag():
    command = build_codex_command(
        "codex",
        CodexExecRequest(task="Run tests and fix.", mode="full_auto"),
        Path("/tmp/final.md"),
    )

    assert "--full-auto" in command
    assert "--sandbox" not in command


def test_specialist_progress_messages_are_persona_specific():
    assert specialist_progress_message("linguist", "WebSearch", 1) == "Linguist is checking sources now."
    assert specialist_progress_message("engineer", "Read", 1) == "Engineer is inspecting the system now."
