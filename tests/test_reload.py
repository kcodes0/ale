import asyncio
import json
import os
from pathlib import Path

import pytest

from ale.config import Settings, load_settings
from ale.observability import EventLogger
from ale.reload import FileWatchdog, ReloadManager


@pytest.fixture
def isolated_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("ALE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ALE_MEMORY_DIR", str(tmp_path / "memory"))
    monkeypatch.setenv("ALE_HOT_RELOAD_ENABLED", "true")
    monkeypatch.setenv("ALE_HOT_RELOAD_TOOLS", "true")
    return load_settings()


def _make_manager(settings: Settings, tmp_path: Path) -> ReloadManager:
    logger = EventLogger(tmp_path / "logs")
    return ReloadManager(settings, logger=logger)


async def test_reload_config_records_history(tmp_path, isolated_settings):
    manager = _make_manager(isolated_settings, tmp_path)

    result = await manager.reload_config_and_prompts(reason="test")

    assert result.ok
    assert result.tier == "config"
    assert manager.history[-1].reload_id == result.reload_id
    assert manager.snapshot.base_system_prompt
    assert manager.snapshot.persona_prompts.keys() == {"actor", "linguist", "engineer"}


async def test_reload_tools_reimports_module(tmp_path, isolated_settings):
    manager = _make_manager(isolated_settings, tmp_path)
    before = manager.snapshot.tools_module_id

    result = await manager.reload_tools(reason="test")

    assert result.ok
    after = manager.snapshot.tools_module_id
    # The module object identity stays the same but its functions get rebound.
    # Ensure the result is recorded and tools_reimported is tracked in extras.
    assert before == after
    assert result.extra.get("tools_reimported") is True


async def test_reload_tools_disabled_returns_error(tmp_path, monkeypatch):
    monkeypatch.setenv("ALE_HOT_RELOAD_TOOLS", "false")
    settings = load_settings()
    manager = _make_manager(settings, tmp_path)

    result = await manager.reload_tools(reason="test")

    assert not result.ok
    assert result.error_type == "ConfigDisabled"


async def test_rollback_restores_previous_snapshot(tmp_path, isolated_settings):
    manager = _make_manager(isolated_settings, tmp_path)
    initial_snapshot = manager.snapshot

    await manager.reload_config_and_prompts(reason="first")
    second_snapshot = manager.snapshot

    rollback = await manager.rollback(reason="undo")
    assert rollback.ok
    assert rollback.rolled_back
    assert manager.snapshot is initial_snapshot
    assert manager.snapshot is not second_snapshot


async def test_rollback_with_no_history_fails(tmp_path, isolated_settings):
    manager = _make_manager(isolated_settings, tmp_path)

    result = await manager.rollback(reason="undo")

    assert not result.ok
    assert result.error_type == "RollbackUnavailable"


async def test_promote_requires_successful_health_check(tmp_path, isolated_settings, monkeypatch):
    monkeypatch.setenv("ALE_ENABLE_SELF_PROMOTE", "true")
    manager = _make_manager(load_settings(), tmp_path)

    result = await manager.promote_candidate(reason="test")

    assert not result.ok
    assert result.error_type == "HealthCheckMissing"


async def test_health_check_subcommand_runs_cleanly(tmp_path, isolated_settings):
    manager = _make_manager(isolated_settings, tmp_path)

    result = await manager.health_check_candidate(reason="test")

    assert result.ok
    assert "stdout_tail" in result.extra


async def test_plan_summary_shape(tmp_path, isolated_settings):
    manager = _make_manager(isolated_settings, tmp_path)

    plan = manager.plan()

    assert "history" in plan
    assert "tiers" in plan
    assert plan["tiers"]["config_and_prompts"] == "always available"
    assert "watch_roots" in plan


async def test_watchdog_fires_reload_on_change(tmp_path, monkeypatch):
    watch_dir = tmp_path / "watched"
    watch_dir.mkdir()
    (watch_dir / "module.py").write_text("# initial\n")

    monkeypatch.setenv("ALE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ALE_MEMORY_DIR", str(tmp_path / "memory"))
    monkeypatch.setenv("ALE_HOT_RELOAD_ENABLED", "true")
    monkeypatch.setenv("ALE_HOT_RELOAD_TOOLS", "false")
    monkeypatch.setenv("ALE_RELOAD_WATCH_PATHS", str(watch_dir))
    monkeypatch.setenv("ALE_RELOAD_POLL_INTERVAL_SECONDS", "0.2")
    monkeypatch.setenv("ALE_RELOAD_DEBOUNCE_SECONDS", "0.2")
    settings = load_settings()
    manager = _make_manager(settings, tmp_path)

    fired = asyncio.Event()

    def listener(result):
        if result.ok and result.tier == "config":
            fired.set()

    manager.add_listener(listener)
    watchdog = FileWatchdog(manager, logger=manager.logger)
    await watchdog.start()
    try:
        # Sleep briefly then mutate to trigger detection.
        await asyncio.sleep(0.4)
        (watch_dir / "module.py").write_text("# changed\n")
        os.utime(watch_dir / "module.py")
        await asyncio.wait_for(fired.wait(), timeout=5)
    finally:
        await watchdog.stop()

    assert any(r.tier == "config" and r.ok for r in manager.history)


def test_health_check_cli_returns_zero(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ALE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ALE_MEMORY_DIR", str(tmp_path / "memory"))
    from ale.cli import health_check

    code = health_check()

    assert code == 0
    out = capsys.readouterr().out.strip()
    payload = json.loads(out.splitlines()[-1])
    assert payload["ok"] is True
    assert payload["stage"] == "complete"
