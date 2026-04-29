"""Research/engineering artifact storage and optional publishing."""

from __future__ import annotations

import asyncio
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path


URL_RE = re.compile(r"https?://\S+")


@dataclass(frozen=True)
class PublishResult:
    url: str | None
    stdout: str
    stderr: str
    exit_code: int


def should_send_as_artifact(text: str, persona: str, threshold: int) -> bool:
    return persona in {"linguist", "engineer"} and len(text) >= threshold


def write_markdown_artifact(
    *,
    artifacts_dir: Path,
    thread_id: str,
    discord_message_id: str | None,
    title: str,
    content: str,
) -> Path:
    artifact_dir = artifacts_dir / thread_id
    artifact_dir.mkdir(parents=True, exist_ok=True)
    file_stem = discord_message_id or "latest"
    path = artifact_dir / f"{file_stem}.md"
    path.write_text(f"# {title}\n\n{content.strip()}\n", encoding="utf-8")
    return path


def build_publish_command(command_template: str, artifact_path: Path) -> list[str]:
    parts = shlex.split(command_template)
    path_value = str(artifact_path)
    if any("{path}" in part for part in parts):
        return [part.replace("{path}", path_value) for part in parts]
    return [*parts, path_value]


async def publish_artifact(
    *,
    command_template: str | None,
    artifact_path: Path,
    timeout_seconds: int,
) -> PublishResult | None:
    if not command_template:
        return None
    command = build_publish_command(command_template, artifact_path)
    proc = await asyncio.to_thread(
        subprocess.run,
        command,
        text=True,
        capture_output=True,
        timeout=timeout_seconds,
        check=False,
    )
    stdout = proc.stdout.strip()
    stderr = proc.stderr.strip()
    match = URL_RE.search(stdout)
    return PublishResult(
        url=match.group(0) if match else None,
        stdout=stdout,
        stderr=stderr,
        exit_code=proc.returncode,
    )
