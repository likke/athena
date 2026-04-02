from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .config import default_paths
from .db import connect_db, ensure_db, now_ts, row_to_dict
from .google import search_gmail_messages
from .outbox import (
    approve_outbox_items,
    create_email_outbox,
    reject_outbox_items,
    send_outbox_items,
)
from .render_markdown import render
from .reviews import run_review_cycle
from .rollups import refresh_rollups
from .state import (
    block_task,
    capture_item,
    create_task,
    review_life_goal,
    reopen_task,
    set_chat_focus,
    start_task,
    triage_capture,
    update_project_status,
    complete_task,
)

DEFAULT_CHANNEL = "telegram"
DEFAULT_CHAT_ID = "1937792843"
OPEN_TASK_STATUSES = ("queued", "in_progress", "blocked", "someday")
CLOSED_TASK_STATUSES = ("done", "cancelled")
OPEN_OUTBOX_STATUSES = ("drafting", "needs_approval", "approved", "sending", "error")
TASK_COLUMNS = (
    "capture_id",
    "life_area_id",
    "life_goal_id",
    "portfolio_id",
    "project_id",
    "workstream_id",
    "title",
    "owner",
    "bucket",
    "status",
    "priority",
    "source_text",
    "why_now",
    "next_action",
    "blocker",
    "notes",
    "requires_approval",
    "requires_browser",
    "required_for_project_completion",
    "dedupe_key",
    "source_channel",
    "source_chat_id",
    "source_message_ref",
    "resolution",
    "completion_summary",
    "completion_record_id",
    "reopened_at",
    "reopen_reason",
    "created_at",
    "updated_at",
    "last_touched_at",
    "closed_at",
)
CHAT_STATE_COLUMNS = (
    "current_capture_id",
    "active_life_area_id",
    "active_life_goal_id",
    "current_portfolio_id",
    "current_project_id",
    "current_task_id",
    "last_user_intent",
    "last_progress",
    "pending_approval_task_id",
    "updated_at",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="DB-first task state helper for Athena.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=None, help="Override tasks.sqlite path.")
    common.add_argument(
        "--render-script",
        default=None,
        help="Compatibility flag. Ignored; markdown rendering is handled by Python.",
    )

    apply_parser = subparsers.add_parser("apply", parents=[common], help="Apply a JSON update file.")
    apply_parser.add_argument("json_path", help="Path to JSON payload.")
    apply_parser.add_argument("--skip-render", action="store_true")

    current_parser = subparsers.add_parser("current", parents=[common], help="Show current state.")
    current_parser.add_argument("--channel", default=DEFAULT_CHANNEL)
    current_parser.add_argument("--chat-id", default=DEFAULT_CHAT_ID)

    capture_parser = subparsers.add_parser("capture", parents=[common], help="Capture raw input into the inbox.")
    capture_parser.add_argument("raw_text")
    capture_parser.add_argument("--source-channel", default=DEFAULT_CHANNEL)
    capture_parser.add_argument("--source-chat-id", default=DEFAULT_CHAT_ID)
    capture_parser.add_argument("--source-message-ref", default=None)
    capture_parser.add_argument("--classification", default=None)
    capture_parser.add_argument("--linked-entity-kind", default=None)
    capture_parser.add_argument("--linked-entity-id", default=None)
    capture_parser.add_argument("--status", default=None)
    capture_parser.add_argument("--note", default=None)
    capture_parser.add_argument("--dedupe-key", default=None)
    capture_parser.add_argument("--capture-id", default=None)

    triage_parser = subparsers.add_parser("triage-capture", parents=[common], help="Triage an inbox item.")
    triage_parser.add_argument("capture_id")
    triage_parser.add_argument("--classification", required=True)
    triage_parser.add_argument("--linked-entity-kind", default=None)
    triage_parser.add_argument("--linked-entity-id", default=None)
    triage_parser.add_argument("--status", default="triaged")
    triage_parser.add_argument("--note", default=None)

    create_parser = subparsers.add_parser("create-task", parents=[common], help="Create a task via the state layer.")
    create_parser.add_argument("title")
    create_parser.add_argument("--owner", required=True, choices=["ATHENA", "FLEIRE"])
    create_parser.add_argument("--bucket", default=None)
    create_parser.add_argument("--status", default="queued")
    create_parser.add_argument("--task-id", default=None)
    create_parser.add_argument("--project-id", default=None)
    create_parser.add_argument("--portfolio-id", default=None)
    create_parser.add_argument("--life-area-id", default=None)
    create_parser.add_argument("--life-goal-id", default=None)
    create_parser.add_argument("--workstream-id", default=None)
    create_parser.add_argument("--source-text", default=None)
    create_parser.add_argument("--why-now", default=None)
    create_parser.add_argument("--next-action", default=None)
    create_parser.add_argument("--blocker", default=None)
    create_parser.add_argument("--notes", default=None)
    create_parser.add_argument("--requires-approval", action="store_true")
    create_parser.add_argument("--requires-browser", action="store_true")
    create_parser.add_argument("--not-required-for-project-completion", action="store_true")
    create_parser.add_argument("--source-channel", default=None)
    create_parser.add_argument("--source-chat-id", default=None)
    create_parser.add_argument("--source-message-ref", default=None)
    create_parser.add_argument("--dedupe-key", default=None)
    create_parser.add_argument("--capture-id", default=None)
    create_parser.add_argument("--priority", type=int, default=0)
    create_parser.add_argument("--actor", default="athena")

    start_parser = subparsers.add_parser("start-task", parents=[common], help="Move a task into progress.")
    start_parser.add_argument("task_id")
    start_parser.add_argument("--next-action", default=None)
    start_parser.add_argument("--note", default=None)
    start_parser.add_argument("--actor", default="athena")

    block_parser = subparsers.add_parser("block-task", parents=[common], help="Block a task with a concrete reason.")
    block_parser.add_argument("task_id")
    block_parser.add_argument("--blocker", required=True)
    block_parser.add_argument("--next-action", default=None)
    block_parser.add_argument("--note", default=None)
    block_parser.add_argument("--requires-approval", action="store_true")
    block_parser.add_argument("--requires-browser", action="store_true")
    block_parser.add_argument("--actor", default="athena")

    complete_parser = subparsers.add_parser("complete-task", parents=[common], help="Complete a task with evidence.")
    complete_parser.add_argument("task_id")
    complete_parser.add_argument("--summary", required=True)
    complete_parser.add_argument("--resolution", default="done")
    complete_parser.add_argument("--evidence", action="append", default=[])
    complete_parser.add_argument("--verified-by", default=None)
    complete_parser.add_argument("--note", default=None)
    complete_parser.add_argument("--actor", default="athena")

    reopen_parser = subparsers.add_parser("reopen-task", parents=[common], help="Reopen a closed task.")
    reopen_parser.add_argument("task_id")
    reopen_parser.add_argument("--reason", required=True)
    reopen_parser.add_argument("--status", default="queued")
    reopen_parser.add_argument("--bucket", default=None)
    reopen_parser.add_argument("--next-action", default=None)
    reopen_parser.add_argument("--actor", default="athena")

    project_parser = subparsers.add_parser("project-status", parents=[common], help="Update project status or close it.")
    project_parser.add_argument("project_id")
    project_parser.add_argument("--status", default=None)
    project_parser.add_argument("--health", default=None)
    project_parser.add_argument("--blocker", default=None)
    project_parser.add_argument("--current-goal", default=None)
    project_parser.add_argument("--next-milestone", default=None)
    project_parser.add_argument("--summary", default=None)
    project_parser.add_argument("--completion-summary", default=None)
    project_parser.add_argument("--completion-resolution", default="done")
    project_parser.add_argument("--wins", default=None)
    project_parser.add_argument("--risks", default=None)
    project_parser.add_argument("--next-7-days", default=None)
    project_parser.add_argument("--actor", default="athena")

    goal_parser = subparsers.add_parser("review-goal", parents=[common], help="Review and update a life goal.")
    goal_parser.add_argument("goal_id")
    goal_parser.add_argument("--status", default=None)
    goal_parser.add_argument("--current-focus", default=None)
    goal_parser.add_argument("--status-note", default=None)
    goal_parser.add_argument("--risk-if-ignored", default=None)
    goal_parser.add_argument("--supporting-rule", default=None)
    goal_parser.add_argument("--next-review-at", type=int, default=None)
    goal_parser.add_argument("--actor", default="athena")

    review_parser = subparsers.add_parser("review-cycle", parents=[common], help="Run daily, weekly, or monthly reviews.")
    review_parser.add_argument("cadence", choices=["daily", "weekly", "monthly"])
    review_parser.add_argument("--actor", default="athena")

    rollup_parser = subparsers.add_parser("refresh-rollups", parents=[common], help="Refresh derived project and life rollups.")

    focus_parser = subparsers.add_parser("set-chat-focus", parents=[common], help="Update chat focus pointers.")
    focus_parser.add_argument("--channel", default=DEFAULT_CHANNEL)
    focus_parser.add_argument("--chat-id", default=DEFAULT_CHAT_ID)
    focus_parser.add_argument("--current-capture-id", default=None)
    focus_parser.add_argument("--active-life-area-id", default=None)
    focus_parser.add_argument("--active-life-goal-id", default=None)
    focus_parser.add_argument("--current-portfolio-id", default=None)
    focus_parser.add_argument("--current-project-id", default=None)
    focus_parser.add_argument("--current-task-id", default=None)
    focus_parser.add_argument("--last-user-intent", default=None)
    focus_parser.add_argument("--last-progress", default=None)
    focus_parser.add_argument("--pending-approval-task-id", default=None)

    outbox_parser = subparsers.add_parser("queue-email", parents=[common], help="Create a Gmail draft in Athena's approval queue.")
    outbox_parser.add_argument("--to", dest="to_recipients", required=True)
    outbox_parser.add_argument("--cc", dest="cc_recipients", default=None)
    outbox_parser.add_argument("--bcc", dest="bcc_recipients", default=None)
    outbox_parser.add_argument("--subject", required=True)
    outbox_parser.add_argument("--body", required=True)
    outbox_parser.add_argument("--task-id", default=None)
    outbox_parser.add_argument("--project-id", default=None)
    outbox_parser.add_argument("--account", dest="account_label", default=None)
    outbox_parser.add_argument("--actor", default="athena")

    approve_outbox_parser = subparsers.add_parser("approve-outbox", parents=[common], help="Approve one or more outbox items.")
    approve_outbox_parser.add_argument("item_ids", nargs="+")
    approve_outbox_parser.add_argument("--note", default=None)
    approve_outbox_parser.add_argument("--actor", default="athena")

    reject_outbox_parser = subparsers.add_parser("reject-outbox", parents=[common], help="Reject one or more outbox items.")
    reject_outbox_parser.add_argument("item_ids", nargs="+")
    reject_outbox_parser.add_argument("--note", default=None)
    reject_outbox_parser.add_argument("--actor", default="athena")

    send_outbox_parser = subparsers.add_parser("send-outbox", parents=[common], help="Send approved outbox items or a selected subset.")
    send_outbox_parser.add_argument("item_ids", nargs="*")
    send_outbox_parser.add_argument("--actor", default="athena")

    gmail_search_parser = subparsers.add_parser("gmail-search", parents=[common], help="Search Gmail through the API instead of the browser.")
    gmail_search_parser.add_argument("--query", required=True)
    gmail_search_parser.add_argument("--max-results", type=int, default=10)
    gmail_search_parser.add_argument("--account", default=None)

    return parser.parse_args()


def fetch_task(conn, task_id: str) -> dict[str, Any] | None:
    return row_to_dict(conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone())


def fetch_chat_state(conn, channel: str, chat_id: str) -> dict[str, Any] | None:
    return row_to_dict(
        conn.execute(
            "SELECT * FROM chat_state WHERE channel = ? AND chat_id = ?",
            (channel, chat_id),
        ).fetchone()
    )


def _latest_weekly_brief(conn) -> dict[str, Any] | None:
    return row_to_dict(
        conn.execute(
            """
            SELECT title, path, summary, last_synced_at
            FROM source_documents
            WHERE kind = 'weekly_ceo_brief'
            ORDER BY COALESCE(last_synced_at, updated_at) DESC, updated_at DESC
            LIMIT 1
            """
        ).fetchone()
    )


def _life_focus(conn) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT
              g.id,
              g.title,
              g.current_focus,
              g.supporting_rule,
              g.risk_if_ignored,
              g.status_note,
              g.derived_summary,
              g.last_reviewed_at,
              la.name AS area_name,
              la.priority AS area_priority
            FROM life_goals g
            JOIN life_areas la ON la.id = g.life_area_id
            WHERE g.status = 'active'
            ORDER BY la.priority DESC, g.updated_at DESC
            LIMIT 4
            """
        ).fetchall()
    ]


def _portfolio_focus(conn) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT
              p.id,
              p.name,
              p.status,
              p.health,
              p.current_goal,
              p.next_milestone,
              p.blocker,
              p.rollup_summary,
              p.last_real_progress_at,
              pf.name AS portfolio_name,
              pf.priority AS portfolio_priority
            FROM projects p
            JOIN portfolios pf ON pf.id = p.portfolio_id
            WHERE p.status IN ('active', 'blocked')
            ORDER BY
              pf.priority DESC,
              CASE p.status WHEN 'blocked' THEN 0 ELSE 1 END,
              CASE p.health WHEN 'red' THEN 0 WHEN 'yellow' THEN 1 ELSE 2 END,
              p.updated_at DESC
            LIMIT 6
            """
        ).fetchall()
    ]


