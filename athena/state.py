from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .config import default_paths
from .db import connect_db, ensure_db, now_ts, query_one, row_to_dict, slugify
from .render_markdown import render

DEFAULT_CHANNEL = "telegram"
DEFAULT_CHAT_ID = "1937792843"
TASK_RESOLUTION_MAP = {
    "done": "done",
    "cancelled": "cancelled",
    "superseded": "cancelled",
    "merged": "cancelled",
}
ACTIVE_TASK_STATUSES = {"queued", "in_progress", "blocked", "someday"}


class StateTransitionError(ValueError):
    pass


def _resolve_db(db_path: Path | None = None) -> Path:
    return (db_path or default_paths().db_path).expanduser().resolve()


def _render_views(db_path: Path) -> None:
    from .rollups import refresh_rollups

    refresh_rollups(db_path)
    render(db_path=db_path)


def _active_bucket_for_owner(owner: str) -> str:
    return "FLEIRE" if owner == "FLEIRE" else "ATHENA"


def _bucket_for_status(owner: str, status: str, current_bucket: str | None = None) -> str:
    if status == "blocked":
        return "BLOCKED"
    if status == "someday":
        return "SOMEDAY"
    if current_bucket and current_bucket not in {"BLOCKED", "SOMEDAY"}:
        return current_bucket
    return _active_bucket_for_owner(owner)


def _ensure_row(conn: sqlite3.Connection, table_name: str, entity_id: str) -> dict[str, Any]:
    row = row_to_dict(conn.execute(f"SELECT * FROM {table_name} WHERE id = ?", (entity_id,)).fetchone())
    if row is None:
        raise StateTransitionError(f"{table_name} row not found: {entity_id}")
    return row


def _update_row(conn: sqlite3.Connection, table_name: str, entity_id: str, updates: dict[str, Any]) -> None:
    if not updates:
        return
    assignments = ", ".join([f"{key} = ?" for key in updates])
    values = [updates[key] for key in updates] + [entity_id]
    conn.execute(f"UPDATE {table_name} SET {assignments} WHERE id = ?", values)


def _insert_task_event(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    event_type: str,
    from_status: str | None,
    to_status: str | None,
    note: str | None,
    actor: str,
) -> None:
    conn.execute(
        """
        INSERT INTO task_events (task_id, event_type, from_status, to_status, note, actor, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (task_id, event_type, from_status, to_status, note, actor, now_ts()),
    )


def _insert_project_update(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    summary: str,
    actor: str,
    wins: str | None = None,
    risks: str | None = None,
    next_7_days: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO project_updates (project_id, summary, wins, risks, next_7_days, actor, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (project_id, summary, wins, risks, next_7_days, actor, now_ts()),
    )


def _insert_completion_record(
    conn: sqlite3.Connection,
    *,
    entity_kind: str,
    entity_id: str,
    resolution: str,
    summary: str,
    actor: str,
    evidence: list[str] | None = None,
    verified_by: str | None = None,
) -> int:
    evidence_json = json.dumps(evidence or [], ensure_ascii=True)
    completed_at = now_ts()
    verified_at = completed_at if verified_by else None
    cursor = conn.execute(
        """
        INSERT INTO completion_records (
          entity_kind, entity_id, resolution, summary, evidence_json, completed_by, verified_by, completed_at, verified_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            entity_kind,
            entity_id,
            resolution,
            summary.strip(),
            evidence_json,
            actor,
            verified_by,
            completed_at,
            verified_at,
        ),
    )
    return int(cursor.lastrowid)


def _make_task_id(conn: sqlite3.Connection, title: str) -> str:
    base = f"task-{slugify(title)}"
    candidate = base
    while query_one(conn, "SELECT id FROM tasks WHERE id = ?", (candidate,)) is not None:
        candidate = f"{base}-{now_ts()}"
    return candidate


