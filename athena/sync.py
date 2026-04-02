from __future__ import annotations

import argparse
import subprocess
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


def sync_notebooklm_exports(conn, notebook_dir: Path) -> list[str]:
    if not notebook_dir.exists():
        return []
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
            except GoogleAuthError as exc:
                google_summary = {
                    "google_enabled": False,
                    "gmail_messages": 0,
                    "drive_files": 0,
                    "notebooklm_files": 0,
                    "google_error": str(exc),
                }
            summary.update(google_summary)
            if command == "google":
                mirrored_notebook = sync_notebooklm_exports(conn, resolved_notebook_dir)
                summary["notebook_exports"] = len(mirrored_notebook)
        if command in ("life", "all"):
            processed = sync_life_docs(conn, resolved_life_dir)
            notebook = sync_notebooklm_exports(conn, resolved_notebook_dir)
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
