from __future__ import annotations

import argparse
import json
import re
import signal
import subprocess
import urllib.error
from dataclasses import replace
from pathlib import Path
from typing import Iterable

from .config import AthenaPaths, default_paths
from .db import (
    connect_db,
    ensure_db,
    now_ts,
    query_all,
    query_one,
)
from .google import GoogleAuthError, mirror_google_sources
from .source_docs import (
    TEXT_SUFFIXES,
    choose_document_id,
    default_document_id,
    dedupe_source_documents,
    iter_text_files,
    text_summary,
    title_from_path,
    upsert_source_document,
)
from .synthesis import generate_weekly_ceo_brief

NOTEBOOKLM_LIFE_BUNDLE = "ATHENA_LIFE_CONTEXT_BUNDLE.md"
LOCAL_READ_TIMEOUT_SECONDS = 2


class LocalDriveReadTimeoutError(TimeoutError):
    pass


def _read_local_text(path: Path, *, timeout_seconds: int = LOCAL_READ_TIMEOUT_SECONDS) -> str:
    if not hasattr(signal, "SIGALRM"):
        return path.read_text(encoding="utf-8", errors="replace")

    previous = signal.getsignal(signal.SIGALRM)

    def _handle_timeout(signum, frame):
        raise LocalDriveReadTimeoutError(f"Timed out reading {path}")

    signal.signal(signal.SIGALRM, _handle_timeout)
    try:
        signal.alarm(timeout_seconds)
        return path.read_text(encoding="utf-8", errors="replace")
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync Athena life, repo, and awareness sources.")
    parser.add_argument("--db", help="Override tasks.sqlite path.")
    parser.add_argument("--life-dir", help="Override canonical life-doc directory.")
    parser.add_argument("--notebooklm-dir", help="Override NotebookLM export mirror directory.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("life", help="Sync the canonical life docs and NotebookLM exports.")
    subparsers.add_parser("google", help="Mirror Gmail, Drive, and NotebookLM exports from Google.")
    subparsers.add_parser("repos", help="Scan project repos and refresh project health signals.")
    subparsers.add_parser("briefs", help="Refresh global, portfolio, and project awareness briefs.")
    subparsers.add_parser("weekly-brief", help="Generate the weekly Athena CEO brief.")
    subparsers.add_parser("all", help="Run life, repos, and briefs together.")

    return parser.parse_args()


def _resolve_paths(args: argparse.Namespace) -> AthenaPaths:
    paths = default_paths()
    if args.db:
        paths = replace(paths, db_path=Path(args.db).expanduser().resolve())
    return paths


def _ensure_dir(path: Path) -> Path:
    path = path.expanduser().resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def sync_life_docs(conn, life_dir: Path) -> list[str]:
    life_dir = _ensure_dir(life_dir)
    processed: list[str] = []
    for entry in iter_text_files(life_dir, suffixes=TEXT_SUFFIXES):
        title = title_from_path(entry)
        summary = text_summary(entry.read_text(encoding="utf-8"))
        doc_id = choose_document_id(conn, entry, default_document_id("life", entry))
        upsert_source_document(
            conn,
            doc_id=doc_id,
            kind="life_doc",
            title=title,
            path=entry,
            source_system="life-doc",
            is_authoritative=True,
            summary=summary,
        )
        dedupe_source_documents(conn, entry, doc_id)
        processed.append(entry.name)
    return processed


def ensure_notebooklm_life_bundle(life_dir: Path, notebook_dir: Path) -> Path | None:
    life_dir = _ensure_dir(life_dir)
    notebook_dir = _ensure_dir(notebook_dir)
    life_docs = iter_text_files(life_dir, suffixes=TEXT_SUFFIXES)
    if not life_docs:
        return None

    sections = [
        "# Athena Life Context Bundle",
        "",
        "This file is generated from Athena's canonical local life docs.",
        "It keeps the NotebookLM context path useful even when no fresh Drive exports are present.",
        "",
    ]
    for entry in life_docs:
        title = title_from_path(entry)
        content = entry.read_text(encoding="utf-8").strip()
        sections.extend(
            [
                f"## {title}",
                "",
                content or "(empty)",
                "",
            ]
        )
    bundle_path = notebook_dir / NOTEBOOKLM_LIFE_BUNDLE
    bundle_path.write_text("\n".join(sections).strip() + "\n", encoding="utf-8")
    return bundle_path


def sync_notebooklm_exports(conn, notebook_dir: Path, *, life_dir: Path | None = None) -> list[str]:
    notebook_dir = _ensure_dir(notebook_dir)
    if life_dir is not None:
        ensure_notebooklm_life_bundle(life_dir, notebook_dir)
    notebook_dir = notebook_dir.expanduser().resolve()
    processed: list[str] = []
    for entry in iter_text_files(notebook_dir, suffixes=TEXT_SUFFIXES):
        title = title_from_path(entry)
        summary = text_summary(entry.read_text(encoding="utf-8"))
        doc_id = choose_document_id(
            conn,
            entry,
            default_document_id("notebooklm", entry),
        )
        upsert_source_document(
            conn,
            doc_id=doc_id,
            kind="notebooklm",
            title=title,
            path=entry,
            source_system="NotebookLM",
            is_authoritative=False,
            summary=summary,
        )
        dedupe_source_documents(conn, entry, doc_id)
        processed.append(entry.name)
    return processed


def _slug_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "local-folder"


def _load_local_drive_folders(paths: AthenaPaths) -> list[tuple[str, Path]]:
    settings_path = paths.google_settings_path.expanduser().resolve()
    if not settings_path.exists():
        return []
    raw = json.loads(settings_path.read_text(encoding="utf-8"))
    folders: list[tuple[str, Path]] = []
    for folder in ((raw.get("drive") or {}).get("local_folders") or []):
        raw_path = str(folder.get("path") or "").strip()
        if not raw_path:
            continue
        path = Path(raw_path).expanduser().resolve()
        name = str(folder.get("name") or path.name or "Local Drive Folder").strip()
        folders.append((name, path))
    return folders


def sync_local_drive_folders(conn, paths: AthenaPaths) -> dict[str, int]:
    specs = _load_local_drive_folders(paths)
    if not specs:
        return {"local_drive_files": 0, "local_drive_folders": 0, "local_drive_skipped": 0}

    summary_dir = _ensure_dir(paths.google_mirror_dir / "drive-local")
    mirrored_files = 0
    mirrored_folders = 0
    skipped_files = 0

    for name, folder_path in specs:
        lines = [
            f"# {name} Local Mirror",
            "",
            f"- path: {folder_path}",
            f"- available: {'yes' if folder_path.exists() else 'no'}",
        ]
        local_count = 0
        if folder_path.exists():
            files = iter_text_files(folder_path, recursive=True)
            lines.append(f"- mirrored_files: {len(files)}")
            lines.append("")
            for entry in files:
                title = title_from_path(entry)
                rel = entry.relative_to(folder_path)
                try:
                    content = _read_local_text(entry)
                except OSError as exc:
                    lines.append(f"- skipped: {rel} ({exc})")
                    skipped_files += 1
                    continue
                summary = text_summary(content)
                doc_id = choose_document_id(conn, entry, default_document_id("gdrive-local", entry))
                upsert_source_document(
                    conn,
                    doc_id=doc_id,
                    kind="drive_file",
                    title=title,
                    path=entry,
                    source_system="gdrive-local",
                    is_authoritative=False,
                    summary=summary,
                )
                dedupe_source_documents(conn, entry, doc_id)
                lines.append(f"- {rel}")
                local_count += 1
            mirrored_folders += 1
        else:
            lines.append("- mirrored_files: 0")

        if skipped_files:
            lines.append(f"- skipped_files: {skipped_files}")

        summary_path = summary_dir / f"{_slug_name(name)}-summary.md"
        summary_path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
        summary_doc_id = choose_document_id(
            conn,
            summary_path,
            default_document_id("gdrive-local", summary_path),
        )
        upsert_source_document(
            conn,
            doc_id=summary_doc_id,
            kind="drive_file_summary",
            title=f"{name} Local Summary",
            path=summary_path,
            source_system="gdrive-local",
            is_authoritative=False,
            summary=f"{local_count} local drive files from {name}",
        )
        dedupe_source_documents(conn, summary_path, summary_doc_id)
        mirrored_files += local_count

    return {
        "local_drive_files": mirrored_files,
        "local_drive_folders": mirrored_folders,
        "local_drive_skipped": skipped_files,
    }


def _run_git(repo_path: Path, args: Iterable[str]) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_path), *args],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def scan_project_repos(conn) -> dict[str, int]:
    now = now_ts()
    summary = {"scanned": 0, "projects_updated": 0}
    rows = query_all(conn, "SELECT id, project_id, repo_path, last_seen_commit FROM project_repos")
    for repo in rows:
        summary["scanned"] += 1
        repo_path = Path(repo["repo_path"]).expanduser().resolve()
        branch = ""
        commit = ""
        dirty = 0
        if repo_path.is_dir():
            branch = _run_git(repo_path, ["rev-parse", "--abbrev-ref", "HEAD"]) or ""
            commit = _run_git(repo_path, ["rev-parse", "HEAD"]) or ""
            status = _run_git(repo_path, ["status", "--porcelain"]) or ""
            dirty = 1 if status.strip() else 0
        conn.execute(
            """
            UPDATE project_repos SET
              last_seen_branch = ?,
              last_seen_commit = ?,
              last_seen_dirty = ?,
              last_scanned_at = ?
            WHERE id = ?
            """,
            (branch, commit, dirty, now, repo["id"]),
        )

        project = query_one(conn, "SELECT id, health, status FROM projects WHERE id = ?", (repo["project_id"],))
        updates: dict[str, object] = {}
        if project:
            if dirty:
                updates["health"] = "yellow"
            elif commit and commit != (repo["last_seen_commit"] or ""):
                updates["health"] = "green"
                updates["last_real_progress_at"] = now
            if updates:
                assignments = ", ".join([f"{key} = ?" for key in updates])
                values = [updates[key] for key in updates] + [project["id"]]
                conn.execute(f"UPDATE projects SET {assignments} WHERE id = ?", values)
                summary["projects_updated"] += 1
    return summary


