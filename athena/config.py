from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AthenaPaths:
    repo_root: Path
    openclaw_root: Path
    workspace_root: Path
    workspace_telegram_root: Path
    db_path: Path
    briefs_dir: Path
    life_dir: Path
    notebooklm_export_dir: Path
    google_dir: Path
    google_settings_path: Path
    google_client_secrets_path: Path
    google_token_path: Path
    google_mirror_dir: Path
    task_view_dir: Path
    ledger_path: Path
    local_ledger_path: Path
    schema_path: Path


def _expand_env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    return Path(raw).expanduser().resolve() if raw else default.resolve()


def default_paths() -> AthenaPaths:
    repo_root = Path(__file__).resolve().parent.parent
    openclaw_root = _expand_env_path(
        "ATHENA_OPENCLAW_ROOT",
        Path.home() / ".openclaw",
    )
    workspace_root = _expand_env_path(
        "ATHENA_WORKSPACE_ROOT",
        openclaw_root / "workspace",
    )
    workspace_telegram_root = _expand_env_path(
        "ATHENA_WORKSPACE_TELEGRAM_ROOT",
        openclaw_root / "workspace-telegram",
    )
    db_path = _expand_env_path(
        "ATHENA_DB_PATH",
        workspace_root / "system/task-ledger/tasks.sqlite",
    )
    task_view_dir = _expand_env_path(
        "ATHENA_TASK_VIEW_DIR",
        workspace_telegram_root / "task-system",
    )
    ledger_path = _expand_env_path(
        "ATHENA_LEDGER_PATH",
        workspace_root / "system/task-ledger/telegram-1937792843.md",
    )
    local_ledger_path = _expand_env_path(
        "ATHENA_LOCAL_LEDGER_PATH",
        task_view_dir / "TELEGRAM_LEDGER.md",
    )
    briefs_dir = _expand_env_path(
        "ATHENA_BRIEFS_DIR",
        workspace_root / "system" / "briefs",
    )
    life_dir = _expand_env_path(
        "ATHENA_LIFE_DIR",
        workspace_root / "life",
    )
    notebooklm_export_dir = _expand_env_path(
        "ATHENA_NOTEBOOKLM_EXPORT_DIR",
        life_dir / "notebooklm-exports",
    )
    google_dir = _expand_env_path(
        "ATHENA_GOOGLE_DIR",
        workspace_root / "system" / "google",
    )
    google_settings_path = _expand_env_path(
        "ATHENA_GOOGLE_SETTINGS_PATH",
        google_dir / "settings.json",
    )
    google_client_secrets_path = _expand_env_path(
        "ATHENA_GOOGLE_CLIENT_SECRETS_PATH",
        google_dir / "client_secret.json",
    )
    google_token_path = _expand_env_path(
        "ATHENA_GOOGLE_TOKEN_PATH",
        google_dir / "token.json",
    )
    google_mirror_dir = _expand_env_path(
        "ATHENA_GOOGLE_MIRROR_DIR",
        workspace_root / "system" / "google-mirror",
    )
    schema_path = repo_root / "athena/sql/schema.sql"

    return AthenaPaths(
        repo_root=repo_root,
        openclaw_root=openclaw_root,
        workspace_root=workspace_root,
        workspace_telegram_root=workspace_telegram_root,
        db_path=db_path,
        briefs_dir=briefs_dir,
        life_dir=life_dir,
        notebooklm_export_dir=notebooklm_export_dir,
        google_dir=google_dir,
        google_settings_path=google_settings_path,
        google_client_secrets_path=google_client_secrets_path,
        google_token_path=google_token_path,
        google_mirror_dir=google_mirror_dir,
        task_view_dir=task_view_dir,
        ledger_path=ledger_path,
        local_ledger_path=local_ledger_path,
        schema_path=schema_path,
    )
