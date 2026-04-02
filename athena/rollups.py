from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import default_paths
from .db import connect_db, ensure_db, now_ts, query_all, query_one


def _resolve_db(db_path: Path | None = None) -> Path:
    return (db_path or default_paths().db_path).expanduser().resolve()


def project_completion_ready(conn, project_id: str) -> bool:
    counts = query_one(
        conn,
        """
        SELECT
          SUM(CASE WHEN required_for_project_completion = 1 AND status IN ('queued', 'in_progress', 'blocked', 'someday') THEN 1 ELSE 0 END) AS open_required_tasks
        FROM tasks
        WHERE project_id = ?
        """,
        (project_id,),
    ) or {}
    return int(counts.get("open_required_tasks") or 0) == 0


def compute_project_rollup(conn, project_id: str) -> dict[str, Any]:
    project = query_one(conn, "SELECT * FROM projects WHERE id = ?", (project_id,))
    if project is None:
        raise ValueError(f"Project not found: {project_id}")

    task_counts = query_one(
        conn,
        """
        SELECT
          SUM(CASE WHEN required_for_project_completion = 1 AND status IN ('queued', 'in_progress', 'blocked', 'someday') THEN 1 ELSE 0 END) AS open_required_tasks,
          SUM(CASE WHEN required_for_project_completion = 1 AND status IN ('done', 'cancelled') THEN 1 ELSE 0 END) AS closed_required_tasks,
          SUM(CASE WHEN status = 'blocked' THEN 1 ELSE 0 END) AS blocked_tasks,
          SUM(CASE WHEN status = 'in_progress' THEN 1 ELSE 0 END) AS in_progress_tasks,
          SUM(CASE WHEN status = 'queued' THEN 1 ELSE 0 END) AS queued_tasks
        FROM tasks
        WHERE project_id = ?
        """,
        (project_id,),
    ) or {}
    repo_counts = query_one(
        conn,
        """
        SELECT
          COUNT(*) AS repo_count,
          SUM(CASE WHEN last_seen_dirty = 1 THEN 1 ELSE 0 END) AS dirty_repos,
          MAX(last_scanned_at) AS last_repo_scan_at
        FROM project_repos
        WHERE project_id = ?
        """,
        (project_id,),
    ) or {}
    last_update = query_one(
        conn,
        "SELECT MAX(created_at) AS last_project_update_at FROM project_updates WHERE project_id = ?",
        (project_id,),
    ) or {}

    open_required = int(task_counts.get("open_required_tasks") or 0)
    closed_required = int(task_counts.get("closed_required_tasks") or 0)
    blocked_tasks = int(task_counts.get("blocked_tasks") or 0)
    in_progress_tasks = int(task_counts.get("in_progress_tasks") or 0)
    queued_tasks = int(task_counts.get("queued_tasks") or 0)
    dirty_repos = int(repo_counts.get("dirty_repos") or 0)
    repo_count = int(repo_counts.get("repo_count") or 0)

    if project["status"] in {"done", "cancelled"} and open_required == 0:
        derived_status = str(project["status"])
    elif blocked_tasks > 0:
        derived_status = "blocked"
    elif open_required > 0 or in_progress_tasks > 0 or queued_tasks > 0:
        derived_status = "active"
    elif closed_required > 0:
        derived_status = "done"
    else:
        derived_status = "queued"

    if blocked_tasks > 0 and in_progress_tasks == 0 and queued_tasks == 0:
        derived_health = "red"
    elif blocked_tasks > 0 or dirty_repos > 0:
        derived_health = "yellow"
    elif in_progress_tasks > 0 or queued_tasks > 0 or closed_required > 0:
        derived_health = "green"
    elif repo_count > 0 or last_update.get("last_project_update_at"):
        derived_health = "yellow"
    else:
        derived_health = "unknown"

    summary_parts = [
        f"{project['name']}: {open_required} open required",
        f"{blocked_tasks} blocked",
        f"{in_progress_tasks} in progress",
    ]
    if dirty_repos:
        summary_parts.append(f"{dirty_repos} dirty repo")
    if last_update.get("last_project_update_at"):
        summary_parts.append("recent project update logged")

    return {
        "project_id": project_id,
        "derived_status": derived_status,
        "derived_health": derived_health,
        "rollup_summary": ", ".join(summary_parts),
        "rollup_updated_at": now_ts(),
    }


