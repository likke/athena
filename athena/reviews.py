from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import default_paths
from .db import connect_db, ensure_db, now_ts, query_all, query_one
from .render_markdown import render
from .rollups import refresh_rollups
from .state import capture_item


def _resolve_db(db_path: Path | None = None) -> Path:
    return (db_path or default_paths().db_path).expanduser().resolve()


def _days_ago(days: int) -> int:
    return now_ts() - (days * 24 * 60 * 60)


def daily_findings(db_path: Path | None = None) -> list[dict[str, Any]]:
    resolved_db = _resolve_db(db_path)
    ensure_db(resolved_db)
    cutoff_blocked = _days_ago(3)
    cutoff_in_progress = _days_ago(7)
    with connect_db(resolved_db) as conn:
        blocked_tasks = query_all(
            conn,
            """
            SELECT id, title, blocker
            FROM tasks
            WHERE status = 'blocked' AND last_touched_at <= ?
            ORDER BY priority DESC, last_touched_at ASC
            """,
            (cutoff_blocked,),
        )
        stale_in_progress = query_all(
            conn,
            """
            SELECT id, title
            FROM tasks
            WHERE status = 'in_progress' AND last_touched_at <= ?
            ORDER BY priority DESC, last_touched_at ASC
            """,
            (cutoff_in_progress,),
        )
        repo_without_update = query_all(
            conn,
            """
            SELECT
              p.id,
              p.name,
              MAX(pr.last_scanned_at) AS last_repo_scan_at,
              MAX(pu.created_at) AS last_project_update_at
            FROM projects p
            JOIN project_repos pr ON pr.project_id = p.id
            LEFT JOIN project_updates pu ON pu.project_id = p.id
            WHERE p.status IN ('active', 'blocked')
            GROUP BY p.id
            HAVING last_repo_scan_at IS NOT NULL AND (last_project_update_at IS NULL OR last_project_update_at < last_repo_scan_at)
            """
        )

    findings: list[dict[str, Any]] = []
    for task in blocked_tasks:
        findings.append(
            {
                "dedupe_key": f"review:daily:blocked-task:{task['id']}",
                "raw_text": f"Blocked task review needed: {task['title']}. Re-check the blocker and decide whether to unblock, cancel, or escalate.",
                "classification": "note",
                "linked_entity_kind": "task",
                "linked_entity_id": task["id"],
                "note": task.get("blocker") or "Blocked task has gone stale.",
            }
        )
    for task in stale_in_progress:
        findings.append(
            {
                "dedupe_key": f"review:daily:stale-task:{task['id']}",
                "raw_text": f"In-progress task looks stale: {task['title']}. Confirm whether it is still active, blocked, or actually done.",
                "classification": "note",
                "linked_entity_kind": "task",
                "linked_entity_id": task["id"],
                "note": "In-progress task untouched for 7 days.",
            }
        )
    for project in repo_without_update:
        findings.append(
            {
                "dedupe_key": f"review:daily:repo-project-gap:{project['id']}",
                "raw_text": f"Project update missing: {project['name']} has repo activity but no recent project update. Log what changed so Athena's project truth stays grounded.",
                "classification": "project_update",
                "linked_entity_kind": "project",
                "linked_entity_id": project["id"],
                "note": "Repo scan is newer than the latest project update.",
            }
        )
    return findings


