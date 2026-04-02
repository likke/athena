from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from athena.taskctl import apply_updates, current_state


class TaskCtlTests(unittest.TestCase):
    def test_apply_updates_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "tasks.sqlite"
            payload_path = tmp_path / "update.json"
            payload = {
                "upserts": [
                    {
                        "id": "task_test",
                        "title": "Test task",
                        "owner": "ATHENA",
                        "bucket": "ATHENA",
                        "status": "in_progress",
                        "priority": 50,
                        "source_channel": "telegram",
                        "source_chat_id": "1937792843",
                        "source_text": "User asked for a clean task flow.",
                        "next_action": "Keep building Athena.",
                    }
                ],
                "chat_state": {
                    "channel": "telegram",
                    "chat_id": "1937792843",
                    "current_task_id": "task_test",
                    "last_user_intent": "Build Athena",
                    "last_progress": "Task system is live.",
                },
                "render": False,
            }
            payload_path.write_text(json.dumps(payload), encoding="utf-8")

            result = apply_updates(db_path=db_path, json_path=payload_path, skip_render=True)

            self.assertTrue(result["ok"])
            self.assertEqual(result["tasks_upserted"], 1)
            self.assertEqual(result["events_written"], 1)
            self.assertTrue(result["chat_state_updated"])
            self.assertEqual(Path(result["db"]).resolve(), db_path.resolve())

            snapshot = current_state(db_path=db_path, channel="telegram", chat_id="1937792843")

            self.assertEqual(snapshot["current"]["current_task_id"], "task_test")
            self.assertEqual(snapshot["current"]["current_task_title"], "Test task")
            self.assertEqual(snapshot["open_tasks"][0]["title"], "Test task")
            self.assertEqual(snapshot["recent_events"][0]["task_id"], "task_test")


if __name__ == "__main__":
    unittest.main()
