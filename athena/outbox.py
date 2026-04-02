from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .config import AthenaPaths, default_paths
from .db import connect_db, ensure_db, now_ts, query_all, query_one, slugify
from .google import create_gmail_draft, send_gmail_draft

OUTBOX_OPEN_STATUSES = ("drafting", "needs_approval", "approved", "sending", "error")


class OutboxError(ValueError):
    pass


def _resolve_db(db_path: Path | None = None) -> Path:
    return (db_path or default_paths().db_path).expanduser().resolve()


def _make_outbox_id(conn: sqlite3.Connection, subject: str) -> str:
    base = f"outbox-{slugify(subject)}"
    candidate = base
    while query_one(conn, "SELECT id FROM outbox_items WHERE id = ?", (candidate,)) is not None:
        candidate = f"{base}-{now_ts()}"
    return candidate


def _normalize_recipients(value: str | Iterable[str] | None) -> str:
    if value is None:
        return ""
    raw_items = [value] if isinstance(value, str) else list(value)
    cleaned: list[str] = []
    for item in raw_items:
        for part in re.split(r"[,\n;]+", str(item)):
            recipient = part.strip()
            if recipient and recipient not in cleaned:
                cleaned.append(recipient)
    return ", ".join(cleaned)


def _ensure_outbox_row(conn: sqlite3.Connection, outbox_id: str) -> dict[str, Any]:
    row = query_one(conn, "SELECT * FROM outbox_items WHERE id = ?", (outbox_id,))
    if row is None:
        raise OutboxError(f"Outbox item not found: {outbox_id}")
    return row


def _insert_outbox_event(
    conn: sqlite3.Connection,
    *,
    outbox_id: str,
    event_type: str,
    from_status: str | None,
    to_status: str | None,
    note: str | None,
    actor: str,
) -> None:
    conn.execute(
        """
        INSERT INTO outbox_events (outbox_id, event_type, from_status, to_status, note, actor, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (outbox_id, event_type, from_status, to_status, note, actor, now_ts()),
    )


def list_outbox_items(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return query_all(
        conn,
        """
        SELECT
          o.id,
          o.task_id,
          o.project_id,
          o.provider,
          o.account_label,
          o.to_recipients,
          o.cc_recipients,
          o.bcc_recipients,
          o.subject,
          o.body_text,
          o.status,
          o.draft_id,
          o.external_ref,
          o.external_url,
          o.approval_note,
          o.error_message,
          o.sent_at,
          o.created_at,
          o.updated_at,
          COALESCE(t.title, '') AS task_title,
          COALESCE(p.name, '') AS project_name
        FROM outbox_items o
        LEFT JOIN tasks t ON t.id = o.task_id
        LEFT JOIN projects p ON p.id = o.project_id
        ORDER BY
          CASE o.status
            WHEN 'needs_approval' THEN 0
            WHEN 'approved' THEN 1
            WHEN 'drafting' THEN 2
            WHEN 'sending' THEN 3
            WHEN 'error' THEN 4
            WHEN 'sent' THEN 5
            ELSE 6
          END,
          o.updated_at DESC
        """,
    )


def create_email_outbox(
    *,
    to_recipients: str | Iterable[str],
    subject: str,
    body_text: str,
    db_path: Path | None = None,
    paths: AthenaPaths | None = None,
    task_id: str | None = None,
    project_id: str | None = None,
    cc_recipients: str | Iterable[str] | None = None,
    bcc_recipients: str | Iterable[str] | None = None,
    account_label: str = "primary",
    actor: str = "athena",
    transport: Any | None = None,
) -> dict[str, Any]:
    resolved_db = _resolve_db(db_path)
    resolved_paths = paths or default_paths()
    ensure_db(resolved_db)
    normalized_to = _normalize_recipients(to_recipients)
    normalized_cc = _normalize_recipients(cc_recipients)
    normalized_bcc = _normalize_recipients(bcc_recipients)
    clean_subject = subject.strip()
    clean_body = body_text.strip()
    if not normalized_to:
        raise OutboxError("Email draft requires at least one recipient")
    if not clean_subject:
        raise OutboxError("Email draft requires a subject")
    if not clean_body:
        raise OutboxError("Email draft requires a body")

    with connect_db(resolved_db) as conn:
        outbox_id = _make_outbox_id(conn, clean_subject)
        now = now_ts()
        conn.execute(
            """
            INSERT INTO outbox_items (
              id, task_id, project_id, provider, account_label, to_recipients, cc_recipients, bcc_recipients,
              subject, body_text, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                outbox_id,
                task_id,
                project_id,
                "gmail",
                account_label,
                normalized_to,
                normalized_cc or None,
                normalized_bcc or None,
                clean_subject,
                clean_body,
                "drafting",
                now,
                now,
            ),
        )
        _insert_outbox_event(
            conn,
            outbox_id=outbox_id,
            event_type="outbox_created",
            from_status=None,
            to_status="drafting",
            note=f"Queued draft for {clean_subject}",
            actor=actor,
        )
        conn.commit()

    try:
        draft = create_gmail_draft(
            paths=resolved_paths,
            to_recipients=normalized_to,
            subject=clean_subject,
            body_text=clean_body,
            cc_recipients=normalized_cc or None,
            bcc_recipients=normalized_bcc or None,
            transport=transport,
        )
    except Exception as exc:
        with connect_db(resolved_db) as conn:
            existing = _ensure_outbox_row(conn, outbox_id)
            now = now_ts()
            conn.execute(
                """
                UPDATE outbox_items
                SET status = ?, error_message = ?, updated_at = ?
                WHERE id = ?
                """,
                ("error", str(exc), now, outbox_id),
            )
            _insert_outbox_event(
                conn,
                outbox_id=outbox_id,
                event_type="draft_failed",
                from_status=str(existing["status"]),
                to_status="error",
                note=str(exc),
                actor=actor,
            )
            conn.commit()
            return _ensure_outbox_row(conn, outbox_id)

    with connect_db(resolved_db) as conn:
        existing = _ensure_outbox_row(conn, outbox_id)
        now = now_ts()
        conn.execute(
            """
            UPDATE outbox_items
            SET status = ?, draft_id = ?, external_ref = ?, external_url = ?, error_message = NULL, updated_at = ?
            WHERE id = ?
            """,
            (
                "needs_approval",
                draft.get("draft_id"),
                draft.get("message_id"),
                draft.get("external_url"),
                now,
                outbox_id,
            ),
        )
        _insert_outbox_event(
            conn,
            outbox_id=outbox_id,
            event_type="draft_ready",
            from_status=str(existing["status"]),
            to_status="needs_approval",
            note="Gmail draft created and queued for approval.",
            actor=actor,
        )
        conn.commit()
        return _ensure_outbox_row(conn, outbox_id)