def _make_capture_id(conn: sqlite3.Connection, raw_text: str) -> str:
    base = f"capture-{slugify(raw_text[:80])}"
    candidate = base
    while query_one(conn, "SELECT id FROM captured_items WHERE id = ?", (candidate,)) is not None:
        candidate = f"{base}-{now_ts()}"
    return candidate


def _fetch_capture_by_dedupe(conn: sqlite3.Connection, dedupe_key: str | None) -> dict[str, Any] | None:
    if not dedupe_key:
        return None
    return query_one(conn, "SELECT * FROM captured_items WHERE dedupe_key = ?", (dedupe_key,))


def capture_item(
    *,
    raw_text: str,
    db_path: Path | None = None,
    source_channel: str = DEFAULT_CHANNEL,
    source_chat_id: str = DEFAULT_CHAT_ID,
    source_message_ref: str | None = None,
    classification: str | None = None,
    linked_entity_kind: str | None = None,
    linked_entity_id: str | None = None,
    status: str | None = None,
    note: str | None = None,
    dedupe_key: str | None = None,
    capture_id: str | None = None,
) -> dict[str, Any]:
    resolved_db = _resolve_db(db_path)
    ensure_db(resolved_db)
    now = now_ts()
    if dedupe_key is None and source_message_ref:
        dedupe_key = f"capture:{source_channel}:{source_chat_id}:{source_message_ref}"

    with connect_db(resolved_db) as conn:
        existing = None
        if capture_id:
            existing = row_to_dict(conn.execute("SELECT * FROM captured_items WHERE id = ?", (capture_id,)).fetchone())
        if existing is None:
            existing = _fetch_capture_by_dedupe(conn, dedupe_key)

        if existing is None:
            capture_id = capture_id or _make_capture_id(conn, raw_text)
            resolved_status = status or "new"
            conn.execute(
                """
                INSERT INTO captured_items (
                  id, source_channel, source_chat_id, source_message_ref, dedupe_key, raw_text, classification,
                  linked_entity_kind, linked_entity_id, status, note, applied_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    capture_id,
                    source_channel,
                    source_chat_id,
                    source_message_ref,
                    dedupe_key,
                    raw_text.strip(),
                    classification,
                    linked_entity_kind,
                    linked_entity_id,
                    resolved_status,
                    note,
                    now if resolved_status == "applied" else None,
                    now,
                    now,
                ),
            )
        else:
            capture_id = str(existing["id"])
            resolved_status = status or str(existing["status"])
            updates = {
                "source_channel": source_channel or existing["source_channel"],
                "source_chat_id": source_chat_id or existing["source_chat_id"],
                "source_message_ref": source_message_ref if source_message_ref is not None else existing["source_message_ref"],
                "dedupe_key": dedupe_key if dedupe_key is not None else existing["dedupe_key"],
                "raw_text": raw_text.strip(),
                "classification": classification if classification is not None else existing["classification"],
                "linked_entity_kind": linked_entity_kind if linked_entity_kind is not None else existing["linked_entity_kind"],
                "linked_entity_id": linked_entity_id if linked_entity_id is not None else existing["linked_entity_id"],
                "status": resolved_status,
                "note": note if note is not None else existing["note"],
                "updated_at": now,
            }
            if resolved_status == "applied" and not existing["applied_at"]:
                updates["applied_at"] = now
            _update_row(conn, "captured_items", capture_id, updates)
        conn.commit()
        result = _ensure_row(conn, "captured_items", capture_id)
    _render_views(resolved_db)
    return result


def triage_capture(
    *,
    capture_id: str,
    classification: str,
    db_path: Path | None = None,
    linked_entity_kind: str | None = None,
    linked_entity_id: str | None = None,
    status: str = "triaged",
    note: str | None = None,
) -> dict[str, Any]:
    resolved_db = _resolve_db(db_path)
    ensure_db(resolved_db)
    with connect_db(resolved_db) as conn:
        existing = _ensure_row(conn, "captured_items", capture_id)
        updates = {
            "classification": classification,
            "linked_entity_kind": linked_entity_kind if linked_entity_kind is not None else existing["linked_entity_kind"],
            "linked_entity_id": linked_entity_id if linked_entity_id is not None else existing["linked_entity_id"],
            "status": status,
            "note": note if note is not None else existing["note"],
            "updated_at": now_ts(),
        }
        if status == "applied":
            updates["applied_at"] = now_ts()
        _update_row(conn, "captured_items", capture_id, updates)
        conn.commit()
        result = _ensure_row(conn, "captured_items", capture_id)
    _render_views(resolved_db)
    return result


def create_task(
    *,
    title: str,
    owner: str,
    bucket: str | None = None,
    status: str = "queued",
    db_path: Path | None = None,
    task_id: str | None = None,
    project_id: str | None = None,
    portfolio_id: str | None = None,
    life_area_id: str | None = None,
    life_goal_id: str | None = None,
    workstream_id: str | None = None,
    source_text: str | None = None,
    why_now: str | None = None,
    next_action: str | None = None,
    blocker: str | None = None,
    notes: str | None = None,
    requires_approval: bool = False,
    requires_browser: bool = False,
    required_for_project_completion: bool = True,
    source_channel: str | None = None,
    source_chat_id: str | None = None,
    source_message_ref: str | None = None,
    dedupe_key: str | None = None,
    capture_id: str | None = None,
    priority: int = 0,
    actor: str = "athena",
) -> dict[str, Any]:
    resolved_db = _resolve_db(db_path)
    ensure_db(resolved_db)
    now = now_ts()
    with connect_db(resolved_db) as conn:
        if dedupe_key:
            existing = query_one(conn, "SELECT * FROM tasks WHERE dedupe_key = ?", (dedupe_key,))
            if existing is not None:
                return existing
        task_id = task_id or _make_task_id(conn, title)
        resolved_bucket = bucket or _bucket_for_status(owner, status)
        conn.execute(
            """
            INSERT INTO tasks (
              id, capture_id, life_area_id, life_goal_id, portfolio_id, project_id, workstream_id, title,
              owner, bucket, status, priority, source_text, why_now, next_action, blocker, notes,
              requires_approval, requires_browser, required_for_project_completion, dedupe_key,
              source_channel, source_chat_id, source_message_ref, created_at, updated_at, last_touched_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                capture_id,
                life_area_id,
                life_goal_id,
                portfolio_id,
                project_id,
                workstream_id,
                title.strip(),
                owner,
                resolved_bucket,
                status,
                int(priority),
                source_text,
                why_now,
                next_action,
                blocker,
                notes,
                int(requires_approval),
                int(requires_browser),
                int(required_for_project_completion),
                dedupe_key,
                source_channel,
                source_chat_id,
                source_message_ref,
                now,
                now,
                now,
            ),
        )
        _insert_task_event(
            conn,
            task_id=task_id,
            event_type="task_created",
            from_status=None,
            to_status=status,
            note=f"Created task: {title.strip()}",
            actor=actor,
        )
        if capture_id:
            _update_row(
                conn,
                "captured_items",
                capture_id,
                {
                    "classification": "task",
                    "linked_entity_kind": "task",
                    "linked_entity_id": task_id,
                    "status": "applied",
                    "applied_at": now,
                    "updated_at": now,
                },
            )
        conn.commit()
        result = _ensure_row(conn, "tasks", task_id)
    _render_views(resolved_db)
    return result