def _calendar_agenda(conn) -> dict[str, Any] | None:
    agenda = row_to_dict(
        conn.execute(
            """
            SELECT title, path, summary, last_synced_at
            FROM source_documents
            WHERE kind = 'calendar_agenda'
            ORDER BY COALESCE(last_synced_at, updated_at) DESC, updated_at DESC
            LIMIT 1
            """
        ).fetchone()
    )
    if not agenda:
        return None

    lines: list[str] = []
    if agenda.get("path"):
        path = Path(str(agenda["path"])).expanduser().resolve()
        if path.exists():
            lines = [
                line.strip()
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip().startswith("- ")
            ][:6]
    agenda["lines"] = lines
    return agenda


def _recent_context_docs(conn) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT title, source_system, kind, summary, path, last_synced_at
            FROM source_documents
            WHERE kind IN (
              'external_context',
              'gmail_mailbox',
              'drive_file_summary',
              'notebooklm_export_summary',
              'notebooklm'
            )
            ORDER BY COALESCE(last_synced_at, updated_at) DESC, updated_at DESC
            LIMIT 6
            """
        ).fetchall()
    ]


def _status_counts(conn) -> dict[str, int]:
    task_counts = row_to_dict(
        conn.execute(
            """
            SELECT
              COALESCE(SUM(CASE WHEN status IN ('queued', 'in_progress', 'blocked', 'someday') THEN 1 ELSE 0 END), 0) AS open_tasks,
              COALESCE(SUM(CASE WHEN status = 'blocked' THEN 1 ELSE 0 END), 0) AS blocked_tasks,
              COALESCE(SUM(CASE WHEN status = 'in_progress' THEN 1 ELSE 0 END), 0) AS in_progress_tasks
            FROM tasks
            """
        ).fetchone()
    ) or {}
    approval_counts = row_to_dict(
        conn.execute(
            """
            SELECT
              COALESCE(SUM(CASE WHEN status = 'needs_approval' THEN 1 ELSE 0 END), 0) AS approvals_waiting,
              COALESCE(SUM(CASE WHEN status = 'approved' THEN 1 ELSE 0 END), 0) AS approvals_ready
            FROM outbox_items
            """
        ).fetchone()
    ) or {}
    return {
        "open_tasks": int(task_counts.get("open_tasks") or 0),
        "blocked_tasks": int(task_counts.get("blocked_tasks") or 0),
        "in_progress_tasks": int(task_counts.get("in_progress_tasks") or 0),
        "approvals_waiting": int(approval_counts.get("approvals_waiting") or 0),
        "approvals_ready": int(approval_counts.get("approvals_ready") or 0),
    }


def _sentence(text: str | None) -> str:
    clean = str(text or "").strip().rstrip(".!?")
    return f"{clean}." if clean else ""


def _founder_summary(
    *,
    weekly_brief: dict[str, Any] | None,
    life_focus: list[dict[str, Any]],
    portfolio_focus: list[dict[str, Any]],
    counts: dict[str, int],
) -> str:
    if weekly_brief and weekly_brief.get("summary"):
        return str(weekly_brief["summary"]).strip()

    parts: list[str] = []
    if life_focus:
        goal = life_focus[0]
        focus = _sentence(goal.get("current_focus") or goal.get("supporting_rule") or goal.get("derived_summary"))
        parts.append(f"Life focus: {goal['title']}. {focus}".strip())
    if portfolio_focus:
        project = portfolio_focus[0]
        next_step = _sentence(project.get("next_milestone") or project.get("current_goal") or project.get("rollup_summary"))
        parts.append(f"Project focus: {project['portfolio_name']} / {project['name']}. {next_step}".strip())
    if counts.get("approvals_waiting"):
        parts.append(f"Approval queue: {counts['approvals_waiting']} waiting.")
    elif counts.get("blocked_tasks"):
        parts.append(f"Blocked work: {counts['blocked_tasks']} blocked task(s).")
    return " | ".join(part for part in parts if part).strip()


def merge_task(existing: dict[str, Any] | None, payload: dict[str, Any]) -> dict[str, Any]:
    task_id = payload.get("id")
    if not task_id:
        raise ValueError("Task payload is missing required field: id")

    current_ts = now_ts()
    merged: dict[str, Any] = {"id": task_id}
    for column in TASK_COLUMNS:
        if column in payload:
            merged[column] = payload[column]
        elif existing is not None:
            merged[column] = existing[column]
        else:
            merged[column] = None

    if existing is None and not merged["title"]:
        raise ValueError(f"Task {task_id} is missing required field: title")
    if existing is None and not merged["owner"]:
        raise ValueError(f"Task {task_id} is missing required field: owner")
    if existing is None and not merged["bucket"]:
        raise ValueError(f"Task {task_id} is missing required field: bucket")
    if existing is None and not merged["status"]:
        raise ValueError(f"Task {task_id} is missing required field: status")

    merged["priority"] = int(merged["priority"] or 0)
    merged["requires_approval"] = int(merged["requires_approval"] or 0)
    merged["requires_browser"] = int(merged["requires_browser"] or 0)
    merged["required_for_project_completion"] = int(merged["required_for_project_completion"] or 0)
    merged["created_at"] = int(merged["created_at"] or current_ts)
    merged["updated_at"] = int(payload.get("updated_at") or current_ts)
    merged["last_touched_at"] = int(payload.get("last_touched_at") or current_ts)

    status = merged["status"]
    if status in CLOSED_TASK_STATUSES:
        merged["closed_at"] = int(merged["closed_at"] or current_ts)
    elif "closed_at" in payload:
        merged["closed_at"] = payload["closed_at"]
    else:
        merged["closed_at"] = None

    return merged


def write_task(conn, task: dict[str, Any]) -> None:
    columns = ("id",) + TASK_COLUMNS
    placeholders = ", ".join(["?"] * len(columns))
    assignments = ", ".join([f"{column} = excluded.{column}" for column in TASK_COLUMNS])
    values = [task[column] for column in columns]
    conn.execute(
        f"""
        INSERT INTO tasks ({", ".join(columns)})
        VALUES ({placeholders})
        ON CONFLICT(id) DO UPDATE SET
          {assignments}
        """,
        values,
    )


def maybe_insert_task_event(conn, existing: dict[str, Any] | None, task: dict[str, Any], payload: dict[str, Any]) -> bool:
    event = payload.get("event")
    event_type = payload.get("event_type")
    event_note = payload.get("event_note")
    event_actor = payload.get("event_actor")
    if isinstance(event, dict):
        event_type = event.get("type", event_type)
        event_note = event.get("note", event_note)
        event_actor = event.get("actor", event_actor)

    status_changed = existing is None or existing["status"] != task["status"]
    should_insert = any(
        [
            isinstance(event, dict),
            event_type is not None,
            event_note is not None,
            status_changed,
        ]
    )
    if not should_insert:
        return False

    resolved_event_type = event_type
    if not resolved_event_type:
        if existing is None:
            resolved_event_type = "task_created"
        elif status_changed:
            resolved_event_type = "status_changed"
        else:
            resolved_event_type = "task_updated"

    conn.execute(
        """
        INSERT INTO task_events (task_id, event_type, from_status, to_status, note, actor, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            task["id"],
            resolved_event_type,
            None if existing is None else existing["status"],
            task["status"],
            event_note,
            event_actor or "athena",
            now_ts(),
        ),
    )
    return True