def _update_outbox_statuses(
    *,
    outbox_ids: Iterable[str],
    status: str,
    db_path: Path | None = None,
    actor: str = "athena",
    note: str | None = None,
    allowed_from_statuses: set[str] | None = None,
) -> dict[str, Any]:
    resolved_db = _resolve_db(db_path)
    ensure_db(resolved_db)
    cleaned_ids = [item.strip() for item in outbox_ids if item and item.strip()]
    if not cleaned_ids:
        raise OutboxError("No outbox ids were provided")

    updated: list[dict[str, Any]] = []
    skipped: list[str] = []
    with connect_db(resolved_db) as conn:
        for outbox_id in cleaned_ids:
            existing = _ensure_outbox_row(conn, outbox_id)
            if allowed_from_statuses and str(existing["status"]) not in allowed_from_statuses:
                skipped.append(outbox_id)
                continue
            now = now_ts()
            conn.execute(
                """
                UPDATE outbox_items
                SET status = ?, approval_note = ?, updated_at = ?, error_message = NULL
                WHERE id = ?
                """,
                (status, note, now, outbox_id),
            )
            _insert_outbox_event(
                conn,
                outbox_id=outbox_id,
                event_type="status_changed",
                from_status=str(existing["status"]),
                to_status=status,
                note=note,
                actor=actor,
            )
            updated.append(_ensure_outbox_row(conn, outbox_id))
        conn.commit()
    return {"updated_count": len(updated), "items": updated, "skipped": skipped}


def approve_outbox_items(
    *,
    outbox_ids: Iterable[str],
    db_path: Path | None = None,
    actor: str = "athena",
    note: str | None = None,
) -> dict[str, Any]:
    return _update_outbox_statuses(
        outbox_ids=outbox_ids,
        status="approved",
        db_path=db_path,
        actor=actor,
        note=note or "Approved for send.",
        allowed_from_statuses={"needs_approval", "error", "rejected"},
    )


def reject_outbox_items(
    *,
    outbox_ids: Iterable[str],
    db_path: Path | None = None,
    actor: str = "athena",
    note: str | None = None,
) -> dict[str, Any]:
    return _update_outbox_statuses(
        outbox_ids=outbox_ids,
        status="rejected",
        db_path=db_path,
        actor=actor,
        note=note or "Rejected before send.",
        allowed_from_statuses={"drafting", "needs_approval", "approved", "error"},
    )


