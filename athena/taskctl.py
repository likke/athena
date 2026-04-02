from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .config import default_paths
from .db import connect_db, ensure_db, now_ts, row_to_dict
from .render_markdown import render

DEFAULT_CHANNEL = "telegram"
DEFAULT_CHAT_ID = "1937792843"
OPEN_TASK_STATUSES = ("queued", "in_progress", "blocked", "someday")
CLOSED_TASK_STATUSES = ("done", "cancelled")
TASK_COLUMNS = (
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
    "dedupe_key",
    "source_channel",
    "source_chat_id",
    "source_message_ref",
    "created_at",
    "updated_at",
    "last_touched_at",
    "closed_at",
)
CHAT_STATE_COLUMNS = (
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
                  c.current_task_id,
                  c.current_project_id,
                  c.current_portfolio_id,
                  c.pending_approval_task_id,
                  c.last_user_intent,
                  c.last_progress,
                  c.updated_at,
                  t.title AS current_task_title,
                  t.status AS current_task_status,
                  t.next_action AS current_task_next_action,
                  pt.title AS pending_approval_title,
                  p.name AS current_project_name,
                  pf.name AS current_portfolio_name
                FROM chat_state c
                LEFT JOIN tasks t ON t.id = c.current_task_id
                LEFT JOIN tasks pt ON pt.id = c.pending_approval_task_id
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
    return {
        "channel": channel,
        "chat_id": chat_id,
        "current": current,
        "open_tasks": open_tasks,
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
        else:
            raise ValueError(f"Unsupported command: {args.command}")
    except Exception as exc:  # pragma: no cover
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 1

    print(json.dumps(result, indent=2, sort_keys=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
