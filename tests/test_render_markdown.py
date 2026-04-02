from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from athena.db import connect_db, ensure_db
from athena.render_markdown import render


class RenderMarkdownTests(unittest.TestCase):
    def test_render_outputs_bucket_and_ledger_views(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "tasks.sqlite"
            task_dir = tmp_path / "task-system"
            ledger_path = tmp_path / "telegram-ledger.md"
            local_ledger_path = task_dir / "TELEGRAM_LEDGER.md"

            ensure_db(db_path=db_path)
            with connect_db(db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO tasks (
                      id, title, owner, bucket, status, priority, source_channel, source_chat_id,
                      next_action, created_at, updated_at, last_touched_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "task_one",
                        "Verify Athena board",
                        "ATHENA",
                        "ATHENA",
                        "in_progress",
                        80,
                        "telegram",
                        "1937792843",
                        "Render the markdown views.",
                        100,
                        100,
                        100,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO chat_state (
                      channel, chat_id, current_task_id, last_user_intent, last_progress, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "telegram",
                        "1937792843",
                        "task_one",
                        "Build the Athena board",
                        "Rendering compatibility views.",
                        100,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO task_events (
                      task_id, event_type, from_status, to_status, note, actor, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "task_one",
                        "status_changed",
                        "queued",
                        "in_progress",
                        "Started building the board.",
                        "athena",
                        100,
                    ),
                )
                conn.commit()

            render(
                db_path=db_path,
                task_dir=task_dir,
                ledger_path=ledger_path,
                local_ledger_path=local_ledger_path,
            )

            athena_view = (task_dir / "ATHENA.md").read_text(encoding="utf-8")
            ledger_view = ledger_path.read_text(encoding="utf-8")
            local_ledger_view = local_ledger_path.read_text(encoding="utf-8")

            self.assertIn("Verify Athena board", athena_view)
            self.assertIn("Render the markdown views.", athena_view)
            self.assertIn("Build the Athena board", ledger_view)
            self.assertIn("task_one", ledger_view)
            self.assertEqual(local_ledger_view, ledger_view)


if __name__ == "__main__":
    unittest.main()