def _replace_brief(conn, scope_kind: str, scope_id: str, brief_type: str, content: str, ts: int) -> None:
    conn.execute(
        "DELETE FROM awareness_briefs WHERE scope_kind = ? AND scope_id = ? AND brief_type = ?",
        (scope_kind, scope_id, brief_type),
    )
    conn.execute(
        "INSERT INTO awareness_briefs (scope_kind, scope_id, brief_type, content, created_at) VALUES (?, ?, ?, ?, ?)",
        (scope_kind, scope_id, brief_type, content.strip(), ts),
    )


def refresh_awareness_briefs(conn) -> dict[str, int]:
    now = now_ts()
    counts = query_one(
        conn,
        """
        SELECT
          SUM(CASE WHEN status IN ('queued', 'in_progress', 'blocked', 'someday') THEN 1 ELSE 0 END) AS open_tasks,
          SUM(CASE WHEN status = 'blocked' THEN 1 ELSE 0 END) AS blocked_tasks
        FROM tasks
        """,
    ) or {}
    open_tasks = counts.get("open_tasks", 0)
    blocked_tasks = counts.get("blocked_tasks", 0)
    global_summary = f"{open_tasks} open tasks, {blocked_tasks} blocked across Athena." if open_tasks else "No open tasks."
    _replace_brief(conn, "global", "global", "status", global_summary, now)

    portfolio_rows = query_all(
        conn,
        """
        SELECT
          pf.id,
          pf.name,
          SUM(CASE WHEN t.status IN ('queued', 'in_progress') THEN 1 ELSE 0 END) AS open_tasks,
          SUM(CASE WHEN t.status = 'blocked' THEN 1 ELSE 0 END) AS blocked_tasks
        FROM portfolios pf
        LEFT JOIN tasks t ON t.portfolio_id = pf.id
        GROUP BY pf.id
        """,
    )
    for row in portfolio_rows:
        open_count = row.get("open_tasks") or 0
        blocked_count = row.get("blocked_tasks") or 0
        text = f"{row['name']}: {open_count} open, {blocked_count} blocked."
        _replace_brief(conn, "portfolio", row["id"], "status", text, now)

    project_rows = query_all(
        conn,
        """
        SELECT id, name, status, health, next_milestone, blocker, last_real_progress_at
        FROM projects
        WHERE status IN ('active', 'blocked')
        ORDER BY updated_at DESC
        LIMIT 12
        """,
    )
    for row in project_rows:
        parts = [f"{row['name']} ({row['status']}/{row['health']})"]
        if row.get("next_milestone"):
            parts.append(f"next: {row['next_milestone']}")
        if row.get("blocker"):
            parts.append(f"blocked by: {row['blocker']}")
        if row.get("last_real_progress_at"):
            parts.append("recent progress")
        text = "; ".join(parts)
        _replace_brief(conn, "project", row["id"], "status", text, now)
    return {"portfolios": len(portfolio_rows), "projects": len(project_rows)}


