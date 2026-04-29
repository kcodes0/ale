from ale.config import load_settings


def test_restricted_tools_default_off(monkeypatch):
    monkeypatch.delenv("ALE_ENABLE_BASH_TOOL", raising=False)
    monkeypatch.delenv("ALE_ENABLE_CODEX_EXEC", raising=False)

    settings = load_settings()

    assert settings.enable_bash_tool is False
    assert settings.enable_codex_exec is False


def test_operational_defaults(monkeypatch):
    monkeypatch.delenv("ALE_DISCORD_SYNC_COMMANDS", raising=False)
    monkeypatch.delenv("ALE_LOG_MAX_BYTES", raising=False)
    monkeypatch.delenv("ALE_LOG_BACKUP_COUNT", raising=False)

    settings = load_settings()

    assert settings.discord_sync_commands == "auto"
    assert settings.log_max_bytes == 10_000_000
    assert settings.log_backup_count == 5
