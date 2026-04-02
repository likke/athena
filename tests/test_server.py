import importlib
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.parse
import urllib.error
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
        "ATHENA_BRIEFS_DIR",
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
        briefs_dir = workspace / "system" / "briefs"
        life_dir = workspace / "life"
        for path in (workspace, workspace_telegram, ledger.parent, task_view, briefs_dir, life_dir):
            path.mkdir(parents=True, exist_ok=True)
        db_path = ledger.parent / "tasks.sqlite"
        env_values = {
            "ATHENA_DB_PATH": str(db_path),
            "ATHENA_WORKSPACE_ROOT": str(workspace),
            "ATHENA_WORKSPACE_TELEGRAM_ROOT": str(workspace_telegram),
            "ATHENA_TASK_VIEW_DIR": str(task_view),
            "ATHENA_BRIEFS_DIR": str(briefs_dir),
            "ATHENA_LIFE_DIR": str(life_dir),
            "ATHENA_LEDGER_PATH": str(ledger),
            "ATHENA_LOCAL_LEDGER_PATH": str(task_view / "TELEGRAM_LEDGER.md"),
        }
        self._env_backup = {key: os.environ.get(key) for key in self.ENV_KEYS}
        for key, value in env_values.items():
            os.environ[key] = value
        self.state_calls: list[dict[str, object]] = []
        state_stub = types.SimpleNamespace(
            capture_item=self._state_handler("capture"),
            create_task=self._state_handler("create"),
            start_task=self._state_handler("start"),
            block_task=self._state_handler("block"),
            complete_task=self._state_handler("complete"),
            reopen_task=self._state_handler("reopen"),
            update_project_status=self._state_handler("project"),
        )
        outbox_stub = types.SimpleNamespace(
            create_email_outbox=self._state_handler("outbox-create"),
            approve_outbox_items=self._state_handler("outbox-approve"),
            reject_outbox_items=self._state_handler("outbox-reject"),
            send_outbox_items=self._state_handler("outbox-send"),
        )
        sys.modules["athena.state"] = state_stub
        sys.modules["athena.outbox"] = outbox_stub
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
        sys.modules.pop("athena.outbox", None)

    def _state_handler(self, action: str):
        def handler(*args, **kwargs):
            target = kwargs.get("task_id") or kwargs.get("project_id") or kwargs.get("capture_id") or (args[0] if args else action)
            self.state_calls.append(
                {
                    "action": action,
                    "target": str(target),
                    "kwargs": kwargs,
                }
            )
            return {"ok": True, "action": action, "target": str(target)}

        return handler

    def _insert_sample(self, db_path: Path) -> None:
        now = int(time.time())
        brief_path = self.paths.briefs_dir / "weekly-ceo-brief-2026-03-31.md"
        brief_path.parent.mkdir(parents=True, exist_ok=True)
        brief_path.write_text(
            "\n".join(
                [
                    "# Athena CEO Weekly Brief",
                    "",
                    "- week_of: 2026-03-30",
                    "- generated_at: 2026-04-02 09:00 PHT",
                    "",
                    "## Executive summary",
                    "",
                    "- Protect founder focus this week.",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
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
                """
                INSERT OR IGNORE INTO source_documents (
                  id, kind, title, path, source_system, is_authoritative, last_synced_at, summary, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "brief-1",
                    "weekly_ceo_brief",
                    "Athena CEO Weekly Brief - Week of 2026-03-30",
                    str(brief_path.resolve()),
                    "athena",
                    0,
                    now,
                    "Protect founder focus this week.",
                    now,
                    now,
                ),
            )
            conn.execute(
                "INSERT OR IGNORE INTO awareness_briefs (scope_kind, scope_id, brief_type, content, created_at) VALUES (?, ?, ?, ?, ?)",
                ("global", "global", "status", "All systems go.", now),
            )
            conn.execute(
                "INSERT OR REPLACE INTO chat_state (channel, chat_id, current_task_id, last_user_intent, last_progress, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                ("telegram", "1937792843", "task-1", "status check", "Phase 4 done", now),
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO outbox_items (
                  id, task_id, project_id, provider, account_label, to_recipients, subject, body_text, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "outbox-1",
                    "task-1",
                    "project-1",
                    "gmail",
                    "primary",
                    "person@example.com",
                    "Board approval",
                    "Draft body",
                    "needs_approval",
                    now,
                    now,
                ),
            )
            conn.commit()

    def _start_server(self):
        try:
            httpd = self.server_module.create_server("127.0.0.1", 0, paths=self.paths)
        except PermissionError:
            self.skipTest("socket bind not permitted in this environment")
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        port = httpd.server_address[1]
        base = f"http://127.0.0.1:{port}"
        return httpd, thread, base

    def _post_json(self, url: str, data: dict[str, object]) -> dict[str, object]:
        encoded = urllib.parse.urlencode(data, doseq=True).encode("utf-8")
        request = urllib.request.Request(url, data=encoded, method="POST")
        with urllib.request.urlopen(request) as response:
            return json.loads(response.read().decode("utf-8"))

    def _post_form_no_redirect(self, url: str, data: dict[str, object]):
        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None

        encoded = urllib.parse.urlencode(data, doseq=True).encode("utf-8")
        request = urllib.request.Request(url, data=encoded, method="POST")
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            return opener.open(request)
        except urllib.error.HTTPError as exc:
            return exc

    def test_gather_data_includes_sections(self):
        data = self.server_module._gather_data(self.paths)
        self.assertIn("dashboard", data)
        self.assertIn("tasks", data)
        self.assertTrue(data["life"]["areas"])
        self.assertTrue(data["projects"])
        self.assertTrue(data["briefs"])
        self.assertTrue(data["weekly_briefs"]["items"])

    def test_render_html_contains_sections(self):
        data = self.server_module._gather_data(self.paths)
        html_blob = self.server_module._render_html(data)
        self.assertIn("Mission control for what matters right now.", html_blob)
        self.assertIn("Athena OS", html_blob)
        self.assertIn('href="/board"', html_blob)
        self.assertIn("Email approvals", html_blob)
        self.assertIn("Portfolio health", html_blob)
        self.assertIn("CEO weekly brief", html_blob)

    def test_render_html_projects_page_contains_project_sections(self):
        data = self.server_module._gather_data(self.paths)
        html_blob = self.server_module._render_html(data, current_path="/projects")
        self.assertIn("DashoContent", html_blob)
        self.assertIn("Projects", html_blob)
        self.assertIn("Repos", html_blob)
        self.assertIn("Brand compliance", html_blob)

    def test_outbox_page_shows_configured_gmail_account(self):
        self.paths.google_dir.mkdir(parents=True, exist_ok=True)
        self.paths.google_settings_path.write_text(
            json.dumps(
                {
                    "gmail": {
                        "enabled": True,
                        "default_account": "athena",
                        "accounts": [
                            {
                                "label": "athena",
                                "email": "athena@thirdteam.org",
                                "display_name": "Athena",
                                "default": True,
                            }
                        ],
                    }
                }
            ),
            encoding="utf-8",
        )
        data = self.server_module._gather_data(self.paths)
        html_blob = self.server_module._render_html(data, current_path="/outbox")
        self.assertIn('name="account_label"', html_blob)
        self.assertIn("athena@thirdteam.org", html_blob)

    def test_api_endpoints_return_expected_payloads(self):
        httpd, thread, base = self._start_server()
        try:
            tasks = json.loads(urllib.request.urlopen(f"{base}/api/tasks").read().decode("utf-8"))
            self.assertTrue(any(row["id"] == "task-1" for row in tasks))

            projects = json.loads(urllib.request.urlopen(f"{base}/api/projects").read().decode("utf-8"))
            self.assertTrue(any(row["id"] == "project-1" for row in projects))

            kanban = json.loads(urllib.request.urlopen(f"{base}/api/kanban").read().decode("utf-8"))
            self.assertIn("ATHENA", kanban)

            outbox = json.loads(urllib.request.urlopen(f"{base}/api/outbox").read().decode("utf-8"))
            self.assertTrue(any(row["id"] == "outbox-1" for row in outbox))

            weekly_briefs = json.loads(urllib.request.urlopen(f"{base}/api/weekly-briefs").read().decode("utf-8"))
            self.assertTrue(weekly_briefs["items"])
            self.assertEqual(weekly_briefs["items"][0]["id"], "brief-1")

            health = json.loads(urllib.request.urlopen(f"{base}/api/health").read().decode("utf-8"))
            self.assertTrue(health["ok"])
            self.assertTrue(str(health["db"]).endswith("tasks.sqlite"))
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=2)

    def test_html_routes_render_page_specific_content(self):
        httpd, thread, base = self._start_server()
        try:
            pages = {
                "/": "Mission control for what matters right now.",
                "/board": "Kanban lists for capture, work in progress, blockers, and done.",
                "/inbox": "Raw captures stay here until Athena or Fleire turns them into work.",
                "/outbox": "Batch approve, reject, and send from one place.",
                "/projects": "The authoritative operating view for the projects Athena is tracking.",
                "/briefs": "A weekly founder packet grounded in Athena's local life, portfolio, task, approval, and calendar state.",
                "/context": "The rules, goals, and people Athena uses to hold the larger picture.",
            }
            for route, marker in pages.items():
                body = urllib.request.urlopen(f"{base}{route}").read().decode("utf-8")
                self.assertIn(marker, body)
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=2)

    def test_complete_task_api_passes_evidence_and_verifier(self):
        httpd, thread, base = self._start_server()
        try:
            response = self._post_json(
                f"{base}/api/tasks/task-1/complete",
                {
                    "summary": "Shipped board mutation tests.",
                    "evidence": "pytest tests/test_server.py\nboard api smoke",
                    "verified_by": "fleire",
                },
            )
            self.assertEqual(response["action"], "complete")
            call = self.state_calls[-1]
            self.assertEqual(call["action"], "complete")
            kwargs = call["kwargs"]
            self.assertEqual(kwargs["task_id"], "task-1")
            self.assertEqual(kwargs["summary"], "Shipped board mutation tests.")
            self.assertEqual(kwargs["evidence"], ["pytest tests/test_server.py", "board api smoke"])
            self.assertEqual(kwargs["verified_by"], "fleire")
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=2)

    def test_capture_redirect_returns_to_requested_page(self):
        httpd, thread, base = self._start_server()
        try:
            response = self._post_form_no_redirect(
                f"{base}/captures/new",
                {
                    "raw_text": "Need a cleaner inbox",
                    "redirect_to": "/inbox",
                },
            )
            self.assertEqual(response.code, 303)
            location = response.headers.get("Location", "")
            self.assertTrue(location.startswith("/inbox?"), location)
            self.assertIn("Captured+into+inbox", location)
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=2)

    def test_outbox_batch_api_supports_approve_and_send(self):
        httpd, thread, base = self._start_server()
        try:
            approve = self._post_json(
                f"{base}/api/outbox/batch",
                {
                    "action": "approve",
                    "outbox_id": ["outbox-1", "outbox-2"],
                    "note": "Batch approved",
                },
            )
            self.assertEqual(approve["action"], "outbox-approve")
            approve_call = self.state_calls[-1]
            self.assertEqual(approve_call["action"], "outbox-approve")
            self.assertEqual(approve_call["kwargs"]["outbox_ids"], ["outbox-1", "outbox-2"])

            send = self._post_json(
                f"{base}/api/outbox/batch",
                {
                    "action": "send",
                    "outbox_id": ["outbox-1"],
                },
            )
            self.assertEqual(send["action"], "outbox-send")
            send_call = self.state_calls[-1]
            self.assertEqual(send_call["action"], "outbox-send")
            self.assertEqual(send_call["kwargs"]["outbox_ids"], ["outbox-1"])
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=2)
