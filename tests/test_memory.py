import pytest

from ale.memory import MemoryStore


def test_memory_write_and_read(tmp_path):
    memory = MemoryStore(tmp_path)
    memory.write("project.md", "# Project\nAle uses Discord.")

    assert "Ale uses Discord" in memory.read("project.md")
    assert "project.md" in memory.index()


def test_memory_blocks_path_escape(tmp_path):
    memory = MemoryStore(tmp_path)

    with pytest.raises(ValueError):
        memory.write("../escape.md", "bad")
