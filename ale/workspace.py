"""Shared workspaces for Linguist and Engineer teams."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ale.models import utc_now_iso
from ale.observability import EventLogger


SAFE_NAME_RE = re.compile(r"^[a-zA-Z0-9_.\-/]+$")


def safe_slug(value: str, fallback: str = "workspace") -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.lower()).strip("-")
    return slug[:80] or fallback


class WorkspaceStore:
    """A persistent shared directory and team chat for research/engineering work."""

    def __init__(self, root: Path, logger: EventLogger | None = None) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.logger = logger

    def create(self, title: str, *, kind: str, lead: str) -> str:
        base = safe_slug(title)
        workspace_id = base
        suffix = 2
        while self.path_for(workspace_id).exists():
            workspace_id = f"{base}-{suffix}"
            suffix += 1
        path = self.path_for(workspace_id)
        (path / "files").mkdir(parents=True)
        (path / "transcripts").mkdir()
        meta = {
            "workspace_id": workspace_id,
            "title": title,
            "kind": kind,
            "lead": lead,
            "created_at": utc_now_iso(),
            "status": "active",
        }
        self._write_json(path / "meta.json", meta)
        (path / "team_chat.jsonl").touch()
        self._log("created", workspace_id=workspace_id, title=title, kind=kind, lead=lead)
        return workspace_id

    def path_for(self, workspace_id: str) -> Path:
        return self.root / safe_slug(workspace_id)

    def append_chat(self, workspace_id: str, *, author: str, message: str) -> None:
        payload = {"ts": utc_now_iso(), "author": author, "message": message}
        path = self.path_for(workspace_id) / "team_chat.jsonl"
        if not path.exists():
            raise FileNotFoundError(f"Unknown workspace {workspace_id}")
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self._log("team_chat_appended", workspace_id=workspace_id, author=author)

    def write_file(self, workspace_id: str, relative_path: str, content: str) -> Path:
        path = self._file_path(workspace_id, relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(content.rstrip() + "\n", encoding="utf-8")
        tmp.replace(path)
        self._log("file_written", workspace_id=workspace_id, path=str(relative_path))
        return path

    def read_file(self, workspace_id: str, relative_path: str) -> str:
        path = self._file_path(workspace_id, relative_path)
        content = path.read_text(encoding="utf-8")
        self._log("file_read", workspace_id=workspace_id, path=str(relative_path))
        return content

    def list_files(self, workspace_id: str) -> list[str]:
        base = self.path_for(workspace_id) / "files"
        if not base.exists():
            raise FileNotFoundError(f"Unknown workspace {workspace_id}")
        files = sorted(str(path.relative_to(base)) for path in base.rglob("*") if path.is_file())
        self._log("files_listed", workspace_id=workspace_id, count=len(files))
        return files

    def list_workspaces(self) -> list[dict[str, Any]]:
        workspaces = []
        for meta_path in sorted(self.root.glob("*/meta.json")):
            workspaces.append(json.loads(meta_path.read_text(encoding="utf-8")))
        self._log("workspaces_listed", count=len(workspaces))
        return workspaces

    def _file_path(self, workspace_id: str, relative_path: str) -> Path:
        if not SAFE_NAME_RE.match(relative_path) or relative_path.startswith("/"):
            raise ValueError("Workspace paths must be relative and contain safe characters")
        base = (self.path_for(workspace_id) / "files").resolve()
        path = (base / relative_path).resolve()
        if base not in path.parents and path != base:
            raise ValueError("Workspace path escapes files directory")
        return path

    def _write_json(self, path: Path, payload: dict[str, Any]) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)

    def _log(self, event: str, **fields: Any) -> None:
        if self.logger:
            self.logger.event("workspace", event, **fields)