def apply_project_rollups(conn, project_id: str | None = None) -> int:
    if project_id:
        project_ids = [project_id]
    else:
        rows = query_all(conn, "SELECT id FROM projects WHERE status IN ('queued', 'active', 'blocked', 'done')")
        project_ids = [str(row["id"]) for row in rows]

    updated = 0
    for current_project_id in project_ids:
        rollup = compute_project_rollup(conn, current_project_id)
        project = query_one(conn, "SELECT status_source, health_source FROM projects WHERE id = ?", (current_project_id,))
        updates: dict[str, Any] = {
            "derived_status": rollup["derived_status"],
            "derived_health": rollup["derived_health"],
            "rollup_summary": rollup["rollup_summary"],
            "rollup_updated_at": rollup["rollup_updated_at"],
        }
        if project and project.get("status_source") == "derived":
            updates["status"] = rollup["derived_status"]
        if project and project.get("health_source") == "derived":
            updates["health"] = rollup["derived_health"]
        assignments = ", ".join([f"{key} = ?" for key in updates])
        values = [updates[key] for key in updates] + [current_project_id]
        conn.execute(f"UPDATE projects SET {assignments} WHERE id = ?", values)
        updated += 1
    return updated


def compute_life_goal_rollup(conn, goal_id: str) -> dict[str, Any]:
    goal = query_one(conn, "SELECT * FROM life_goals WHERE id = ?", (goal_id,))
    if goal is None:
        raise ValueError(f"Life goal not found: {goal_id}")

    counts = query_one(
        conn,
        """
        SELECT
          SUM(CASE WHEN status IN ('active', 'blocked') THEN 1 ELSE 0 END) AS active_projects,
          SUM(CASE WHEN status = 'blocked' THEN 1 ELSE 0 END) AS blocked_projects
        FROM projects
        WHERE life_goal_id = ?
        """,
        (goal_id,),
    ) or {}
    task_counts = query_one(
        conn,
        """
        SELECT
          SUM(CASE WHEN status IN ('queued', 'in_progress', 'blocked', 'someday') THEN 1 ELSE 0 END) AS open_tasks
        FROM tasks
        WHERE life_goal_id = ?
        """,
        (goal_id,),
    ) or {}

    active_projects = int(counts.get("active_projects") or 0)
    blocked_projects = int(counts.get("blocked_projects") or 0)
    open_tasks = int(task_counts.get("open_tasks") or 0)

    if blocked_projects > 0:
        derived_status = "active"
    elif active_projects > 0 or open_tasks > 0:
        derived_status = "active"
    else:
        derived_status = "paused"

    derived_summary = (
        f"{goal['title']}: {active_projects} active projects, {blocked_projects} blocked projects, {open_tasks} open tasks."
    )
    return {
        "goal_id": goal_id,
        "derived_status": derived_status,
        "derived_summary": derived_summary,
        "rollup_updated_at": now_ts(),
    }


def apply_life_goal_rollups(conn, goal_id: str | None = None) -> int:
    if goal_id:
        goal_ids = [goal_id]
    else:
        rows = query_all(conn, "SELECT id FROM life_goals WHERE status IN ('active', 'paused')")
        goal_ids = [str(row["id"]) for row in rows]

    updated = 0
    for current_goal_id in goal_ids:
        rollup = compute_life_goal_rollup(conn, current_goal_id)
        conn.execute(
            """
            UPDATE life_goals
            SET derived_status = ?, derived_summary = ?, rollup_updated_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                rollup["derived_status"],
                rollup["derived_summary"],
                rollup["rollup_updated_at"],
                rollup["rollup_updated_at"],
                current_goal_id,
            ),
        )
        updated += 1
    return updated


def refresh_rollups(db_path: Path | None = None) -> dict[str, int]:
    resolved_db = _resolve_db(db_path)
    ensure_db(resolved_db)
    with connect_db(resolved_db) as conn:
        projects_updated = apply_project_rollups(conn)
        goals_updated = apply_life_goal_rollups(conn)
        conn.commit()
    return {"projects_updated": projects_updated, "goals_updated": goals_updated}
