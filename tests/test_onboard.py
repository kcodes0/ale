from pathlib import Path

import pytest

from ale.onboard import (
    EnvFile,
    _looks_like_discord_token,
    _looks_like_git_url,
    _looks_like_user_id,
    _normalize_user_ids,
)


def test_envfile_load_returns_empty_for_missing_path(tmp_path: Path):
    env = EnvFile.load(tmp_path / "nope.env")

    assert env.lines == []
    assert env.get("DISCORD_TOKEN") is None


def test_envfile_set_replaces_existing_key_in_place(tmp_path: Path):
    path = tmp_path / ".env"
    path.write_text("# header comment\nDISCORD_TOKEN=old\nOTHER=keep\n", encoding="utf-8")
    env = EnvFile.load(path)

    env.set("DISCORD_TOKEN", "new")
    env.write()
    body = path.read_text(encoding="utf-8")

    assert "DISCORD_TOKEN=new" in body
    assert "DISCORD_TOKEN=old" not in body
    assert "# header comment" in body
    assert "OTHER=keep" in body


def test_envfile_set_appends_new_key(tmp_path: Path):
    path = tmp_path / ".env"
    path.write_text("EXISTING=1\n", encoding="utf-8")
    env = EnvFile.load(path)

    env.set("ALE_ALLOWED_USER_IDS", "12345")
    env.write()

    lines = path.read_text(encoding="utf-8").splitlines()
    assert "EXISTING=1" in lines
    assert "ALE_ALLOWED_USER_IDS=12345" in lines


def test_envfile_write_is_chmod_600(tmp_path: Path):
    path = tmp_path / ".env"
    env = EnvFile.load(path)
    env.set("DISCORD_TOKEN", "secret")
    env.write()

    mode = path.stat().st_mode & 0o777
    assert mode == 0o600


def test_discord_token_validator():
    assert _looks_like_discord_token("MTQ5ODg4Mjg0MzUxMjY2ODI2MQ.G94wEa.dmWmEIxPi_m0aQYmJnuq9Gr")
    assert not _looks_like_discord_token("short")
    assert not _looks_like_discord_token("has spaces in it that is not allowed at all")


def test_user_id_validator():
    assert _looks_like_user_id("1234567890123456")
    assert not _looks_like_user_id("12")
    assert not _looks_like_user_id("not-a-number")


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/user/repo.git",
        "git@github.com:user/repo.git",
        "ssh://git@github.com:22/user/repo.git",
        "https://github.com/user/repo",
    ],
)
def test_git_url_validator_accepts_common_forms(url: str):
    assert _looks_like_git_url(url)


@pytest.mark.parametrize("url", ["not a url", "", "ftp://nope"])
def test_git_url_validator_rejects_garbage(url: str):
    assert not _looks_like_git_url(url)


def test_normalize_user_ids_accepts_csv_and_dedupes():
    assert _normalize_user_ids("1234567890123456, 9876543210987654, 1234567890123456") == [
        "1234567890123456",
        "9876543210987654",
    ]


def test_normalize_user_ids_rejects_mixed_garbage():
    assert _normalize_user_ids("1234567890123456, hello") is None


def test_normalize_user_ids_rejects_empty():
    assert _normalize_user_ids("") is None
    assert _normalize_user_ids("   ") is None