def run_sync(
    command: str,
    *,
    paths: AthenaPaths | None = None,
    life_dir: Path | None = None,
    notebook_dir: Path | None = None,
) -> dict[str, object]:
    resolved_paths = paths or default_paths()
    ensure_db(paths=resolved_paths)
    resolved_life_dir = (life_dir or resolved_paths.life_dir).expanduser().resolve()
    resolved_notebook_dir = (notebook_dir or resolved_paths.notebooklm_export_dir).expanduser().resolve()

    with connect_db(resolved_paths.db_path) as conn:
        summary: dict[str, object] = {"command": command, "db": str(resolved_paths.db_path)}
        if command in ("google", "all"):
            try:
                google_summary = mirror_google_sources(conn, paths=resolved_paths)
            except (GoogleAuthError, urllib.error.HTTPError, urllib.error.URLError) as exc:
                google_summary = {
                    "google_enabled": False,
                    "gmail_messages": 0,
                    "drive_files": 0,
                    "notebooklm_files": 0,
                    "google_error": str(exc),
                }
            summary.update(google_summary)
            summary.update(sync_local_drive_folders(conn, resolved_paths))
            if command == "google":
                mirrored_notebook = sync_notebooklm_exports(
                    conn,
                    resolved_notebook_dir,
                    life_dir=resolved_life_dir,
                )
                summary["notebook_exports"] = len(mirrored_notebook)
        if command in ("life", "all"):
            processed = sync_life_docs(conn, resolved_life_dir)
            notebook = sync_notebooklm_exports(
                conn,
                resolved_notebook_dir,
                life_dir=resolved_life_dir,
            )
            summary["life_docs"] = len(processed)
            summary["notebook_exports"] = len(notebook)
        if command in ("repos", "all"):
            repo_summary = scan_project_repos(conn)
            summary["repos_scanned"] = repo_summary.get("scanned", 0)
            summary["projects_updated"] = repo_summary.get("projects_updated", 0)
        if command in ("briefs", "all"):
            brief_summary = refresh_awareness_briefs(conn)
            summary["portfolios_briefed"] = brief_summary.get("portfolios", 0)
            summary["projects_briefed"] = brief_summary.get("projects", 0)
        if command == "weekly-brief":
            weekly_brief = generate_weekly_ceo_brief(conn, paths=resolved_paths)
            summary["weekly_brief_path"] = weekly_brief["path"]
            summary["weekly_brief_week_of"] = weekly_brief["week_of"]
            summary["weekly_brief_summary"] = weekly_brief["summary"]
        conn.commit()
    return summary



def main() -> int:
    args = parse_args()
    paths = _resolve_paths(args)
    life_dir = Path(args.life_dir).expanduser().resolve() if args.life_dir else paths.life_dir
    notebook_dir = (
        Path(args.notebooklm_dir).expanduser().resolve()
        if args.notebooklm_dir
        else paths.notebooklm_export_dir
    )
    summary = run_sync(
        args.command,
        paths=paths,
        life_dir=life_dir,
        notebook_dir=notebook_dir,
    )

    for key, value in summary.items():
        print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