def send_outbox_items(
    *,
    db_path: Path | None = None,
    paths: AthenaPaths | None = None,
    outbox_ids: Iterable[str] | None = None,
    actor: str = "athena",
    transport: Any | None = None,
) -> dict[str, Any]:
    resolved_db = _resolve_db(db_path)
    resolved_paths = paths or default_paths()
    ensure_db(resolved_db)
    selected_ids = [item.strip() for item in (outbox_ids or []) if item and item.strip()]
    with connect_db(resolved_db) as conn:
        if selected_ids:
            placeholders = ", ".join("?" for _ in selected_ids)
            rows = query_all(
                conn,
                f"SELECT * FROM outbox_items WHERE id IN ({placeholders}) ORDER BY updated_at DESC",
                tuple(selected_ids),
            )
        else:
            rows = query_all(
                conn,
                "SELECT * FROM outbox_items WHERE status = 'approved' ORDER BY updated_at DESC",
            )

    sent: list[dict[str, Any]] = []
    skipped: list[str] = []
    failed: list[dict[str, str]] = []
    for row in rows:
        outbox_id = str(row["id"])
        if str(row["status"]) != "approved":
            skipped.append(outbox_id)
            continue

        draft_id = str(row.get("draft_id") or "").strip()
        try:
            if not draft_id:
                draft = create_gmail_draft(
                    paths=resolved_paths,
                    to_recipients=str(row["to_recipients"]),
                    subject=str(row["subject"]),
                    body_text=str(row["body_text"]),
                    cc_recipients=str(row.get("cc_recipients") or ""),
                    bcc_recipients=str(row.get("bcc_recipients") or ""),
                    transport=transport,
                )
                draft_id = str(draft.get("draft_id") or "").strip()
                with connect_db(resolved_db) as conn:
                    now = now_ts()
                    conn.execute(
                        """
                        UPDATE outbox_items
                        SET draft_id = ?, external_ref = ?, external_url = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (draft_id or None, draft.get("message_id"), draft.get("external_url"), now, outbox_id),
                    )
                    _insert_outbox_event(
                        conn,
                        outbox_id=outbox_id,
                        event_type="draft_ready",
                        from_status="approved",
                        to_status="approved",
                        note="Created missing Gmail draft before send.",
                        actor=actor,
                    )
                    conn.commit()
            with connect_db(resolved_db) as conn:
                conn.execute(
                    "UPDATE outbox_items SET status = ?, updated_at = ?, error_message = NULL WHERE id = ?",
                    ("sending", now_ts(), outbox_id),
                )
                _insert_outbox_event(
                    conn,
                    outbox_id=outbox_id,
                    event_type="status_changed",
                    from_status="approved",
                    to_status="sending",
                    note="Sending approved draft.",
                    actor=actor,
                )
                conn.commit()

            result = send_gmail_draft(
                paths=resolved_paths,
                draft_id=draft_id,
                transport=transport,
            )
            with connect_db(resolved_db) as conn:
                now = now_ts()
                conn.execute(
                    """
                    UPDATE outbox_items
                    SET status = ?, external_ref = ?, external_url = ?, sent_at = ?, updated_at = ?, error_message = NULL
                    WHERE id = ?
                    """,
                    (
                        "sent",
                        result.get("message_id"),
                        result.get("external_url"),
                        now,
                        now,
                        outbox_id,
                    ),
                )
                _insert_outbox_event(
                    conn,
                    outbox_id=outbox_id,
                    event_type="outbox_sent",
                    from_status="sending",
                    to_status="sent",
                    note="Gmail draft sent.",
                    actor=actor,
                )
                conn.commit()
                sent.append(_ensure_outbox_row(conn, outbox_id))
        except Exception as exc:
            with connect_db(resolved_db) as conn:
                conn.execute(
                    "UPDATE outbox_items SET status = ?, error_message = ?, updated_at = ? WHERE id = ?",
                    ("error", str(exc), now_ts(), outbox_id),
                )
                _insert_outbox_event(
                    conn,
                    outbox_id=outbox_id,
                    event_type="outbox_error",
                    from_status="sending" if draft_id else "approved",
                    to_status="error",
                    note=str(exc),
                    actor=actor,
                )
                conn.commit()
            failed.append({"id": outbox_id, "error": str(exc)})

    return {
        "sent_count": len(sent),
        "sent": sent,
        "skipped": skipped,
        "failed": failed,
    }
