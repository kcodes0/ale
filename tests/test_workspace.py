import pytest

from ale.workspace import WorkspaceStore


def test_workspace_files_and_chat_round_trip(tmp_path):
    store = WorkspaceStore(tmp_path)
    workspace_id = store.create("Research Plan", kind="linguist", lead="Lead Linguist")

    store.append_chat(workspace_id, author="Lead Linguist", message="Check primary sources.")
    store.write_file(workspace_id, "notes/source-map.md", "# Sources")

    assert store.read_file(workspace_id, "notes/source-map.md") == "# Sources\n"
    assert store.list_files(workspace_id) == ["notes/source-map.md"]
    assert store.list_workspaces()[0]["workspace_id"] == workspace_id


def test_workspace_blocks_path_escape(tmp_path):
    store = WorkspaceStore(tmp_path)
    workspace_id = store.create("Engineering", kind="engineer", lead="Engineer")

    with pytest.raises(ValueError):
        store.write_file(workspace_id, "../escape.md", "bad")