def merge_chat_state(payload: dict[str, Any], existing: dict[str, Any] | None) -> dict[str, Any]:
    channel = str(payload.get("channel") or (existing["channel"] if existing else DEFAULT_CHANNEL))
    chat_id = str(payload.get("chat_id") or (existing["chat_id"] if existing else DEFAULT_CHAT_ID))
    merged: dict[str, Any] = {"channel": channel, "chat_id": chat_id}
    current_ts = now_ts()
    for column in CHAT_STATE_COLUMNS:
        if column in payload:
            merged[column] = payload[column]
        elif existing is not None:
            merged[column] = existing[column]
        else:
            merged[column] = None
    merged["updated_at"] = int(payload.get("updated_at") or current_ts)
    return merged


def write_chat_state(conn, chat_state: dict[str, Any]) -> None:
    columns = ("channel", "chat_id") + CHAT_STATE_COLUMNS
    placeholders = ", ".join(["?"] * len(columns))
    assignments = ", ".join([f"{column} = excluded.{column}" for column in CHAT_STATE_COLUMNS])
    values = [chat_state[column] for column in columns]
    conn.execute(
        f"""
        INSERT INTO chat_state ({", ".join(columns)})
        VALUES ({placeholders})
        ON CONFLICT(channel, chat_id) DO UPDATE SET
          {assignments}
        """,
        values,
    )


