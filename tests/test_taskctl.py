from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from athena.db import connect_db, ensure_db
from athena.taskctl import apply_updates, current_state, main


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

    def test_cli_current_command_returns_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "tasks.sqlite"
            payload_path = tmp_path / "update.json"
            payload = {
                "upserts": [
                    {
                        "id": "task_cli",
                        "title": "CLI task",
                        "owner": "ATHENA",
                        "bucket": "ATHENA",
                        "status": "queued",
                        "source_channel": "telegram",
                        "source_chat_id": "1937792843",
                    }
                ],
                "chat_state": {
                    "channel": "telegram",
                    "chat_id": "1937792843",
                    "current_task_id": "task_cli",
                },
                "render": False,
            }
            payload_path.write_text(json.dumps(payload), encoding="utf-8")
            apply_updates(db_path=db_path, json_path=payload_path, skip_render=True)

            stdout = io.StringIO()
            with patch(
                "sys.argv",
                [
                    "taskctl",
                    "current",
                    "--db",
                    str(db_path),
                    "--channel",
                    "telegram",
                    "--chat-id",
                    "1937792843",
                ],
            ), contextlib.redirect_stdout(stdout):
                exit_code = main()

            self.assertEqual(exit_code, 0)
            data = json.loads(stdout.getvalue())
            self.assertEqual(data["current"]["current_task_id"], "task_cli")
            self.assertEqual(data["open_tasks"][0]["id"], "task_cli")

    def test_cli_apply_command_invalid_json_returns_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "tasks.sqlite"
            payload_path = tmp_path / "update.json"
            payload_path.write_text("{not-json}", encoding="utf-8")

            stdout = io.StringIO()
            stderr = io.StringIO()
            with patch(
                "sys.argv",
                ["taskctl", "apply", "--db", str(db_path), str(payload_path)],
            ), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                exit_code = main()

            self.assertEqual(exit_code, 1)
            err = json.loads(stderr.getvalue().strip())
            self.assertFalse(err["ok"])
            self.assertIn("Expecting property name enclosed in double quotes", err["error"])

    def test_current_state_includes_founder_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "tasks.sqlite"
            brief_path = tmp_path / "weekly-ceo-brief.md"
            agenda_path = tmp_path / "calendar-agenda.md"
            brief_path.write_text("# Brief\n", encoding="utf-8")
            agenda_path.write_text("- Founder review\n- Client call\n", encoding="utf-8")

            ensure_db(db_path)
            with connect_db(db_path) as conn:
                ts = 1_712_000_000
                conn.execute(
                    """
                    INSERT INTO life_areas (id, slug, name, status, priority, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    ("life_health", "life-health", "Health", "active", 100, ts, ts),
                )
                conn.execute(
                    """
                    INSERT INTO life_goals (
                      id, life_area_id, slug, title, horizon, status, current_focus, supporting_rule,
                      risk_if_ignored, derived_summary, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "goal_energy",
                        "life_health",
                        "goal-energy",
                        "Protect founder energy",
                        "quarter",
                        "active",
                        "Keep recovery real this week",
                        "Do not trade sleep for fake urgency",
                        "Decision quality drops fast",
                        "Energy is the real bottleneck",
                        ts,
                        ts,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO portfolios (id, slug, name, status, priority, review_cadence, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    ("portfolio_dasho", "dashocontent", "DashoContent", "active", 100, "weekly", ts, ts),
                )
                conn.execute(
                    """
                    INSERT INTO projects (
                      id, life_area_id, life_goal_id, portfolio_id, slug, name, kind, tier, status, health,
                      current_goal, next_milestone, blocker, rollup_summary, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "project_brand",
                        "life_health",
                        "goal_energy",
                        "portfolio_dasho",
                        "brand-compliance",
                        "Brand compliance scoring MVP",
                        "product",
                        "core",
                        "active",
                        "yellow",
                        "Ship the smallest scoring workflow",
                        "Finish the scoring pass and validate it with a real account",
                        "",
                        "Core product focus this week",
                        ts,
                        ts,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO source_documents (
                      id, kind, title, path, source_system, is_authoritative, last_synced_at, summary, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "brief_1",
                        "weekly_ceo_brief",
                        "Athena CEO Weekly Brief - Week of 2026-03-30",
                        str(brief_path),
                        "athena",
                        0,
                        ts,
                        "Life focus: Protect founder energy. | Project focus: DashoContent / Brand compliance scoring MVP.",
                        ts,
                        ts,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO source_documents (
                      id, kind, title, path, source_system, is_authoritative, last_synced_at, summary, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "agenda_1",
                        "calendar_agenda",
                        "Upcoming Calendar Agenda",
                        str(agenda_path),
                        "gcal",
                        0,
                        ts,
                        "Founder review and client call this week.",
                        ts,
                        ts,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO source_documents (
                      id, kind, title, path, source_system, is_authoritative, last_synced_at, summary, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "notebook_1",
                        "notebooklm",
                        "Fleire Life Context",
                        str(tmp_path / "life-context.md"),
                        "notebooklm",
                        0,
                        ts,
                        "North star and current season context mirrored locally.",
                        ts,
                        ts,
                    ),
                )
                conn.commit()

            snapshot = current_state(db_path=db_path, channel="telegram", chat_id="1937792843")

            founder = snapshot["founder_context"]
            self.assertEqual(
                founder["summary"],
                "Life focus: Protect founder energy. | Project focus: DashoContent / Brand compliance scoring MVP.",
            )
            self.assertEqual(founder["weekly_brief"]["title"], "Athena CEO Weekly Brief - Week of 2026-03-30")
            self.assertEqual(founder["life_focus"][0]["title"], "Protect founder energy")
            self.assertEqual(founder["portfolio_focus"][0]["name"], "Brand compliance scoring MVP")
            self.assertEqual(founder["calendar_agenda"]["lines"][0], "- Founder review")
            self.assertEqual(founder["recent_context"][0]["kind"], "notebooklm")

    def test_cli_queue_email_and_outbox_actions_route_through_module(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "tasks.sqlite"

            stdout = io.StringIO()
            with patch(
                "athena.taskctl.create_email_outbox",
                return_value={"id": "outbox-1", "status": "needs_approval"},
            ) as create_mock, patch(
                "sys.argv",
                [
                    "taskctl",
                    "queue-email",
                    "--db",
                    str(db_path),
                    "--account",
                    "athena",
                    "--to",
                    "person@example.com",
                    "--subject",
                    "Follow-up",
                    "--body",
                    "Draft body",
                ],
            ), contextlib.redirect_stdout(stdout):
                exit_code = main()

            self.assertEqual(exit_code, 0)
            create_mock.assert_called_once()
            self.assertEqual(create_mock.call_args.kwargs["account_label"], "athena")
            data = json.loads(stdout.getvalue())
            self.assertEqual(data["id"], "outbox-1")

            stdout = io.StringIO()
            with patch(
                "athena.taskctl.approve_outbox_items",
                return_value={"updated_count": 2, "items": []},
            ) as approve_mock, patch(
                "sys.argv",
                ["taskctl", "approve-outbox", "--db", str(db_path), "outbox-1", "outbox-2"],
            ), contextlib.redirect_stdout(stdout):
                exit_code = main()

            self.assertEqual(exit_code, 0)
            approve_mock.assert_called_once()
            approve_data = json.loads(stdout.getvalue())
            self.assertEqual(approve_data["updated_count"], 2)


if __name__ == "__main__":
    unittest.main()
