from __future__ import annotations

import argparse
from pathlib import Path

from .config import default_paths
from .db import connect_db


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render Telegram markdown views from tasks.sqlite.")
    parser.add_argument("--db", default=None, help="Override tasks.sqlite path.")
    parser.add_argument("--task-dir", default=None, help="Override task-system output directory.")
    parser.add_argument("--ledger", default=None, help="Override system ledger path.")
    parser.add_argument("--local-ledger", default=None, help="Override workspace-local ledger path.")
    return parser.parse_args()


def _stamp() -> str:
    from datetime import datetime, timezone, timedelta

    pht = timezone(timedelta(hours=8))
    return datetime.now(pht).strftime("%Y-%m-%d %H:%M:%S PHT")


def _format_touched(ts: int | None) -> str:
    if not ts:
        return ""
    from datetime import datetime, timezone, timedelta

    pht = timezone(timedelta(hours=8))
    return datetime.fromtimestamp(int(ts), tz=pht).strftime("%Y-%m-%d %H:%M PHT")


def _render_bucket(conn, title: str, intro: str, sql: str, output_path: Path, stamp: str, empty_text: str) -> None:
    rows = conn.execute(sql).fetchall()
    lines = [
        f"# {title}",
        "",
        intro,
        "",
        f"_Generated from tasks.sqlite on {stamp}._",
        "",
    ]
    if not rows:
        lines.append(empty_text)
        output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return

    for row in rows:
        checkbox = "[x]" if row["status"] in ("done", "cancelled") else "[ ]"
        lines.append(f"- {checkbox} {row['title']}")
        lines.append(f"  - owner: {row['owner']}")
        if row["source_text"]:
            lines.append(f"  - source: {row['source_text']}")
        if row["next_action"]:
            lines.append(f"  - next_action: {row['next_action']}")
        if row["why_now"]:
            lines.append(f"  - why_now: {row['why_now']}")
        if row["blocker"]:
            lines.append(f"  - blocked_by: {row['blocker']}")
        if row["notes"]:
            lines.append(f"  - notes: {row['notes']}")
        lines.append(f"  - status: {row['status']}")
        touched = _format_touched(row["last_touched_at"])
        if touched:
            lines.append(f"  - last_touched: {touched}")
        lines.append("")

    output_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def render(db_path: Path | None = None, task_dir: Path | None = None, ledger_path: Path | None = None, local_ledger_path: Path | None = None) -> None:
    paths = default_paths()
    resolved_db = (db_path or paths.db_path).expanduser().resolve()
    resolved_task_dir = (task_dir or paths.task_view_dir).expanduser().resolve()
    resolved_ledger = (ledger_path or paths.ledger_path).expanduser().resolve()
    resolved_local_ledger = (local_ledger_path or paths.local_ledger_path).expanduser().resolve()
    resolved_task_dir.mkdir(parents=True, exist_ok=True)
    resolved_ledger.parent.mkdir(parents=True, exist_ok=True)

    stamp = _stamp()
    with connect_db(resolved_db) as conn:
        base_fields = """
            SELECT
              title,
              owner,
              COALESCE(source_text, '') AS source_text,
              COALESCE(next_action, '') AS next_action,
              COALESCE(why_now, '') AS why_now,
              COALESCE(blocker, '') AS blocker,
              COALESCE(notes, '') AS notes,
              status,
              last_touched_at
            FROM tasks
        """
        _render_bucket(
            conn,
            "ATHENA",
            "Active execution queue. Athena should work these without prompting Fleire unless blocked.",
            base_fields
            + """
            WHERE bucket = 'ATHENA' AND status IN ('in_progress', 'queued')
            ORDER BY CASE status WHEN 'in_progress' THEN 0 ELSE 1 END, priority DESC, last_touched_at DESC
            """,
            resolved_task_dir / "ATHENA.md",
            stamp,
            "_No current Athena-owned execution tasks._",
        )
        _render_bucket(
            conn,
            "FLEIRE",
            "Only important, clear, truly Fleire-only tasks belong here.",
            base_fields
            + """
            WHERE bucket = 'FLEIRE' AND status IN ('in_progress', 'queued')
            ORDER BY CASE status WHEN 'in_progress' THEN 0 ELSE 1 END, priority DESC, last_touched_at DESC
            """,
            resolved_task_dir / "FLEIRE.md",
            stamp,
            "_No current Fleire-only tasks._",
        )
        _render_bucket(
            conn,
            "BLOCKED",
            "Only items Athena cannot continue without Fleire approval, decision, access, or missing information.",
            base_fields
            + """
            WHERE bucket = 'BLOCKED' AND status = 'blocked'
            ORDER BY priority DESC, last_touched_at DESC
            """,
            resolved_task_dir / "BLOCKED.md",
            stamp,
            "_No current blocked items._",
        )
        _render_bucket(
            conn,
            "SOMEDAY",
            "Ideas, future possibilities, and low-priority items that should stay off Fleire's active plate.",
            base_fields
            + """
            WHERE bucket = 'SOMEDAY' AND status IN ('someday', 'cancelled')
            ORDER BY priority DESC, last_touched_at DESC
            """,
            resolved_task_dir / "SOMEDAY.md",
            stamp,
            "_No current someday items._",
        )

        current = conn.execute(
            """
            SELECT
              COALESCE(c.current_task_id, '') AS current_task_id,
              COALESCE(t.title, '') AS current_task_title,
              COALESCE(p.name, '') AS project_name,
              COALESCE(pf.name, '') AS portfolio_name,
              COALESCE(c.last_user_intent, '') AS last_user_intent,
              COALESCE(c.last_progress, '') AS last_progress,
              COALESCE(pt.title, '') AS pending_approval,
              COALESCE(t.next_action, '') AS next_action,
              COALESCE(t.status, 'idle') AS status
            FROM chat_state c
            LEFT JOIN tasks t ON t.id = c.current_task_id
            LEFT JOIN projects p ON p.id = c.current_project_id
            LEFT JOIN portfolios pf ON pf.id = c.current_portfolio_id
            LEFT JOIN tasks pt ON pt.id = c.pending_approval_task_id
            WHERE c.channel = 'telegram' AND c.chat_id = '1937792843'
            LIMIT 1
            """
        ).fetchone()
        events = conn.execute(
            """
            SELECT
              t.title,
              e.event_type,
              e.to_status,
              e.note,
              e.created_at
            FROM task_events e
            JOIN tasks t ON t.id = e.task_id
            WHERE t.source_channel = 'telegram' AND t.source_chat_id = '1937792843'
            ORDER BY e.id DESC
            LIMIT 8
            """
        ).fetchall()

    lines = [
        "# Telegram Task Ledger (1937792843)",
        "",
        f"Generated from tasks.sqlite on {stamp}.",
        "",
        "## Current",
    ]
    if current:
        lines.extend(
            [
                f"- task_id: {current['current_task_id']}",
                f"- task_summary: {current['current_task_title']}",
                f"- project: {current['project_name']}",
                f"- portfolio: {current['portfolio_name']}",
                f"- status: {current['status']}",
                f"- last_user_intent: {current['last_user_intent']}",
                f"- last_progress: {current['last_progress']}",
                f"- pending_approval: {current['pending_approval']}",
                f"- next_action: {current['next_action']}",
            ]
        )
    else:
        lines.append("- No current Telegram chat state.")
    lines.extend(["", "## History"])
    if not events:
        lines.append("- No history yet.")
    else:
        for event in events:
            lines.append(
                f"- {_format_touched(event['created_at'])} | {event['title']} | {event['event_type']} | {event['to_status']}"
            )
            if event["note"]:
                lines.append(f"  note: {event['note']}")

    content = "\n".join(lines).rstrip() + "\n"
    resolved_ledger.write_text(content, encoding="utf-8")
    resolved_local_ledger.write_text(content, encoding="utf-8")


def main() -> int:
    args = parse_args()
    render(
        db_path=Path(args.db).expanduser().resolve() if args.db else None,
        task_dir=Path(args.task_dir).expanduser().resolve() if args.task_dir else None,
        ledger_path=Path(args.ledger).expanduser().resolve() if args.ledger else None,
        local_ledger_path=Path(args.local_ledger).expanduser().resolve() if args.local_ledger else None,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