def apply_updates(db_path: Path, json_path: Path, skip_render: bool = False) -> dict[str, Any]:
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    upserts = payload.get("upserts", [])
    chat_state_payload = payload.get("chat_state")
    should_render = bool(payload.get("render", True)) and not skip_render

    if not isinstance(upserts, list):
        raise ValueError("upserts must be an array")
    if chat_state_payload is not None and not isinstance(chat_state_payload, dict):
        raise ValueError("chat_state must be an object when provided")

    ensure_db(db_path)
    tasks_upserted = 0
    events_written = 0
    chat_state_updated = False
    with connect_db(db_path) as conn:
        for item in upserts:
            if not isinstance(item, dict):
                raise ValueError("Each upsert entry must be an object")
            task_id = item.get("id")
            if not task_id:
                raise ValueError("Each upsert entry must include id")
            existing = fetch_task(conn, str(task_id))
            merged = merge_task(existing, item)
            write_task(conn, merged)
            tasks_upserted += 1
            if maybe_insert_task_event(conn, existing, merged, item):
                events_written += 1

        if chat_state_payload is not None:
            existing_chat = fetch_chat_state(
                conn,
                str(chat_state_payload.get("channel") or DEFAULT_CHANNEL),
                str(chat_state_payload.get("chat_id") or DEFAULT_CHAT_ID),
            )
            write_chat_state(conn, merge_chat_state(chat_state_payload, existing_chat))
            chat_state_updated = True

        conn.commit()

    if should_render:
        refresh_rollups(db_path=db_path)
        render(db_path=db_path)
    return {
        "ok": True,
        "tasks_upserted": tasks_upserted,
        "events_written": events_written,
        "chat_state_updated": chat_state_updated,
        "rendered": should_render,
        "db": str(db_path),
    }


