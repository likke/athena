from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path
from typing import Any

from .config import AthenaPaths, default_paths


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc, tb):
        try:
            return super().__exit__(exc_type, exc, tb)
        finally:
            self.close()


MIGRATION_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "life_goals": [
        ("status_note", "TEXT"),
        ("next_review_at", "INTEGER"),
        ("completion_record_id", "INTEGER"),
        ("derived_status", "TEXT"),
        ("derived_summary", "TEXT"),
        ("rollup_updated_at", "INTEGER"),
    ],
    "projects": [
        ("status_source", "TEXT NOT NULL DEFAULT 'manual'"),
        ("health_source", "TEXT NOT NULL DEFAULT 'derived'"),
        ("derived_status", "TEXT"),
        ("derived_health", "TEXT"),
        ("rollup_summary", "TEXT"),
        ("rollup_updated_at", "INTEGER"),
        ("completion_summary", "TEXT"),
        ("completion_record_id", "INTEGER"),
        ("completion_mode", "TEXT"),
    ],
    "tasks": [
        ("capture_id", "TEXT"),
        ("required_for_project_completion", "INTEGER NOT NULL DEFAULT 1"),
        ("resolution", "TEXT"),
        ("completion_summary", "TEXT"),
        ("completion_record_id", "INTEGER"),
        ("reopened_at", "INTEGER"),
        ("reopen_reason", "TEXT"),
    ],
    "chat_state": [
        ("current_capture_id", "TEXT"),
    ],
}


def now_ts() -> int:
    return int(time.time())


def connect_db(db_path: Path | None = None) -> sqlite3.Connection:
    paths = default_paths()
    resolved = (db_path or paths.db_path).expanduser().resolve()
    conn = sqlite3.connect(resolved, factory=ClosingConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {row["name"] for row in rows}


def apply_schema_migrations(conn: sqlite3.Connection) -> None:
    for table_name, columns in MIGRATION_COLUMNS.items():
        existing = table_columns(conn, table_name)
        for column_name, column_type in columns:
            if column_name in existing:
                continue
            conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")
            existing.add(column_name)


def ensure_db(
    db_path: Path | None = None,
    *,
    paths: AthenaPaths | None = None,
    schema_path: Path | None = None,
) -> Path:
    resolved_paths = paths or default_paths()
    resolved_db = (db_path or resolved_paths.db_path).expanduser().resolve()
    resolved_schema = (schema_path or resolved_paths.schema_path).expanduser().resolve()
    resolved_db.parent.mkdir(parents=True, exist_ok=True)
    with connect_db(resolved_db) as conn:
        schema = resolved_schema.read_text(encoding="utf-8")
        conn.executescript(schema)
        apply_schema_migrations(conn)
        conn.commit()
    return resolved_db


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or f"item-{now_ts()}"


def query_all(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def query_one(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    return row_to_dict(conn.execute(sql, params).fetchone())


def dashboard_snapshot(db_path: Path | None = None) -> dict[str, Any]:
    resolved = (db_path or default_paths().db_path).expanduser().resolve()
    with connect_db(resolved) as conn:
        counts = query_one(
            conn,
            """
            SELECT
              COALESCE(SUM(CASE WHEN status IN ('queued', 'in_progress', 'blocked', 'someday') THEN 1 ELSE 0 END), 0) AS open_tasks,
              COALESCE(SUM(CASE WHEN status = 'blocked' THEN 1 ELSE 0 END), 0) AS blocked_tasks,
              COALESCE(SUM(CASE WHEN status = 'in_progress' THEN 1 ELSE 0 END), 0) AS in_progress_tasks,
              COALESCE(SUM(CASE WHEN status = 'queued' THEN 1 ELSE 0 END), 0) AS queued_tasks,
              COALESCE(SUM(CASE WHEN status IN ('done', 'cancelled') THEN 1 ELSE 0 END), 0) AS closed_tasks
            FROM tasks
            """,
        ) or {}
        outbox = query_one(
            conn,
            """
            SELECT
              COALESCE(SUM(CASE WHEN status = 'needs_approval' THEN 1 ELSE 0 END), 0) AS outbox_needs_approval,
              COALESCE(SUM(CASE WHEN status = 'approved' THEN 1 ELSE 0 END), 0) AS outbox_approved,
              COALESCE(SUM(CASE WHEN status = 'sent' THEN 1 ELSE 0 END), 0) AS outbox_sent,
              COALESCE(SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END), 0) AS outbox_errors
            FROM outbox_items
            """,
        ) or {}
        inbox = query_one(
            conn,
            """
            SELECT
              COALESCE(SUM(CASE WHEN status = 'new' THEN 1 ELSE 0 END), 0) AS new_items,
              COALESCE(SUM(CASE WHEN status = 'triaged' THEN 1 ELSE 0 END), 0) AS triaged_items
            FROM captured_items
            """,
        ) or {}
        project_health = query_all(
            conn,
            """
            SELECT health, COUNT(*) AS count
            FROM projects
            WHERE status IN ('active', 'blocked')
            GROUP BY health
            ORDER BY count DESC, health
            """,
        )
        active_projects = query_all(
            conn,
            """
            SELECT
              p.id,
              p.name,
              pf.name AS portfolio_name,
              p.health,
              p.status,
              p.current_goal,
              p.next_milestone,
              p.blocker,
              p.last_real_progress_at
            FROM projects p
            JOIN portfolios pf ON pf.id = p.portfolio_id
            WHERE p.status IN ('active', 'blocked')
            ORDER BY pf.priority DESC, p.updated_at DESC
            """
        )
        current_chat = query_one(
            conn,
            """
            SELECT
              c.channel,
              c.chat_id,
              c.last_user_intent,
              c.last_progress,
              c.updated_at,
              p.name AS project_name,
              pf.name AS portfolio_name,
              t.title AS current_task_title,
              t.next_action AS current_task_next_action,
              t.status AS current_task_status
            FROM chat_state c
            LEFT JOIN projects p ON p.id = c.current_project_id
            LEFT JOIN portfolios pf ON pf.id = c.current_portfolio_id
            LEFT JOIN tasks t ON t.id = c.current_task_id
            WHERE c.channel = 'telegram' AND c.chat_id = '1937792843'
            LIMIT 1
            """,
        )
        briefs = query_all(
            conn,
            """
            SELECT scope_kind, scope_id, brief_type, content, created_at
            FROM awareness_briefs
            ORDER BY created_at DESC
            LIMIT 12
            """
        )
        sources = query_all(
            conn,
            """
            SELECT id, kind, title, path, external_url, source_system, is_authoritative, last_synced_at, summary
            FROM source_documents
            ORDER BY is_authoritative DESC, updated_at DESC, title
            """
        )
    return {
        "counts": counts,
        "outbox": outbox,
        "inbox": inbox,
        "project_health": project_health,
        "active_projects": active_projects,
        "current_chat": current_chat,
        "briefs": briefs,
        "sources": sources,
        "db": str(resolved),
    }
