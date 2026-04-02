from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

from athena.db import connect_db, ensure_db, query_one
from athena.reviews import run_review_cycle


class ReviewCycleTests(unittest.TestCase):
    ENV_KEYS = [
        "ATHENA_DB_PATH",
        "ATHENA_WORKSPACE_ROOT",
        "ATHENA_WORKSPACE_TELEGRAM_ROOT",
        "ATHENA_TASK_VIEW_DIR",
        "ATHENA_BRIEFS_DIR",
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
        briefs_dir = workspace / "system" / "briefs"
        life_dir = workspace / "life"
        ledger = workspace / "system" / "task-ledger" / "telegram-1937792843.md"
        for path in [workspace, workspace_telegram, task_dir, briefs_dir, life_dir, ledger.parent]:
            path.mkdir(parents=True, exist_ok=True)

        self.db_path = ledger.parent / "tasks.sqlite"
        self._env_backup = {key: os.environ.get(key) for key in self.ENV_KEYS}
        env_values = {
            "ATHENA_DB_PATH": str(self.db_path),
            "ATHENA_WORKSPACE_ROOT": str(workspace),
            "ATHENA_WORKSPACE_TELEGRAM_ROOT": str(workspace_telegram),
            "ATHENA_TASK_VIEW_DIR": str(task_dir),
            "ATHENA_BRIEFS_DIR": str(briefs_dir),
            "ATHENA_LIFE_DIR": str(life_dir),
            "ATHENA_LEDGER_PATH": str(ledger),
            "ATHENA_LOCAL_LEDGER_PATH": str(task_dir / "TELEGRAM_LEDGER.md"),
        }
        for key, value in env_values.items():
            os.environ[key] = value

        ensure_db(self.db_path)
        self._seed_rows()

    def tearDown(self) -> None:
        self.tmpdir.cleanup()
        for key, value in self._env_backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _seed_rows(self) -> None:
        now = int(time.time())
        old_10_days = now - (10 * 24 * 60 * 60)
        old_35_days = now - (35 * 24 * 60 * 60)
        with connect_db(self.db_path) as conn:
            conn.execute(
                "INSERT INTO life_areas (id, slug, name, status, priority, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("area-1", "core", "Core", "active", 10, now, now),
            )
            conn.execute(
                """
                INSERT INTO life_goals (
                  id, life_area_id, slug, title, horizon, status, created_at, updated_at, last_reviewed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("goal-1", "area-1", "athena-goal", "Athena goal", "quarter", "active", now, now, old_10_days),
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
                  id, life_area_id, life_goal_id, portfolio_id, slug, name, kind, tier, status, health,
                  last_reviewed_at, last_real_progress_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "project-1",
                    "area-1",
                    "goal-1",
                    "portfolio-1",
                    "athena-os",
                    "Athena OS",
                    "internal",
                    "core",
                    "active",
                    "green",
                    old_10_days,
                    old_35_days,
                    now,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO project_repos (
                  project_id, repo_name, repo_path, role, is_primary, last_seen_commit, last_seen_branch, last_seen_dirty, last_scanned_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "project-1",
                    "athena",
                    "/tmp/athena",
                    "app",
                    1,
                    "abc123",
                    "main",
                    1,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO tasks (
                  id, title, owner, bucket, status, priority, project_id, portfolio_id, life_goal_id,
                  source_channel, source_chat_id, blocker, created_at, updated_at, last_touched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "task-blocked",
                    "Blocked task",
                    "ATHENA",
                    "BLOCKED",
                    "blocked",
                    10,
                    "project-1",
                    "portfolio-1",
                    "goal-1",
                    "telegram",
                    "1937792843",
                    "Awaiting approval",
                    now,
                    now,
                    old_10_days,
                ),
            )
            conn.execute(
                """
                INSERT INTO tasks (
                  id, title, owner, bucket, status, priority, project_id, portfolio_id, life_goal_id,
                  source_channel, source_chat_id, created_at, updated_at, last_touched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "task-stale-progress",
                    "Stale in progress task",
                    "ATHENA",
                    "ATHENA",
                    "in_progress",
                    5,
                    "project-1",
                    "portfolio-1",
                    "goal-1",
                    "telegram",
                    "1937792843",
                    now,
                    now,
                    old_10_days,
                ),
            )
            conn.commit()

    def test_daily_review_dedupes_created_items(self) -> None:
        first = run_review_cycle("daily", db_path=self.db_path, actor="test")
        second = run_review_cycle("daily", db_path=self.db_path, actor="test")

        self.assertGreaterEqual(first["findings_count"], 2)
        self.assertGreater(first["created_items_count"], 0)
        self.assertEqual(second["created_items_count"], 0)
        self.assertEqual(second["findings_count"], first["findings_count"])

        with connect_db(self.db_path) as conn:
            captures = query_one(conn, "SELECT COUNT(*) AS count FROM captured_items")
            runs = query_one(conn, "SELECT COUNT(*) AS count FROM review_runs WHERE cadence = 'daily'")
        self.assertIsNotNone(captures)
        self.assertIsNotNone(runs)
        assert captures is not None
        assert runs is not None
        self.assertGreaterEqual(int(captures["count"]), first["findings_count"])
        self.assertEqual(int(runs["count"]), 2)

    def test_weekly_and_monthly_reviews_record_runs(self) -> None:
        weekly = run_review_cycle("weekly", db_path=self.db_path, actor="test")
        monthly = run_review_cycle("monthly", db_path=self.db_path, actor="test")

        self.assertGreaterEqual(weekly["findings_count"], 1)
        self.assertGreaterEqual(monthly["findings_count"], 1)
        self.assertTrue(str(weekly.get("weekly_brief_path") or "").endswith(".md"))
        self.assertTrue(Path(str(weekly["weekly_brief_path"])).exists())
        with connect_db(self.db_path) as conn:
            weekly_runs = query_one(conn, "SELECT COUNT(*) AS count FROM review_runs WHERE cadence = 'weekly'")
            monthly_runs = query_one(conn, "SELECT COUNT(*) AS count FROM review_runs WHERE cadence = 'monthly'")
            brief_doc = query_one(conn, "SELECT id FROM source_documents WHERE kind = 'weekly_ceo_brief'")
        self.assertIsNotNone(weekly_runs)
        self.assertIsNotNone(monthly_runs)
        self.assertIsNotNone(brief_doc)
        assert weekly_runs is not None
        assert monthly_runs is not None
        self.assertEqual(int(weekly_runs["count"]), 1)
        self.assertEqual(int(monthly_runs["count"]), 1)


if __name__ == "__main__":
    unittest.main()