def start_task(
    *,
    task_id: str,
    db_path: Path | None = None,
    actor: str = "athena",
    next_action: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    resolved_db = _resolve_db(db_path)
    ensure_db(resolved_db)
    with connect_db(resolved_db) as conn:
        task = _ensure_row(conn, "tasks", task_id)
        if task["status"] in {"done", "cancelled"}:
            raise StateTransitionError(f"Cannot start closed task: {task_id}")
        updates = {
            "status": "in_progress",
            "bucket": _bucket_for_status(str(task["owner"]), "in_progress", str(task["bucket"])),
            "blocker": None,
            "updated_at": now_ts(),
            "last_touched_at": now_ts(),
        }
        if next_action is not None:
            updates["next_action"] = next_action
        _update_row(conn, "tasks", task_id, updates)
        _insert_task_event(
            conn,
            task_id=task_id,
            event_type="status_changed",
            from_status=str(task["status"]),
            to_status="in_progress",
            note=note or "Task started.",
            actor=actor,
        )
        conn.commit()
        result = _ensure_row(conn, "tasks", task_id)
    _render_views(resolved_db)
    return result


def block_task(
    *,
    task_id: str,
    blocker: str,
    db_path: Path | None = None,
    actor: str = "athena",
    next_action: str | None = None,
    note: str | None = None,
    requires_approval: bool | None = None,
    requires_browser: bool | None = None,
) -> dict[str, Any]:
    resolved_db = _resolve_db(db_path)
    ensure_db(resolved_db)
    now = now_ts()
    with connect_db(resolved_db) as conn:
        task = _ensure_row(conn, "tasks", task_id)
        updates: dict[str, Any] = {
            "status": "blocked",
            "bucket": "BLOCKED",
            "blocker": blocker.strip(),
            "updated_at": now,
            "last_touched_at": now,
        }
        if next_action is not None:
            updates["next_action"] = next_action
        if requires_approval is not None:
            updates["requires_approval"] = int(requires_approval)
        if requires_browser is not None:
            updates["requires_browser"] = int(requires_browser)
        _update_row(conn, "tasks", task_id, updates)
        _insert_task_event(
            conn,
            task_id=task_id,
            event_type="status_changed",
            from_status=str(task["status"]),
            to_status="blocked",
            note=note or blocker.strip(),
            actor=actor,
        )
        conn.commit()
        result = _ensure_row(conn, "tasks", task_id)
    _render_views(resolved_db)
    return result


def complete_task(
    *,
    task_id: str,
    summary: str,
    db_path: Path | None = None,
    actor: str = "athena",
    resolution: str = "done",
    evidence: list[str] | None = None,
    verified_by: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    if resolution not in TASK_RESOLUTION_MAP:
        raise StateTransitionError(f"Unsupported task resolution: {resolution}")
    if not summary.strip():
        raise StateTransitionError("Task completion requires a non-empty summary")
    resolved_db = _resolve_db(db_path)
    ensure_db(resolved_db)
    with connect_db(resolved_db) as conn:
        task = _ensure_row(conn, "tasks", task_id)
        if task["status"] in {"done", "cancelled"}:
            raise StateTransitionError(f"Task is already closed: {task_id}")
        completion_record_id = _insert_completion_record(
            conn,
            entity_kind="task",
            entity_id=task_id,
            resolution=resolution,
            summary=summary,
            evidence=evidence,
            actor=actor,
            verified_by=verified_by,
        )
        final_status = TASK_RESOLUTION_MAP[resolution]
        now = now_ts()
        _update_row(
            conn,
            "tasks",
            task_id,
            {
                "status": final_status,
                "resolution": resolution,
                "completion_summary": summary.strip(),
                "completion_record_id": completion_record_id,
                "closed_at": now,
                "updated_at": now,
                "last_touched_at": now,
            },
        )
        _insert_task_event(
            conn,
            task_id=task_id,
            event_type="task_completed",
            from_status=str(task["status"]),
            to_status=final_status,
            note=note or summary.strip(),
            actor=actor,
        )
        conn.commit()
        result = _ensure_row(conn, "tasks", task_id)
    _render_views(resolved_db)
    return result


def reopen_task(
    *,
    task_id: str,
    reason: str,
    db_path: Path | None = None,
    actor: str = "athena",
    status: str = "queued",
    bucket: str | None = None,
    next_action: str | None = None,
) -> dict[str, Any]:
    if not reason.strip():
        raise StateTransitionError("Reopening a task requires a reason")
    resolved_db = _resolve_db(db_path)
    ensure_db(resolved_db)
    with connect_db(resolved_db) as conn:
        task = _ensure_row(conn, "tasks", task_id)
        if task["status"] not in {"done", "cancelled"}:
            raise StateTransitionError(f"Task is not closed: {task_id}")
        now = now_ts()
        _update_row(
            conn,
            "tasks",
            task_id,
            {
                "status": status,
                "bucket": bucket or _bucket_for_status(str(task["owner"]), status, str(task["bucket"])),
                "resolution": None,
                "completion_summary": None,
                "completion_record_id": None,
                "closed_at": None,
                "reopened_at": now,
                "reopen_reason": reason.strip(),
                "updated_at": now,
                "last_touched_at": now,
                "next_action": next_action if next_action is not None else task["next_action"],
            },
        )
        _insert_task_event(
            conn,
            task_id=task_id,
            event_type="task_reopened",
            from_status=str(task["status"]),
            to_status=status,
            note=reason.strip(),
            actor=actor,
        )
        conn.commit()
        result = _ensure_row(conn, "tasks", task_id)
    _render_views(resolved_db)
    return result


def update_project_status(
    *,
    project_id: str,
    db_path: Path | None = None,
    actor: str = "athena",
    status: str | None = None,
    health: str | None = None,
    blocker: str | None = None,
    current_goal: str | None = None,
    next_milestone: str | None = None,
    summary: str | None = None,
    completion_summary: str | None = None,
    completion_resolution: str = "done",
    wins: str | None = None,
    risks: str | None = None,
    next_7_days: str | None = None,
) -> dict[str, Any]:
    from .rollups import project_completion_ready

    resolved_db = _resolve_db(db_path)
    ensure_db(resolved_db)
    with connect_db(resolved_db) as conn:
        project = _ensure_row(conn, "projects", project_id)
        now = now_ts()
        updates: dict[str, Any] = {
            "updated_at": now,
            "last_reviewed_at": now,
        }
        if status is not None:
            updates["status"] = status
            updates["status_source"] = "manual"
        if health is not None:
            updates["health"] = health
            updates["health_source"] = "manual"
        if blocker is not None:
            updates["blocker"] = blocker
        if current_goal is not None:
            updates["current_goal"] = current_goal
        if next_milestone is not None:
            updates["next_milestone"] = next_milestone

        is_closing = status in {"done", "cancelled"}
        if is_closing:
            if status == "done" and not project_completion_ready(conn, project_id):
                raise StateTransitionError(
                    f"Project {project_id} still has required open work and cannot be marked done"
                )
            if not completion_summary or not completion_summary.strip():
                raise StateTransitionError("Project completion requires a completion summary")
            completion_record_id = _insert_completion_record(
                conn,
                entity_kind="project",
                entity_id=project_id,
                resolution=completion_resolution if status == "cancelled" else "done",
                summary=completion_summary,
                evidence=[summary] if summary else None,
                actor=actor,
            )
            updates["blocker"] = None
            updates["completion_summary"] = completion_summary.strip()
            updates["completion_record_id"] = completion_record_id
            updates["completion_mode"] = "manual"
            updates["last_real_progress_at"] = now
        elif summary:
            updates["last_real_progress_at"] = now

        _update_row(conn, "projects", project_id, updates)
        if summary:
            _insert_project_update(
                conn,
                project_id=project_id,
                summary=summary,
                wins=wins,
                risks=risks,
                next_7_days=next_7_days,
                actor=actor,
            )
        conn.commit()
        result = _ensure_row(conn, "projects", project_id)
    _render_views(resolved_db)
    return result


def review_life_goal(
    *,
    goal_id: str,
    db_path: Path | None = None,
    actor: str = "athena",
    status: str | None = None,
    current_focus: str | None = None,
    status_note: str | None = None,
    risk_if_ignored: str | None = None,
    supporting_rule: str | None = None,
    next_review_at: int | None = None,
) -> dict[str, Any]:
    resolved_db = _resolve_db(db_path)
    ensure_db(resolved_db)
    with connect_db(resolved_db) as conn:
        goal = _ensure_row(conn, "life_goals", goal_id)
        now = now_ts()
        updates: dict[str, Any] = {
            "last_reviewed_at": now,
            "updated_at": now,
        }
        if status is not None:
            updates["status"] = status
        if current_focus is not None:
            updates["current_focus"] = current_focus
        if status_note is not None:
            updates["status_note"] = status_note
        if risk_if_ignored is not None:
            updates["risk_if_ignored"] = risk_if_ignored
        if supporting_rule is not None:
            updates["supporting_rule"] = supporting_rule
        if next_review_at is not None:
            updates["next_review_at"] = next_review_at
        _update_row(conn, "life_goals", goal_id, updates)
        conn.commit()
        result = _ensure_row(conn, "life_goals", goal_id)
    _render_views(resolved_db)
    return result


def set_chat_focus(
    *,
    db_path: Path | None = None,
    channel: str = DEFAULT_CHANNEL,
    chat_id: str = DEFAULT_CHAT_ID,
    current_capture_id: str | None = None,
    active_life_area_id: str | None = None,
    active_life_goal_id: str | None = None,
    current_portfolio_id: str | None = None,
    current_project_id: str | None = None,
    current_task_id: str | None = None,
    last_user_intent: str | None = None,
    last_progress: str | None = None,
    pending_approval_task_id: str | None = None,
) -> dict[str, Any]:
    resolved_db = _resolve_db(db_path)
    ensure_db(resolved_db)
    now = now_ts()
    with connect_db(resolved_db) as conn:
        existing = query_one(
            conn,
            "SELECT * FROM chat_state WHERE channel = ? AND chat_id = ?",
            (channel, chat_id),
        )
        if existing is None:
            conn.execute(
                """
                INSERT INTO chat_state (
                  channel, chat_id, current_capture_id, active_life_area_id, active_life_goal_id,
                  current_portfolio_id, current_project_id, current_task_id, last_user_intent,
                  last_progress, pending_approval_task_id, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    channel,
                    chat_id,
                    current_capture_id,
                    active_life_area_id,
                    active_life_goal_id,
                    current_portfolio_id,
                    current_project_id,
                    current_task_id,
                    last_user_intent,
                    last_progress,
                    pending_approval_task_id,
                    now,
                ),
            )
        else:
            updates = {
                "current_capture_id": current_capture_id if current_capture_id is not None else existing["current_capture_id"],
                "active_life_area_id": active_life_area_id if active_life_area_id is not None else existing["active_life_area_id"],
                "active_life_goal_id": active_life_goal_id if active_life_goal_id is not None else existing["active_life_goal_id"],
                "current_portfolio_id": current_portfolio_id if current_portfolio_id is not None else existing["current_portfolio_id"],
                "current_project_id": current_project_id if current_project_id is not None else existing["current_project_id"],
                "current_task_id": current_task_id if current_task_id is not None else existing["current_task_id"],
                "last_user_intent": last_user_intent if last_user_intent is not None else existing["last_user_intent"],
                "last_progress": last_progress if last_progress is not None else existing["last_progress"],
                "pending_approval_task_id": pending_approval_task_id if pending_approval_task_id is not None else existing["pending_approval_task_id"],
                "updated_at": now,
            }
            assignments = ", ".join([f"{key} = ?" for key in updates])
            values = [updates[key] for key in updates] + [channel, chat_id]
            conn.execute(
                f"UPDATE chat_state SET {assignments} WHERE channel = ? AND chat_id = ?",
                values,
            )
        conn.commit()
        result = query_one(
            conn,
            "SELECT * FROM chat_state WHERE channel = ? AND chat_id = ?",
            (channel, chat_id),
        )
    _render_views(resolved_db)
    return result or {}