def weekly_findings(db_path: Path | None = None) -> list[dict[str, Any]]:
    resolved_db = _resolve_db(db_path)
    ensure_db(resolved_db)
    review_cutoff = _days_ago(7)
    with connect_db(resolved_db) as conn:
        stale_projects = query_all(
            conn,
            """
            SELECT id, name
            FROM projects
            WHERE status IN ('active', 'blocked') AND (last_reviewed_at IS NULL OR last_reviewed_at <= ?)
            ORDER BY updated_at ASC
            """,
            (review_cutoff,),
        )
        stale_goals = query_all(
            conn,
            """
            SELECT id, title
            FROM life_goals
            WHERE status = 'active' AND (last_reviewed_at IS NULL OR last_reviewed_at <= ?)
            ORDER BY updated_at ASC
            """,
            (review_cutoff,),
        )

    findings: list[dict[str, Any]] = []
    for project in stale_projects:
        findings.append(
            {
                "dedupe_key": f"review:weekly:project:{project['id']}",
                "raw_text": f"Weekly project review needed: {project['name']}. Update status, health, blocker, and next milestone.",
                "classification": "project_update",
                "linked_entity_kind": "project",
                "linked_entity_id": project["id"],
                "note": "Project review overdue.",
            }
        )
    for goal in stale_goals:
        findings.append(
            {
                "dedupe_key": f"review:weekly:life-goal:{goal['id']}",
                "raw_text": f"Weekly life-goal review needed: {goal['title']}. Confirm focus, risk, and what this goal should shape this week.",
                "classification": "life_update",
                "linked_entity_kind": "life_goal",
                "linked_entity_id": goal["id"],
                "note": "Life-goal review overdue.",
            }
        )
    return findings


def monthly_findings(db_path: Path | None = None) -> list[dict[str, Any]]:
    resolved_db = _resolve_db(db_path)
    ensure_db(resolved_db)
    stale_cutoff = _days_ago(30)
    with connect_db(resolved_db) as conn:
        stale_projects = query_all(
            conn,
            """
            SELECT id, name
            FROM projects
            WHERE status IN ('queued', 'active', 'blocked') AND (last_real_progress_at IS NULL OR last_real_progress_at <= ?)
            ORDER BY updated_at ASC
            """,
            (stale_cutoff,),
        )

    findings: list[dict[str, Any]] = []
    for project in stale_projects:
        findings.append(
            {
                "dedupe_key": f"review:monthly:stale-project:{project['id']}",
                "raw_text": f"Monthly stale-project review: {project['name']} has had no real progress for 30 days. Decide whether to recommit, park, or close it.",
                "classification": "project_update",
                "linked_entity_kind": "project",
                "linked_entity_id": project["id"],
                "note": "Project has gone 30 days without real progress.",
            }
        )
    return findings


def run_review_cycle(cadence: str, db_path: Path | None = None, actor: str = "athena") -> dict[str, Any]:
    if cadence not in {"daily", "weekly", "monthly"}:
        raise ValueError(f"Unsupported review cadence: {cadence}")

    resolved_db = _resolve_db(db_path)
    ensure_db(resolved_db)
    refresh_rollups(resolved_db)

    if cadence == "daily":
        findings = daily_findings(resolved_db)
    elif cadence == "weekly":
        findings = weekly_findings(resolved_db)
    else:
        findings = monthly_findings(resolved_db)

    created_items = 0
    for finding in findings:
        with connect_db(resolved_db) as conn:
            existing = query_one(conn, "SELECT id FROM captured_items WHERE dedupe_key = ?", (finding["dedupe_key"],))
        if existing is None:
            created_items += 1
        capture_item(
            db_path=resolved_db,
            raw_text=str(finding["raw_text"]),
            source_channel="system",
            source_chat_id=f"review:{cadence}",
            source_message_ref=str(finding["dedupe_key"]),
            classification=str(finding["classification"]),
            linked_entity_kind=finding.get("linked_entity_kind"),
            linked_entity_id=finding.get("linked_entity_id"),
            status="new",
            note=finding.get("note"),
            dedupe_key=str(finding["dedupe_key"]),
        )

    with connect_db(resolved_db) as conn:
        conn.execute(
            """
            INSERT INTO review_runs (cadence, findings_count, created_items_count, actor, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (cadence, len(findings), created_items, actor, now_ts()),
        )
        conn.commit()

    render(db_path=resolved_db)
    return {
        "cadence": cadence,
        "findings_count": len(findings),
        "created_items_count": created_items,
        "db": str(resolved_db),
    }
