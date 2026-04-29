"""Read and write Ale's durable semantic memory files."""

from __future__ import annotations

import re
from pathlib import Path

from ale.observability import EventLogger


MEMORY_NAME_RE = re.compile(r"^[a-zA-Z0-9_.-]+(?:\.md)?$")


class MemoryStore:
    def __init__(self, root: Path, logger: EventLogger | None = None) -> None:
        self.root = root
        self.logger = logger
        self.root.mkdir(parents=True, exist_ok=True)

    def _path_for(self, name: str) -> Path:
        name = name.strip()
        if not MEMORY_NAME_RE.match(name):
            raise ValueError("Memory names may only contain letters, numbers, dot, dash, underscore")
        if not name.endswith(".md"):
            name += ".md"
        path = (self.root / name).resolve()
        root = self.root.resolve()
        if root not in path.parents and path != root:
            raise ValueError("Memory path escapes memory root")
        return path

    def read(self, name: str) -> str:
        path = self._path_for(name)
        if not path.exists():
            raise FileNotFoundError(f"No memory file named {name}")
        content = path.read_text(encoding="utf-8")
        self._log("memory_read", name=name, bytes=len(content.encode("utf-8")))
        return content

    def write(self, name: str, content: str) -> Path:
        path = self._path_for(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(content.rstrip() + "\n", encoding="utf-8")
        tmp.replace(path)
        self._log("memory_written", name=name, bytes=len(content.encode("utf-8")))
        return path

    def index(self, *, limit: int = 100) -> str:
        files = sorted(self.root.glob("*.md"))[:limit]
        if not files:
            return "No memory files found."
        lines = []
        for path in files:
            first_line = ""
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    if line.strip() and not line.startswith("---"):
                        first_line = line.strip()[:160]
                        break
            except UnicodeDecodeError:
                first_line = "[unreadable text]"
            lines.append(f"- {path.name}: {first_line}")
        index = "\n".join(lines)
        self._log("memory_indexed", count=len(files), bytes=len(index.encode("utf-8")))
        return index

    def _log(self, event: str, **fields: object) -> None:
        if self.logger:
            self.logger.event("core", event, **fields)
