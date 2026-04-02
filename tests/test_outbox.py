from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from athena.config import default_paths
from athena.db import connect_db, ensure_db
from athena.outbox import approve_outbox_items, create_email_outbox, reject_outbox_items, send_outbox_items


def _test_paths(tmp_dir: Path):
    paths = default_paths()
    overrides = {
        "db_path": tmp_dir / "tasks.sqlite",
        "workspace_root": tmp_dir / "workspace",
        "workspace_telegram_root": tmp_dir / "workspace-telegram",
        "life_dir": tmp_dir / "life",
        "notebooklm_export_dir": tmp_dir / "life" / "notebooklm-exports",
        "google_dir": tmp_dir / "workspace" / "system" / "google",
        "google_settings_path": tmp_dir / "workspace" / "system" / "google" / "settings.json",
        "google_client_secrets_path": tmp_dir / "workspace" / "system" / "google" / "client_secret.json",
        "google_token_path": tmp_dir / "workspace" / "system" / "google" / "token.json",
        "google_mirror_dir": tmp_dir / "workspace" / "system" / "google-mirror",
        "task_view_dir": tmp_dir / "workspace-telegram" / "task-system",
        "ledger_path": tmp_dir / "workspace" / "system" / "task-ledger" / "telegram-1937792843.md",
        "local_ledger_path": tmp_dir / "workspace-telegram" / "task-system" / "TELEGRAM_LEDGER.md",
    }
    return replace(paths, **overrides)


class FakeTransport:
    def __init__(self):
        self.json_routes: list[tuple[str, object]] = []

    def add_json(self, contains: str, payload: object) -> None:
        self.json_routes.append((contains, payload))

    def request_json(self, method: str, url: str, *, headers=None, data=None):
        for contains, payload in self.json_routes:
            if contains in url:
                if isinstance(payload, Exception):
                    raise payload
                return payload
        raise AssertionError(f"Unexpected JSON request: {method} {url}")

    def request_bytes(self, method: str, url: str, *, headers=None, data=None) -> bytes:
        raise AssertionError(f"Unexpected bytes request: {method} {url}")


class OutboxTests(unittest.TestCase):
    def test_create_email_outbox_creates_gmail_draft_and_queue_row(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            paths = _test_paths(tmp)
            ensure_db(paths=paths)
            paths.google_dir.mkdir(parents=True, exist_ok=True)
            paths.google_token_path.write_text(
                json.dumps(
                    {
                        "access_token": "cached-access-token",
                        "refresh_token": "refresh-token",
                        "expiry": 4102444800,
                    }
                ),
                encoding="utf-8",
            )
            transport = FakeTransport()
            transport.add_json(
                "gmail.googleapis.com/gmail/v1/users/me/drafts",
                {
                    "id": "draft-1",
                    "message": {"id": "msg-1", "threadId": "thread-1"},
                },
            )

            item = create_email_outbox(
                db_path=paths.db_path,
                paths=paths,
                to_recipients="client@example.com",
                subject="Proposal follow-up",
                body_text="Draft body",
                actor="test",
                transport=transport,
            )

            self.assertEqual(item["status"], "needs_approval")
            self.assertEqual(item["draft_id"], "draft-1")
            with connect_db(paths.db_path) as conn:
                stored = conn.execute("SELECT * FROM outbox_items WHERE id = ?", (item["id"],)).fetchone()
                events = list(conn.execute("SELECT event_type FROM outbox_events WHERE outbox_id = ? ORDER BY id", (item["id"],)))
            self.assertIsNotNone(stored)
            self.assertEqual([row["event_type"] for row in events], ["outbox_created", "draft_ready"])

    def test_outbox_approve_reject_and_send_flow(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            paths = _test_paths(tmp)
            ensure_db(paths=paths)
            paths.google_dir.mkdir(parents=True, exist_ok=True)
            paths.google_token_path.write_text(
                json.dumps(
                    {
                        "access_token": "cached-access-token",
                        "refresh_token": "refresh-token",
                        "expiry": 4102444800,
                    }
                ),
                encoding="utf-8",
            )
            transport = FakeTransport()
            transport.add_json(
                "gmail.googleapis.com/gmail/v1/users/me/drafts",
                {
                    "id": "draft-1",
                    "message": {"id": "msg-1", "threadId": "thread-1"},
                },
            )
            transport.add_json(
                "gmail.googleapis.com/gmail/v1/users/me/drafts/send",
                {
                    "id": "sent-1",
                    "threadId": "thread-1",
                    "labelIds": ["SENT"],
                },
            )

            send_item = create_email_outbox(
                db_path=paths.db_path,
                paths=paths,
                to_recipients="client@example.com",
                subject="Ready to send",
                body_text="Draft body",
                actor="test",
                transport=transport,
            )
            reject_item = create_email_outbox(
                db_path=paths.db_path,
                paths=paths,
                to_recipients="client2@example.com",
                subject="Needs edits",
                body_text="Second draft body",
                actor="test",
                transport=transport,
            )
            approve = approve_outbox_items(
                db_path=paths.db_path,
                outbox_ids=[send_item["id"]],
                note="Looks good",
                actor="test",
            )
            self.assertEqual(approve["updated_count"], 1)
            self.assertEqual(approve["items"][0]["status"], "approved")

            send = send_outbox_items(
                db_path=paths.db_path,
                paths=paths,
                outbox_ids=[send_item["id"]],
                actor="test",
                transport=transport,
            )
            self.assertEqual(send["sent_count"], 1)
            self.assertFalse(send["failed"])

            reject = reject_outbox_items(
                db_path=paths.db_path,
                outbox_ids=[reject_item["id"]],
                note="Need a rewrite",
                actor="test",
            )
            self.assertEqual(reject["updated_count"], 1)
            self.assertEqual(reject["items"][0]["status"], "rejected")

            with connect_db(paths.db_path) as conn:
                sent_row = conn.execute("SELECT status, sent_at FROM outbox_items WHERE id = ?", (send_item["id"],)).fetchone()
                rejected_row = conn.execute("SELECT status, sent_at FROM outbox_items WHERE id = ?", (reject_item["id"],)).fetchone()
                sent_events = list(conn.execute("SELECT event_type FROM outbox_events WHERE outbox_id = ? ORDER BY id", (send_item["id"],)))
            self.assertIsNotNone(sent_row)
            self.assertIsNotNone(rejected_row)
            assert sent_row is not None
            assert rejected_row is not None
            self.assertEqual(sent_row["status"], "sent")
            self.assertIsNotNone(sent_row["sent_at"])
            self.assertEqual(rejected_row["status"], "rejected")
            self.assertIsNone(rejected_row["sent_at"])
            self.assertIn("outbox_sent", [row["event_type"] for row in sent_events])


if __name__ == "__main__":
    unittest.main()
