from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

from athena.db import connect_db, ensure_db, query_one
from athena.state import (
    StateTransitionError,
    capture_item,
    complete_task,
    create_task,
    reopen_task,
    set_chat_focus,
    triage_capture,
    update_project_status,
)


class StateLayerTests(unittest.TestCase):
    ENV_KEYS = [
        "ATHENA_DB_PATH",
        "ATHENA_WORKSPACE_ROOT",
        "ATHENA_WORKSPACE_TELEGRAM_ROOT",
        "ATHENA_TASK_VIEW_DIR",
        "ATHENA_LIFE_DIR",
        "ATHENA_LEDGER_PATH",
        "ATHENA_LOCAL_LEDGER_PATH",
    ]

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        root = Path(self.tmpdir.name)
        workspace = root / "workspace"
        workspace_telegram = root / "workspace-telegram"
        task_dir = workspace_telegram / "task-system"
        life_dir = workspace / "life"
        ledger = workspace / "system" / "task-ledger" / "telegram-1937792843.md"
        for path in [workspace, workspace_telegram, task_dir, life_dir, ledger.parent]:
            path.mkdir(parents=True, exist_ok=True)

        self.db_path = ledger.parent / "tasks.sqlite"
        self._env_backup = {key: os.environ.get(key) for key in self.ENV_KEYS}
        env_values = {
            "ATHENA_DB_PATH": str(self.db_path),
            "ATHENA_WORKSPACE_ROOT": str(workspace),
            "ATHENA_WORKSPACE_TELEGRAM_ROOT": str(workspace_telegram),
            "ATHENA_TASK_VIEW_DIR": str(task_dir),
            "ATHENA_LIFE_DIR": str(life_dir),
            "ATHENA_LEDGER_PATH": str(ledger),
            "ATHENA_LOCAL_LEDGER_PATH": str(task_dir / "TELEGRAM_LEDGER.md"),
        }
        for key, value in env_values.items():
            os.environ[key] = value

        ensure_db(self.db_path)
        self._seed_core_rows()

    def tearDown(self) -> None:
        self.tmpdir.cleanup()
        for key, value in self._env_backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _seed_core_rows(self) -> None:
        now = int(time.time())
        with connect_db(self.db_path) as conn:
            conn.execute(
                "INSERT INTO life_areas (id, slug, name, status, priority, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("life-area-1", "core", "Core", "active", 10, now, now),
            )
            conn.execute(
                """
                INSERT INTO life_goals (
                  id, life_area_id, slug, title, horizon, status, success_definition, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "life-goal-1",
                    "life-area-1",
                    "ship-athena",
                    "Ship Athena",
                    "quarter",
                    "active",
                    "Athena stays consistent for project and life tasking.",
                    now,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO portfolios (id, slug, name, status, priority, review_cadence, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("portfolio-1", "dasho", "DashoContent", "active", 50, "weekly", now, now),
            )
            conn.execute(
                """
                INSERT INTO projects (
                  id, life_area_id, life_goal_id, portfolio_id, slug, name, kind, tier, status, health, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "project-1",
                    "life-area-1",
                    "life-goal-1",
                    "portfolio-1",
                    "athena-os",
                    "Athena OS",
                    "internal",
                    "core",
                    "active",
                    "green",
                    now,
                    now,
                ),
            )
            conn.commit()

    def test_capture_triage_create_task_flow_marks_capture_applied(self) -> None:
        captured = capture_item(
            raw_text="Create a durable inbox flow for Telegram tasks",
            db_path=self.db_path,
            source_channel="telegram",
            source_chat_id="1937792843",
            source_message_ref="msg-1",
            dedupe_key="capture:telegram:1937792843:msg-1",
            classification="note",
        )
        duplicate = capture_item(
            raw_text="Create a durable inbox flow for Telegram tasks",
            db_path=self.db_path,
            source_channel="telegram",
            source_chat_id="1937792843",
            source_message_ref="msg-1",
            dedupe_key="capture:telegram:1937792843:msg-1",
            classification="note",
        )
        self.assertEqual(captured["id"], duplicate["id"])

        triaged = triage_capture(
            capture_id=str(captured["id"]),
            classification="task",
            db_path=self.db_path,
            status="triaged",
        )
        self.assertEqual(triaged["status"], "triaged")
        self.assertEqual(triaged["classification"], "task")

        task = create_task(
            title="Wire DB-first task mutations",
            owner="ATHENA",
            db_path=self.db_path,
            project_id="project-1",
            portfolio_id="portfolio-1",
            life_area_id="life-area-1",
            life_goal_id="life-goal-1",
            source_channel="telegram",
            source_chat_id="1937792843",
            capture_id=str(captured["id"]),
            dedupe_key="task:db-first-mutations",
            actor="test",
        )
        self.assertEqual(task["status"], "queued")
        self.assertEqual(task["capture_id"], captured["id"])

        with connect_db(self.db_path) as conn:
            stored_capture = query_one(conn, "SELECT * FROM captured_items WHERE id = ?", (captured["id"],))
        self.assertIsNotNone(stored_capture)
        assert stored_capture is not None
        self.assertEqual(stored_capture["status"], "applied")
        self.assertEqual(stored_capture["linked_entity_kind"], "task")
        self.assertEqual(stored_capture["linked_entity_id"], task["id"])

    def test_complete_and_reopen_task_writes_completion_record(self) -> None:
        task = create_task(
            title="Close project truth gap",
            owner="ATHENA",
            db_path=self.db_path,
            project_id="project-1",
            portfolio_id="portfolio-1",
            source_channel="telegram",
            source_chat_id="1937792843",
            dedupe_key="task:close-project-truth-gap",
            actor="test",
        )

        completed = complete_task(
            task_id=str(task["id"]),
            db_path=self.db_path,
            summary="Added completion records and validated state transitions.",
            actor="test",
            evidence=["unit-tests:state", "manual-check:ledger"],
            verified_by="fleire",
        )
        self.assertEqual(completed["status"], "done")
        self.assertEqual(completed["resolution"], "done")
        self.assertIsNotNone(completed["completion_record_id"])
        self.assertTrue(completed["completion_summary"])

        with connect_db(self.db_path) as conn:
            completion = query_one(conn, "SELECT * FROM completion_records WHERE id = ?", (completed["completion_record_id"],))
        self.assertIsNotNone(completion)
        assert completion is not None
        self.assertEqual(completion["entity_kind"], "task")
        self.assertEqual(completion["entity_id"], task["id"])
        self.assertEqual(completion["resolution"], "done")
        self.assertIn("completion records", completion["summary"])

        reopened = reopen_task(
            task_id=str(task["id"]),
            db_path=self.db_path,
            reason="Need to patch edge cases in CLI output handling.",
            actor="test",
            status="queued",
        )
        self.assertEqual(reopened["status"], "queued")
        self.assertIsNone(reopened["completion_record_id"])
        self.assertIsNone(reopened["completion_summary"])
        self.assertEqual(reopened["reopen_reason"], "Need to patch edge cases in CLI output handling.")

    def test_task_completion_requires_evidence(self) -> None:
        task = create_task(
            title="Require proof for completion",
            owner="ATHENA",
            db_path=self.db_path,
            project_id="project-1",
            portfolio_id="portfolio-1",
            source_channel="telegram",
            source_chat_id="1937792843",
            dedupe_key="task:require-proof",
            actor="test",
        )

        with self.assertRaises(StateTransitionError):
            complete_task(
                task_id=str(task["id"]),
                db_path=self.db_path,
                summary="Marked done without evidence.",
                evidence=[],
                actor="test",
            )

    def test_project_completion_requires_summary(self) -> None:
        with self.assertRaises(StateTransitionError):
            update_project_status(
                project_id="project-1",
                db_path=self.db_path,
                status="done",
                completion_summary="",
                actor="test",
            )

    def test_chat_focus_tracks_current_capture(self) -> None:
        captured = capture_item(
            raw_text="Inbox item for chat focus test",
            db_path=self.db_path,
            source_channel="telegram",
            source_chat_id="1937792843",
            source_message_ref="focus-1",
            dedupe_key="capture:telegram:1937792843:focus-1",
            classification="note",
        )
        focus = set_chat_focus(
            db_path=self.db_path,
            channel="telegram",
            chat_id="1937792843",
            current_capture_id=str(captured["id"]),
            current_project_id="project-1",
            last_user_intent="Track focus in chat state",
            last_progress="Focus now points at capture item",
        )
        self.assertEqual(focus["current_capture_id"], captured["id"])
        self.assertEqual(focus["current_project_id"], "project-1")
        self.assertEqual(focus["last_user_intent"], "Track focus in chat state")


if __name__ == "__main__":
    unittest.main()