def current_state(db_path: Path, channel: str, chat_id: str) -> dict[str, Any]:
    ensure_db(db_path)
    with connect_db(db_path) as conn:
        current = row_to_dict(
            conn.execute(
                """
                SELECT
                  c.channel,
                  c.chat_id,
                  c.current_capture_id,
                  c.active_life_area_id,
                  c.active_life_goal_id,
                  c.current_task_id,
                  c.current_project_id,
                  c.current_portfolio_id,
                  c.pending_approval_task_id,
                  c.last_user_intent,
                  c.last_progress,
                  c.updated_at,
                  ci.raw_text AS current_capture_text,
                  ci.status AS current_capture_status,
                  t.title AS current_task_title,
                  t.status AS current_task_status,
                  t.next_action AS current_task_next_action,
                  pt.title AS pending_approval_title,
                  la.name AS active_life_area_name,
                  lg.title AS active_life_goal_title,
                  p.name AS current_project_name,
                  pf.name AS current_portfolio_name
                FROM chat_state c
                LEFT JOIN captured_items ci ON ci.id = c.current_capture_id
                LEFT JOIN tasks t ON t.id = c.current_task_id
                LEFT JOIN tasks pt ON pt.id = c.pending_approval_task_id
                LEFT JOIN life_areas la ON la.id = c.active_life_area_id
                LEFT JOIN life_goals lg ON lg.id = c.active_life_goal_id
                LEFT JOIN projects p ON p.id = c.current_project_id
                LEFT JOIN portfolios pf ON pf.id = c.current_portfolio_id
                WHERE c.channel = ? AND c.chat_id = ?
                """,
                (channel, chat_id),
            ).fetchone()
        )
        open_tasks = [
            dict(row)
            for row in conn.execute(
                """
                SELECT id, title, owner, bucket, status, priority, next_action, blocker, last_touched_at
                FROM tasks
                WHERE source_channel = ? AND source_chat_id = ? AND status IN (?, ?, ?, ?)
                ORDER BY
                  CASE bucket
                    WHEN 'ATHENA' THEN 0
                    WHEN 'FLEIRE' THEN 1
                    WHEN 'BLOCKED' THEN 2
                    ELSE 3
                  END,
                  CASE status
                    WHEN 'in_progress' THEN 0
                    WHEN 'queued' THEN 1
                    WHEN 'blocked' THEN 2
                    ELSE 3
                  END,
                  priority DESC,
                  last_touched_at DESC
                LIMIT 12
                """,
                (channel, chat_id, *OPEN_TASK_STATUSES),
            ).fetchall()
        ]
        inbox_items = [
            dict(row)
            for row in conn.execute(
                """
                SELECT
                  id,
                  raw_text,
                  classification,
                  status,
                  note,
                  linked_entity_kind,
                  linked_entity_id,
                  updated_at
                FROM captured_items
                WHERE source_channel = ? AND source_chat_id = ? AND status IN ('new', 'triaged')
                ORDER BY updated_at DESC
                LIMIT 12
                """,
                (channel, chat_id),
            ).fetchall()
        ]
        outbox_items = [
            dict(row)
            for row in conn.execute(
                f"""
                SELECT id, subject, status, to_recipients, task_id, project_id, updated_at, draft_id, external_url
                FROM outbox_items
                WHERE status IN ({", ".join("?" for _ in OPEN_OUTBOX_STATUSES)})
                ORDER BY updated_at DESC
                LIMIT 12
                """,
                OPEN_OUTBOX_STATUSES,
            ).fetchall()
        ]
        recent_events = [
            dict(row)
            for row in conn.execute(
                """
                SELECT e.id, e.task_id, t.title, e.event_type, e.from_status, e.to_status, e.note, e.actor, e.created_at
                FROM task_events e
                JOIN tasks t ON t.id = e.task_id
                WHERE t.source_channel = ? AND t.source_chat_id = ?
                ORDER BY e.id DESC
                LIMIT 8
                """,
                (channel, chat_id),
            ).fetchall()
        ]
        weekly_brief = _latest_weekly_brief(conn)
        life_focus = _life_focus(conn)
        portfolio_focus = _portfolio_focus(conn)
        calendar_agenda = _calendar_agenda(conn)
        recent_context = _recent_context_docs(conn)
        status_counts = _status_counts(conn)
        founder_summary = _founder_summary(
            weekly_brief=weekly_brief,
            life_focus=life_focus,
            portfolio_focus=portfolio_focus,
            counts=status_counts,
        )
    return {
        "channel": channel,
        "chat_id": chat_id,
        "founder_context": {
            "summary": founder_summary,
            "status_counts": status_counts,
            "weekly_brief": weekly_brief,
            "life_focus": life_focus,
            "portfolio_focus": portfolio_focus,
            "calendar_agenda": calendar_agenda,
            "recent_context": recent_context,
        },
        "current": current,
        "open_tasks": open_tasks,
        "inbox_items": inbox_items,
        "outbox_items": outbox_items,
        "recent_events": recent_events,
        "db": str(db_path),
    }


