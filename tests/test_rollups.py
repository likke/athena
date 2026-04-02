from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from athena.db import connect_db, ensure_db, query_one
from athena.rollups import (
    apply_life_goal_rollups,
    apply_project_rollups,
    project_completion_ready,
)


class RollupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "tasks.sqlite"
        ensure_db(self.db_path)
        self._seed_rows()

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def _seed_rows(self) -> None:
        now = int(time.time())
        with connect_db(self.db_path) as conn:
            conn.execute(
                "INSERT INTO life_areas (id, slug, name, status, priority, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("area-1", "core", "Core", "active", 10, now, now),
            )
            conn.execute(
                """
                INSERT INTO life_goals (id, life_area_id, slug, title, horizon, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("goal-1", "area-1", "ship-athena", "Ship Athena", "quarter", "active", now, now),
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
                  status_source, health_source, created_at, updated_at
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
                    "manual",
                    "derived",
                    now,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO tasks (
                  id, title, owner, bucket, status, priority, project_id, portfolio_id, life_goal_id,
                  required_for_project_completion, created_at, updated_at, last_touched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "task-required-open",
                    "Required open task",
                    "ATHENA",
                    "ATHENA",
                    "queued",
                    10,
                    "project-1",
                    "portfolio-1",
                    "goal-1",
                    1,
                    now,
                    now,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO tasks (
                  id, title, owner, bucket, status, priority, project_id, portfolio_id, life_goal_id,
                  required_for_project_completion, created_at, updated_at, last_touched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "task-optional-done",
                    "Optional done task",
                    "ATHENA",
                    "ATHENA",
                    "done",
                    1,
                    "project-1",
                    "portfolio-1",
                    "goal-1",
                    0,
                    now,
                    now,
                    now,
                ),
            )
            conn.commit()

    def test_project_completion_ready_depends_on_required_open_tasks(self) -> None:
        with connect_db(self.db_path) as conn:
            self.assertFalse(project_completion_ready(conn, "project-1"))
            conn.execute("UPDATE tasks SET status = 'done' WHERE id = 'task-required-open'")
            conn.commit()
            self.assertTrue(project_completion_ready(conn, "project-1"))

    def test_apply_project_rollups_updates_derived_fields(self) -> None:
        with connect_db(self.db_path) as conn:
            updated_count = apply_project_rollups(conn, "project-1")
            conn.commit()
            self.assertEqual(updated_count, 1)
            project = query_one(conn, "SELECT * FROM projects WHERE id = 'project-1'")
        self.assertIsNotNone(project)
        assert project is not None
        self.assertEqual(project["derived_status"], "active")
        self.assertEqual(project["derived_health"], "green")
        self.assertTrue(project["rollup_summary"])

    def test_apply_life_goal_rollups_sets_derived_summary(self) -> None:
        with connect_db(self.db_path) as conn:
            updated_count = apply_life_goal_rollups(conn, "goal-1")
            conn.commit()
            self.assertEqual(updated_count, 1)
            goal = query_one(conn, "SELECT * FROM life_goals WHERE id = 'goal-1'")
        self.assertIsNotNone(goal)
        assert goal is not None
        self.assertEqual(goal["derived_status"], "active")
        self.assertIn("open tasks", goal["derived_summary"])


if __name__ == "__main__":
    unittest.main()
