"""First-run onboarding REPL for Ale.

Walks a fresh clone through the three things the harness needs before it can
talk to Discord: a bot token, an allowlist of Discord user ids, and (optional)
a git remote so Engineer can commit and push. Designed to be safe to re-run —
existing values in `.env` are preserved unless the user replaces them, and a
non-zero git remote is never overwritten without confirmation.
"""

from __future__ import annotations

import getpass
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path


# ----- terminal styling -------------------------------------------------------

_USE_COLOR = sys.stdout.isatty()


def _ansi(code: str) -> str:
    return f"\033[{code}m" if _USE_COLOR else ""


BOLD = _ansi("1")
DIM = _ansi("2")
RESET = _ansi("0")
CYAN = _ansi("36")
GREEN = _ansi("32")
RED = _ansi("31")
YELLOW = _ansi("33")

DEFAULT_ENV_VALUES = {
    "ALE_DISCORD_MESSAGE_CONTENT_INTENT": "false",
    "ALE_DISCORD_SYNC_COMMANDS": "auto",
    "ALE_ENABLE_LLM_ROUTER": "true",
    "ALE_ENABLE_BASH_TOOL": "false",
    "ALE_ENABLE_CODEX_EXEC": "false",
    "ALE_HOT_RELOAD_ENABLED": "false",
    "ALE_LOG_MAX_BYTES": "10000000",
    "ALE_LOG_BACKUP_COUNT": "5",
}


def _print_banner() -> None:
    print(f"{BOLD}{CYAN}Ale onboarding{RESET}")
    print(f"{DIM}Discord-native Claude Agent SDK harness — pronounced Ali.{RESET}")
    print()


def _section(index: int, total: int, title: str) -> None:
    print(f"{BOLD}[{index}/{total}] {title}{RESET}")


def _hint(text: str) -> None:
    for line in text.splitlines():
        print(f"  {DIM}{line}{RESET}")


def _ok(text: str) -> None:
    print(f"  {GREEN}✓{RESET} {text}")


def _warn(text: str) -> None:
    print(f"  {YELLOW}!{RESET} {text}")


def _err(text: str) -> None:
    print(f"  {RED}✗{RESET} {text}")


def _prompt(label: str, *, default: str | None = None, secret: bool = False) -> str:
    suffix = f" {DIM}[{default}]{RESET}" if default else ""
    full = f"  {label}{suffix}{DIM} >{RESET} "
    while True:
        try:
            value = getpass.getpass(full) if secret else input(full)
        except EOFError:
            print()
            sys.exit(1)
        value = value.strip()
        if value:
            return value
        if default is not None:
            return default
        if secret:
            _err("This field is required.")
            continue
        _err("This field is required. Press Ctrl-C to abort.")