def main() -> int:
    args = parse_args()
    db_path = Path(args.db).expanduser().resolve() if args.db else default_paths().db_path
    try:
        if args.command == "apply":
            result = apply_updates(
                db_path=db_path,
                json_path=Path(args.json_path).expanduser().resolve(),
                skip_render=bool(args.skip_render),
            )
        elif args.command == "current":
            result = current_state(
                db_path=db_path,
                channel=args.channel,
                chat_id=args.chat_id,
            )
        elif args.command == "capture":
            result = capture_item(
                db_path=db_path,
                raw_text=args.raw_text,
                source_channel=args.source_channel,
                source_chat_id=args.source_chat_id,
                source_message_ref=args.source_message_ref,
                classification=args.classification,
                linked_entity_kind=args.linked_entity_kind,
                linked_entity_id=args.linked_entity_id,
                status=args.status,
                note=args.note,
                dedupe_key=args.dedupe_key,
                capture_id=args.capture_id,
            )
        elif args.command == "triage-capture":
            result = triage_capture(
                db_path=db_path,
                capture_id=args.capture_id,
                classification=args.classification,
                linked_entity_kind=args.linked_entity_kind,
                linked_entity_id=args.linked_entity_id,
                status=args.status,
                note=args.note,
            )
        elif args.command == "create-task":
            result = create_task(
                db_path=db_path,
                title=args.title,
                owner=args.owner,
                bucket=args.bucket,
                status=args.status,
                task_id=args.task_id,
                project_id=args.project_id,
                portfolio_id=args.portfolio_id,
                life_area_id=args.life_area_id,
                life_goal_id=args.life_goal_id,
                workstream_id=args.workstream_id,
                source_text=args.source_text,
                why_now=args.why_now,
                next_action=args.next_action,
                blocker=args.blocker,
                notes=args.notes,
                requires_approval=bool(args.requires_approval),
                requires_browser=bool(args.requires_browser),
                required_for_project_completion=not bool(args.not_required_for_project_completion),
                source_channel=args.source_channel,
                source_chat_id=args.source_chat_id,
                source_message_ref=args.source_message_ref,
                dedupe_key=args.dedupe_key,
                capture_id=args.capture_id,
                priority=args.priority,
                actor=args.actor,
            )
        elif args.command == "start-task":
            result = start_task(
                db_path=db_path,
                task_id=args.task_id,
                next_action=args.next_action,
                note=args.note,
                actor=args.actor,
            )
        elif args.command == "block-task":
            result = block_task(
                db_path=db_path,
                task_id=args.task_id,
                blocker=args.blocker,
                next_action=args.next_action,
                note=args.note,
                requires_approval=True if args.requires_approval else None,
                requires_browser=True if args.requires_browser else None,
                actor=args.actor,
            )
        elif args.command == "complete-task":
            result = complete_task(
                db_path=db_path,
                task_id=args.task_id,
                summary=args.summary,
                resolution=args.resolution,
                evidence=args.evidence,
                verified_by=args.verified_by,
                note=args.note,
                actor=args.actor,
            )
        elif args.command == "reopen-task":
            result = reopen_task(
                db_path=db_path,
                task_id=args.task_id,
                reason=args.reason,
                status=args.status,
                bucket=args.bucket,
                next_action=args.next_action,
                actor=args.actor,
            )
        elif args.command == "project-status":
            result = update_project_status(
                db_path=db_path,
                project_id=args.project_id,
                status=args.status,
                health=args.health,
                blocker=args.blocker,
                current_goal=args.current_goal,
                next_milestone=args.next_milestone,
                summary=args.summary,
                completion_summary=args.completion_summary,
                completion_resolution=args.completion_resolution,
                wins=args.wins,
                risks=args.risks,
                next_7_days=args.next_7_days,
                actor=args.actor,
            )
        elif args.command == "review-goal":
            result = review_life_goal(
                db_path=db_path,
                goal_id=args.goal_id,
                status=args.status,
                current_focus=args.current_focus,
                status_note=args.status_note,
                risk_if_ignored=args.risk_if_ignored,
                supporting_rule=args.supporting_rule,
                next_review_at=args.next_review_at,
                actor=args.actor,
            )
        elif args.command == "review-cycle":
            result = run_review_cycle(
                cadence=args.cadence,
                db_path=db_path,
                actor=args.actor,
            )
        elif args.command == "refresh-rollups":
            result = refresh_rollups(db_path=db_path)
        elif args.command == "set-chat-focus":
            result = set_chat_focus(
                db_path=db_path,
                channel=args.channel,
                chat_id=args.chat_id,
                current_capture_id=args.current_capture_id,
                active_life_area_id=args.active_life_area_id,
                active_life_goal_id=args.active_life_goal_id,
                current_portfolio_id=args.current_portfolio_id,
                current_project_id=args.current_project_id,
                current_task_id=args.current_task_id,
                last_user_intent=args.last_user_intent,
                last_progress=args.last_progress,
                pending_approval_task_id=args.pending_approval_task_id,
            )
        elif args.command == "queue-email":
            result = create_email_outbox(
                db_path=db_path,
                to_recipients=args.to_recipients,
                cc_recipients=args.cc_recipients,
                bcc_recipients=args.bcc_recipients,
                subject=args.subject,
                body_text=args.body,
                task_id=args.task_id,
                project_id=args.project_id,
                account_label=args.account_label,
                actor=args.actor,
            )
        elif args.command == "approve-outbox":
            result = approve_outbox_items(
                db_path=db_path,
                outbox_ids=args.item_ids,
                note=args.note,
                actor=args.actor,
            )
        elif args.command == "reject-outbox":
            result = reject_outbox_items(
                db_path=db_path,
                outbox_ids=args.item_ids,
                note=args.note,
                actor=args.actor,
            )
        elif args.command == "send-outbox":
            result = send_outbox_items(
                db_path=db_path,
                outbox_ids=args.item_ids or None,
                actor=args.actor,
            )
        elif args.command == "gmail-search":
            result = search_gmail_messages(
                query=args.query,
                account_label=args.account,
                max_results=args.max_results,
            )
        else:
            raise ValueError(f"Unsupported command: {args.command}")
    except Exception as exc:  # pragma: no cover
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 1

    print(json.dumps(result, indent=2, sort_keys=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
