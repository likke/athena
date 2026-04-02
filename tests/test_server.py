import importlib
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.request
import types
import sys
from pathlib import Path

from athena import db as db_module
from athena.config import default_paths


class AthenaServerTest(unittest.TestCase):
    ENV_KEYS = [
        "ATHENA_DB_PATH",
        "ATHENA_WORKSPACE_ROOT",
        "ATHENA_WORKSPACE_TELEGRAM_ROOT",
        "ATHENA_TASK_VIEW_DIR",
        "ATHENA_LIFE_DIR",
        "ATHENA_LEDGER_PATH",
        "ATHENA_LOCAL_LEDGER_PATH",
    ]

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        workspace = Path(self.tmpdir.name) / "workspace"
        workspace_telegram = Path(self.tmpdir.name) / "workspace-telegram"
        ledger = workspace / "system" / "task-ledger" / "telegram-1937792843.md"
        task_view = workspace_telegram / "task-system"
        life_dir = workspace / "life"
        for path in (workspace, workspace_telegram, ledger.parent, task_view, life_dir):
            path.mkdir(parents=True, exist_ok=True)
        db_path = ledger.parent / "tasks.sqlite"
        env_values = {
            "ATHENA_DB_PATH": str(db_path),
            "ATHENA_WORKSPACE_ROOT": str(workspace),
            "ATHENA_WORKSPACE_TELEGRAM_ROOT": str(workspace_telegram),
            "ATHENA_TASK_VIEW_DIR": str(task_view),
            "ATHENA_LIFE_DIR": str(life_dir),
            "ATHENA_LEDGER_PATH": str(ledger),
            "ATHENA_LOCAL_LEDGER_PATH": str(task_view / "TELEGRAM_LEDGER.md"),
        }
        self._env_backup = {key: os.environ.get(key) for key in self.ENV_KEYS}
        for key, value in env_values.items():
            os.environ[key] = value
        self.state_calls: list[tuple[str, str]] = []
        state_stub = types.SimpleNamespace(
            capture_item=self._state_handler("capture"),
            create_task=self._state_handler("create"),
            start_task=self._state_handler("start"),
            block_task=self._state_handler("block"),
            complete_task=self._state_handler("complete"),
            reopen_task=self._state_handler("reopen"),
            update_project_status=self._state_handler("project"),
        )
        sys.modules["athena.state"] = state_stub
        self.paths = default_paths()
        db_module.ensure_db(paths=self.paths)
        self._insert_sample(self.paths.db_path)
        import athena.server as server_module  # noqa: E402

        self.server_module = importlib.reload(server_module)

    def tearDown(self):
        if hasattr(self, "tmpdir"):
            self.tmpdir.cleanup()
        for key, value in self._env_backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        sys.modules.pop("athena.state", None)

    def _state_handler(self, action: str):
        def handler(*args, **kwargs):
            target = kwargs.get("task_id") or kwargs.get("project_id") or kwargs.get("capture_id") or (args[0] if args else action)
            self.state_calls.append((action, str(target)))
            return {"ok": True, "action": action, "target": str(target)}

        return handler

    def _insert_sample(self, db_path: Path) -> None:
        now = int(time.time())
        with db_module.connect_db(db_path) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO life_areas (id, slug, name, status, priority, notes, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("area-1", "core", "Life", "active", 10, "", now, now),
            )
            conn.execute(
                "INSERT OR IGNORE INTO life_goals (id, life_area_id, slug, title, horizon, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("goal-1", "area-1", "goal-core", "Ship MVP", "quarter", "active", now, now),
            )
            conn.execute(
                "INSERT OR IGNORE INTO portfolios (id, slug, name, status, priority, review_cadence, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("portfolio-1", "dashoc", "DashoContent", "active", 20, "weekly", now, now),
            )
            conn.execute(
                "INSERT OR IGNORE INTO projects (id, portfolio_id, slug, name, kind, tier, status, health, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("project-1", "portfolio-1", "brand-score", "Brand compliance", "product", "core", "active", "green", now, now),
            )
            conn.execute(
                "INSERT OR IGNORE INTO tasks (id, title, owner, bucket, status, priority, created_at, updated_at, last_touched_at, portfolio_id, project_id, source_channel, source_chat_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("task-1", "Review model", "ATHENA", "ATHENA", "queued", 5, now, now, now, "portfolio-1", "project-1", "telegram", "1937792843"),
            )
            conn.execute(
                "INSERT OR IGNORE INTO source_documents (id, kind, title, source_system, is_authoritative, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("source-1", "life", "NotebookLM Goals", "notebooklm", 0, now, now),
            )
            conn.execute(
                "INSERT OR IGNORE INTO awareness_briefs (scope_kind, scope_id, brief_type, content, created_at) VALUES (?, ?, ?, ?, ?)",
                ("global", "global", "status", "All systems go.", now),
            )
            conn.execute(
                "INSERT OR REPLACE INTO chat_state (channel, chat_id, current_task_id, last_user_intent, last_progress, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                ("telegram", "1937792843", "task-1", "status check", "Phase 4 done", now),
            )
            conn.commit()

    def test_gather_data_includes_sections(self):
        data = self.server_module._gather_data(self.paths)
        self.assertIn("dashboard", data)
        self.assertIn("tasks", data)
        self.assertTrue(data["life"]["areas"])
        self.assertTrue(data["projects"])
        self.assertTrue(data["briefs"])

    def test_render_html_contains_sections(self):
        data = self.server_module._gather_data(self.paths)
        html_blob = self.server_module._render_html(data)
        self.assertIn("Life context", html_blob)
        self.assertIn("DashoContent", html_blob)
        self.assertIn("Tasks", html_blob)
        self.assertIn("Board", html_blob)
        self.assertIn("Quick Capture", html_blob)

    def test_api_endpoints_return_expected_payloads(self):
        try:
            httpd = self.server_module.create_server("127.0.0.1", 0, paths=self.paths)
        except PermissionError:
            self.skipTest("socket bind not permitted in this environment")
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        port = httpd.server_address[1]
        base = f"http://127.0.0.1:{port}"

        try:
            tasks = json.loads(urllib.request.urlopen(f"{base}/api/tasks").read().decode("utf-8"))
            self.assertTrue(any(row["id"] == "task-1" for row in tasks))

            projects = json.loads(urllib.request.urlopen(f"{base}/api/projects").read().decode("utf-8"))
            self.assertTrue(any(row["id"] == "project-1" for row in projects))

            kanban = json.loads(urllib.request.urlopen(f"{base}/api/kanban").read().decode("utf-8"))
            self.assertIn("ATHENA", kanban)

            health = json.loads(urllib.request.urlopen(f"{base}/api/health").read().decode("utf-8"))
            self.assertTrue(health["ok"])
            self.assertTrue(str(health["db"]).endswith("tasks.sqlite"))
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=2)
