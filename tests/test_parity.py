from __future__ import annotations

import contextlib
import importlib
import io
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.parse
import urllib.request
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from athena.config import default_paths
from athena.db import connect_db, ensure_db, query_one
from athena.state import complete_task
from athena.taskctl import main as taskctl_main


class SurfaceParityTests(unittest.TestCase):
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
        self._env_backup = {key: os.environ.get(key) for key in self.ENV_KEYS}

    def tearDown(self) -> None:
        self.tmpdir.cleanup()
        for key, value in self._env_backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _paths_for(self, name: str):
        root = Path(self.tmpdir.name) / name
        workspace = root / "workspace"
        workspace_telegram = root / "workspace-telegram"
        task_view = workspace_telegram / "task-system"
        life_dir = workspace / "life"
        ledger = workspace / "system" / "task-ledger" / "telegram-1937792843.md"
        for path in (workspace, workspace_telegram, task_view, life_dir, ledger.parent):
            path.mkdir(parents=True, exist_ok=True)
        paths = default_paths()
        paths = replace(
            paths,
            openclaw_root=root,
            workspace_root=workspace,
            workspace_telegram_root=workspace_telegram,
            task_view_dir=task_view,
            life_dir=life_dir,
            db_path=ledger.parent / "tasks.sqlite",
            ledger_path=ledger,
            local_ledger_path=task_view / "TELEGRAM_LEDGER.md",
            google_dir=workspace / "system" / "google",
            google_settings_path=workspace / "system" / "google" / "settings.json",
            google_client_secrets_path=workspace / "system" / "google" / "client_secret.json",
            google_token_path=workspace / "system" / "google" / "token.json",
            google_mirror_dir=workspace / "system" / "google-mirror",
            notebooklm_export_dir=life_dir / "notebooklm-exports",
        )
        return paths

    def _seed_db(self, db_path: Path) -> None:
        now = int(time.time())
        ensure_db(db_path)
        with connect_db(db_path) as conn:
            conn.execute(
                "INSERT INTO portfolios (id, slug, name, status, priority, review_cadence, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("portfolio-1", "dasho", "DashoContent", "active", 10, "weekly", now, now),
            )
            conn.execute(
                """
                INSERT INTO projects (
                  id, portfolio_id, slug, name, kind, tier, status, health, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("project-1", "portfolio-1", "athena", "Athena", "internal", "core", "active", "green", now, now),
            )
            conn.execute(
                """
                INSERT INTO tasks (
                  id, project_id, portfolio_id, title, owner, bucket, status, source_channel, source_chat_id,
                  created_at, updated_at, last_touched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("task-1", "project-1", "portfolio-1", "Finish parity flow", "ATHENA", "ATHENA", "queued", "telegram", "1937792843", now, now, now),
            )
            conn.commit()

    def _task_snapshot(self, db_path: Path) -> tuple[dict[str, object], dict[str, object], list[tuple[str, str | None, str | None]]]:
        with connect_db(db_path) as conn:
            task = query_one(
                conn,
                "SELECT status, resolution, completion_summary, completion_record_id FROM tasks WHERE id = ?",
                ("task-1",),
            )
            assert task is not None
            completion = query_one(
                conn,
                "SELECT resolution, summary, evidence_json, verified_by FROM completion_records WHERE id = ?",
                (task["completion_record_id"],),
            )
            assert completion is not None
            events = [
                (str(row["event_type"]), row["from_status"], row["to_status"])
                for row in conn.execute(
                    "SELECT event_type, from_status, to_status FROM task_events WHERE task_id = ? ORDER BY id",
                    ("task-1",),
                ).fetchall()
            ]
        return task, completion, events

    def _run_board_complete(self, db_path: Path) -> None:
        import athena.server as server_module  # noqa: E402

        server = importlib.reload(server_module)
        try:
            httpd = server.create_server("127.0.0.1", 0, paths=self._paths_for("board"))
        except PermissionError:
            self.skipTest("socket bind not permitted in this environment")
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            port = httpd.server_address[1]
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/tasks/task-1/complete",
                data=urllib.parse.urlencode(
                    {
                        "summary": "Completed the parity slice.",
                        "evidence": "pytest tests/test_parity.py\nboard route verified",
                        "verified_by": "fleire",
                    }
                ).encode("utf-8"),
                method="POST",
            )
            with urllib.request.urlopen(request) as response:
                json.loads(response.read().decode("utf-8"))
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=2)

    def test_completion_parity_between_state_cli_and_board(self) -> None:
        state_paths = self._paths_for("state")
        cli_paths = self._paths_for("cli")
        board_paths = self._paths_for("board")
        for paths in (state_paths, cli_paths, board_paths):
            self._seed_db(paths.db_path)

        complete_task(
            db_path=state_paths.db_path,
            task_id="task-1",
            summary="Completed the parity slice.",
            evidence=["pytest tests/test_parity.py", "board route verified"],
            verified_by="fleire",
            actor="test",
        )

        stdout = io.StringIO()
        with patch(
            "sys.argv",
            [
                "taskctl",
                "complete-task",
                "--db",
                str(cli_paths.db_path),
                "task-1",
                "--summary",
                "Completed the parity slice.",
                "--evidence",
                "pytest tests/test_parity.py",
                "--evidence",
                "board route verified",
                "--verified-by",
                "fleire",
                "--actor",
                "test",
            ],
        ), contextlib.redirect_stdout(stdout):
            exit_code = taskctl_main()
        self.assertEqual(exit_code, 0)

        env_values = {
            "ATHENA_DB_PATH": str(board_paths.db_path),
            "ATHENA_WORKSPACE_ROOT": str(board_paths.workspace_root),
            "ATHENA_WORKSPACE_TELEGRAM_ROOT": str(board_paths.workspace_telegram_root),
            "ATHENA_TASK_VIEW_DIR": str(board_paths.task_view_dir),
            "ATHENA_LIFE_DIR": str(board_paths.life_dir),
            "ATHENA_LEDGER_PATH": str(board_paths.ledger_path),
            "ATHENA_LOCAL_LEDGER_PATH": str(board_paths.local_ledger_path),
        }
        previous = {key: os.environ.get(key) for key in env_values}
        try:
            for key, value in env_values.items():
                os.environ[key] = value
            self._run_board_complete(board_paths.db_path)
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        state_snapshot = self._task_snapshot(state_paths.db_path)
        cli_snapshot = self._task_snapshot(cli_paths.db_path)
        board_snapshot = self._task_snapshot(board_paths.db_path)

        self.assertEqual(state_snapshot[0], cli_snapshot[0])
        self.assertEqual(state_snapshot[0], board_snapshot[0])
        self.assertEqual(state_snapshot[1], cli_snapshot[1])
        self.assertEqual(state_snapshot[1], board_snapshot[1])
        self.assertEqual(state_snapshot[2], cli_snapshot[2])
        self.assertEqual(state_snapshot[2], board_snapshot[2])


if __name__ == "__main__":
    unittest.main()