def _confirm(label: str, *, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    full = f"  {label} {DIM}[{hint}]{RESET}{DIM} >{RESET} "
    while True:
        try:
            value = input(full).strip().lower()
        except EOFError:
            print()
            sys.exit(1)
        if not value:
            return default
        if value in {"y", "yes"}:
            return True
        if value in {"n", "no"}:
            return False
        _err("Please answer y or n.")


# ----- env parsing ------------------------------------------------------------


@dataclass
class EnvFile:
    """Order-preserving editor for a `.env` file.

    Existing keys keep their position and surrounding comments. New keys append
    to the end so the diff stays small on re-runs.
    """

    path: Path
    lines: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> "EnvFile":
        if path.exists():
            return cls(path=path, lines=path.read_text(encoding="utf-8").splitlines())
        return cls(path=path, lines=[])

    def get(self, key: str) -> str | None:
        for line in self.lines:
            stripped = line.strip()
            if stripped.startswith("#") or "=" not in stripped:
                continue
            k, v = stripped.split("=", 1)
            if k.strip() == key:
                return v.strip()
        return None

    def set(self, key: str, value: str) -> None:
        replacement = f"{key}={value}"
        for index, line in enumerate(self.lines):
            stripped = line.strip()
            if stripped.startswith("#") or "=" not in stripped:
                continue
            k, _ = stripped.split("=", 1)
            if k.strip() == key:
                self.lines[index] = replacement
                return
        if self.lines and self.lines[-1].strip():
            self.lines.append("")
        self.lines.append(replacement)

    def ensure_default(self, key: str, value: str) -> bool:
        if self.get(key) is not None:
            return False
        self.set(key, value)
        return True

    def write(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        body = "\n".join(self.lines).rstrip() + "\n"
        self.path.write_text(body, encoding="utf-8")
        if os.name == "nt":
            return False
        try:
            self.path.chmod(0o600)
            return True
        except OSError:
            return False


# ----- validators -------------------------------------------------------------


_DISCORD_TOKEN_RE = re.compile(r"^[A-Za-z0-9._-]{40,}$")
_DISCORD_ID_RE = re.compile(r"^\d{15,25}$")
_GIT_URL_RE = re.compile(
    r"^(?:https://[\w.@:/_~+-]+\.git|git@[\w.-]+:[\w./_-]+\.git|"
    r"ssh://git@[\w.-]+(?::\d+)?/[\w./_-]+\.git|"
    r"https://github\.com/[\w.-]+/[\w.-]+/?)$"
)


def _looks_like_discord_token(token: str) -> bool:
    return bool(_DISCORD_TOKEN_RE.match(token))


def _looks_like_user_id(value: str) -> bool:
    return bool(_DISCORD_ID_RE.match(value))


def _looks_like_git_url(url: str) -> bool:
    return bool(_GIT_URL_RE.match(url))


def _normalize_user_ids(raw: str) -> list[str] | None:
    parts = [p.strip() for p in re.split(r"[,\s]+", raw) if p.strip()]
    if not parts:
        return None
    if not all(_looks_like_user_id(p) for p in parts):
        return None
    seen: list[str] = []
    for p in parts:
        if p not in seen:
            seen.append(p)
    return seen


# ----- git helpers ------------------------------------------------------------


def _git_available() -> bool:
    return shutil.which("git") is not None


def _run_git(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )


def _existing_remote_url(repo_root: Path, name: str = "origin") -> str | None:
    if not _git_available():
        return None
    proc = _run_git(["remote", "get-url", name], cwd=repo_root)
    if proc.returncode == 0:
        return proc.stdout.strip() or None
    return None


def _set_remote(repo_root: Path, url: str, *, name: str = "origin") -> tuple[bool, str]:
    if not _git_available():
        return False, "git is not installed"
    if (repo_root / ".git").exists():
        existing = _existing_remote_url(repo_root, name)
        if existing:
            proc = _run_git(["remote", "set-url", name, url], cwd=repo_root)
        else:
            proc = _run_git(["remote", "add", name, url], cwd=repo_root)
    else:
        return False, f"{repo_root} is not a git repository"
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout).strip()
    return True, "ok"


# ----- main flow --------------------------------------------------------------


def run_onboarding(repo_root: Path | None = None) -> int:
    repo_root = (repo_root or Path.cwd()).resolve()
    env_path = repo_root / ".env"
    env = EnvFile.load(env_path)

    _print_banner()
    print(f"  Working directory: {DIM}{repo_root}{RESET}")
    if env_path.exists():
        _hint("An existing .env was found — values you skip will be preserved.")
    print()

    total = 5

    # 1) Discord bot token --------------------------------------------------
    _section(1, total, "Discord bot token")
    _hint(
        "Create a bot at https://discord.com/developers/applications, then\n"
        "copy the token from the Bot tab. Input is hidden.\n"
        "Press Enter to keep the existing token if one is set."
    )
    existing_token = env.get("DISCORD_TOKEN")
    if existing_token:
        _ok(f"existing token detected (…{existing_token[-6:]})")
    while True:
        token = _prompt(
            "bot token",
            default=existing_token or "",
            secret=True,
        )
        if not token:
            _err("A token is required.")
            continue
        if existing_token and token == existing_token:
            break
        if _looks_like_discord_token(token):
            break
        _warn("That doesn't look like a Discord bot token.")
        if _confirm("Use it anyway?", default=False):
            break
    env.set("DISCORD_TOKEN", token)
    print()

    # 2) Allowed user ids ---------------------------------------------------
    _section(2, total, "Allowed user IDs")
    _hint(
        "Discord ignores anyone not on this list. To find your ID, enable\n"
        "Developer Mode in Discord (Settings → Advanced), right-click your\n"
        "avatar, and pick Copy User ID. Multiple IDs may be comma-separated."
    )
    existing_ids = env.get("ALE_ALLOWED_USER_IDS") or ""
    if existing_ids:
        _ok(f"existing allowlist: {existing_ids}")
    while True:
        raw = _prompt("user IDs", default=existing_ids)
        ids = _normalize_user_ids(raw)
        if ids is None:
            _err("User IDs must be 15–25 digit Discord snowflakes, comma-separated.")
            continue
        break
    env.set("ALE_ALLOWED_USER_IDS", ",".join(ids))
    print()

    # 3) Safe runtime defaults ---------------------------------------------
    _section(3, total, "Runtime defaults")
    _hint(
        "Ale writes explicit safe defaults so a fresh .env is self-documenting:\n"
        "custom bash/codex_exec stay off, hot reload stays manual, Discord\n"
        "slash commands sync once per command schema, and logs rotate locally."
    )
    added_defaults = [
        key for key, value in DEFAULT_ENV_VALUES.items() if env.ensure_default(key, value)
    ]
    if added_defaults:
        _ok(f"will add {len(added_defaults)} default settings")
    else:
        _ok("existing runtime settings preserved")
    print()

    # 4) Git remote ---------------------------------------------------------
    _section(4, total, f"Git remote {DIM}(optional){RESET}")
    _hint(
        "When set, Engineer commits and pushes here after a successful patch.\n"
        "Accepted forms: https://github.com/user/repo.git, git@github.com:user/repo.git\n"
        "Press Enter to skip."
    )
    existing_remote = _existing_remote_url(repo_root) if _git_available() else None
    if existing_remote:
        _ok(f"existing 'origin' remote: {existing_remote}")
    git_url = ""
    if _git_available():
        while True:
            git_url = _prompt(
                "remote URL",
                default=existing_remote or "skip",
            )
            if git_url in {"", "skip"}:
                git_url = ""
                break
            if _looks_like_git_url(git_url):
                break
            _warn("That doesn't look like a git URL.")
            if _confirm("Use it anyway?", default=False):
                break
    else:
        _warn("git is not installed — skipping remote setup.")
    print()

    # 5) Review -------------------------------------------------------------
    _section(5, total, "Review")
    print(f"  {DIM}Will write{RESET} {env_path}")
    masked = "…" + token[-6:] if token else ""
    print(f"    DISCORD_TOKEN={masked}")
    print(f"    ALE_ALLOWED_USER_IDS={','.join(ids)}")
    for key in DEFAULT_ENV_VALUES:
        print(f"    {key}={env.get(key)}")
    if git_url:
        action = "update" if existing_remote else "add"
        print(f"  {DIM}Will{RESET} {action} git remote 'origin' → {git_url}")
    print()
    if not _confirm("Proceed?", default=True):
        _err("Cancelled — no files written.")
        return 1

    # Apply -----------------------------------------------------------------
    print()
    secure_permissions = env.write()
    _ok(f"wrote {env_path}")
    if not secure_permissions:
        _warn(f"could not enforce chmod 600 on {env_path}; check file permissions manually")
    if git_url:
        success, message = _set_remote(repo_root, git_url)
        if success:
            verb = "updated" if existing_remote else "added"
            _ok(f"{verb} git remote 'origin' → {git_url}")
        else:
            _err(f"could not set git remote: {message}")
            _hint("You can set it manually with: git remote add origin <url>")

    print()
    print(f"{BOLD}You're set up.{RESET} Next:")
    print(f"  {CYAN}uv run ale discord{RESET}    {DIM}# start the gateway{RESET}")
    print(
        f"  {CYAN}uv run ale ask 'Actor: hi'{RESET}   "
        f"{DIM}# one-shot local prompt without Discord{RESET}"
    )
    print()
    return 0
