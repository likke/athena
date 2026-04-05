from __future__ import annotations

import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from athena.config import default_paths
from athena.db import connect_db, ensure_db, now_ts
from athena.synthesis import LATEST_WEEKLY_CEO_BRIEF, WEEKLY_CEO_BRIEF_KIND
from athena.sync import (
    LocalDriveReadTimeoutError,
    NOTEBOOKLM_LIFE_BUNDLE,
    ensure_notebooklm_life_bundle,
    refresh_awareness_briefs,
    run_sync,
    scan_project_repos,
    sync_local_drive_folders,
    sync_life_docs,
    sync_notebooklm_exports,
)


def _test_paths(tmp_dir: Path):
    paths = default_paths()
    overrides = {
        "db_path": tmp_dir / "tasks.sqlite",
        "workspace_root": tmp_dir / "workspace",
        "workspace_telegram_root": tmp_dir / "workspace-telegram",
        "briefs_dir": tmp_dir / "workspace" / "system" / "briefs",
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


class SyncTestCase(unittest.TestCase):
    def test_sync_life_docs_and_notebooklm(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            paths = _test_paths(tmp)
            ensure_db(paths=paths)
            paths.life_dir.mkdir(parents=True, exist_ok=True)
            north = paths.life_dir / "NORTH_STAR.md"
            north.write_text("# North Star\nBe clear.")
            (paths.life_dir / "CURRENT_SEASON.md").write_text("Season content.")
            notebook_dir = paths.notebooklm_export_dir
            notebook_dir.mkdir(parents=True, exist_ok=True)
            (notebook_dir / "notebook-note.md").write_text("Notebook insight.")

            with connect_db(paths.db_path) as conn:
                now = now_ts()
                conn.execute(
                    """
                    INSERT INTO source_documents (
                      id, kind, title, path, external_url, source_system, is_authoritative,
                      last_synced_at, summary, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "doc_north_star",
                        "life_doc",
                        "North Star",
                        str(north),
                        None,
                        "local_markdown",
                        1,
                        now,
                        "Old summary",
                        now,
                        now,
                    ),
                )
                docs = sync_life_docs(conn, paths.life_dir)
                notebooks = sync_notebooklm_exports(conn, notebook_dir)
                conn.commit()

            self.assertCountEqual(docs, ["CURRENT_SEASON.md", "NORTH_STAR.md"])
            self.assertEqual(notebooks, ["notebook-note.md"])
            with connect_db(paths.db_path) as conn:
                rows = list(conn.execute("SELECT id, is_authoritative, kind, source_system FROM source_documents"))
                self.assertEqual(len(rows), 3)
                authoritative = [row for row in rows if row["is_authoritative"]]
                self.assertEqual(len(authoritative), 2)
                self.assertTrue(all(row["kind"] == "life_doc" for row in authoritative))
                self.assertIn("doc_north_star", [row["id"] for row in rows])
                north_row = conn.execute("SELECT source_system FROM source_documents WHERE id = 'doc_north_star'").fetchone()
                self.assertEqual(north_row["source_system"], "life-doc")
                north_summary = conn.execute("SELECT summary FROM source_documents WHERE id = 'doc_north_star'").fetchone()
                self.assertEqual(north_summary["summary"], "Be clear.")
                self.assertEqual([row["kind"] for row in rows if not row["is_authoritative"]], ["notebooklm"])

    def test_sync_local_drive_folders_imports_recursive_text_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            paths = _test_paths(tmp)
            ensure_db(paths=paths)
            local_drive = tmp / "GoogleDrive" / "My Drive" / "Athena Drive Mirror"
            nested = local_drive / "Collected Call Transcripts - 2026-04-03"
            nested.mkdir(parents=True, exist_ok=True)
            (local_drive / "Founder OS.md").write_text("# Founder OS\nKeep context tight.\n", encoding="utf-8")
            (nested / "lorna.txt").write_text("Lorna transcript summary", encoding="utf-8")
            (nested / "ignore.png").write_text("not text", encoding="utf-8")
            paths.google_settings_path.parent.mkdir(parents=True, exist_ok=True)
            paths.google_settings_path.write_text(
                f"""
{{
  "drive": {{
    "local_folders": [
      {{
        "name": "Athena Drive Mirror",
        "path": "{local_drive}"
      }}
    ]
  }}
}}
""".strip(),
                encoding="utf-8",
            )

            with connect_db(paths.db_path) as conn:
                result = sync_local_drive_folders(conn, paths)
                conn.commit()

            self.assertEqual(result["local_drive_files"], 2)
            self.assertEqual(result["local_drive_folders"], 1)
            summary_path = paths.google_mirror_dir / "drive-local" / "athena-drive-mirror-summary.md"
            self.assertTrue(summary_path.exists())
            summary_text = summary_path.read_text(encoding="utf-8")
            self.assertIn("mirrored_files: 2", summary_text)
            self.assertIn("Collected Call Transcripts - 2026-04-03/lorna.txt", summary_text)
            with connect_db(paths.db_path) as conn:
                rows = list(
                    conn.execute(
                        "SELECT source_system, kind, title FROM source_documents WHERE source_system = 'gdrive-local' ORDER BY title"
                    )
                )
            self.assertEqual(
                [(row["source_system"], row["kind"]) for row in rows],
                [
                    ("gdrive-local", "drive_file_summary"),
                    ("gdrive-local", "drive_file"),
                    ("gdrive-local", "drive_file"),
                ],
            )

    def test_sync_local_drive_folders_skips_unreadable_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            paths = _test_paths(tmp)
            ensure_db(paths=paths)
            local_drive = tmp / "GoogleDrive" / "My Drive" / "Athena Drive Mirror"
            local_drive.mkdir(parents=True, exist_ok=True)
            good = local_drive / "Founder OS.md"
            bad = local_drive / "Offloaded.md"
            good.write_text("# Founder OS\nKeep context tight.\n", encoding="utf-8")
            bad.write_text("placeholder", encoding="utf-8")
            bad.chmod(0)
            paths.google_settings_path.parent.mkdir(parents=True, exist_ok=True)
            paths.google_settings_path.write_text(
                f"""
{{
  "drive": {{
    "local_folders": [
      {{
        "name": "Athena Drive Mirror",
        "path": "{local_drive}"
      }}
    ]
  }}
}}
""".strip(),
                encoding="utf-8",
            )

            try:
                with connect_db(paths.db_path) as conn:
                    result = sync_local_drive_folders(conn, paths)
                    conn.commit()
            finally:
                bad.chmod(0o644)

            self.assertEqual(result["local_drive_files"], 1)
            self.assertEqual(result["local_drive_skipped"], 1)
            summary_path = paths.google_mirror_dir / "drive-local" / "athena-drive-mirror-summary.md"
            summary_text = summary_path.read_text(encoding="utf-8")
            self.assertIn("skipped: Offloaded.md", summary_text)
            self.assertIn("skipped_files: 1", summary_text)
            with connect_db(paths.db_path) as conn:
                rows = list(
                    conn.execute(
                        "SELECT kind, title FROM source_documents WHERE source_system = 'gdrive-local' ORDER BY title"
                    )
                )
            self.assertEqual(
                [(row["kind"], row["title"]) for row in rows],
                [
                    ("drive_file_summary", "Athena Drive Mirror Local Summary"),
                    ("drive_file", "Founder Os"),
                ],
            )

    def test_sync_local_drive_folders_skips_timed_out_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            paths = _test_paths(tmp)
            ensure_db(paths=paths)
            local_drive = tmp / "GoogleDrive" / "My Drive" / "Athena Drive Mirror"
            local_drive.mkdir(parents=True, exist_ok=True)
            good = local_drive / "Founder OS.md"
            slow = local_drive / "Slow.md"
            good.write_text("# Founder OS\nKeep context tight.\n", encoding="utf-8")
            slow.write_text("slow", encoding="utf-8")
            paths.google_settings_path.parent.mkdir(parents=True, exist_ok=True)
            paths.google_settings_path.write_text(
                f"""
{{
  "drive": {{
    "local_folders": [
      {{
        "name": "Athena Drive Mirror",
        "path": "{local_drive}"
      }}
    ]
  }}
}}
""".strip(),
                encoding="utf-8",
            )

            from athena import sync as sync_module

            def fake_read_local_text(path, *, timeout_seconds=2):
                if path.name == slow.name:
                    raise LocalDriveReadTimeoutError(f"Timed out reading {path}")
                return path.read_text(encoding="utf-8", errors="replace")

            with connect_db(paths.db_path) as conn:
                with mock.patch.object(sync_module, "_read_local_text", new=fake_read_local_text):
                    result = sync_local_drive_folders(conn, paths)
                conn.commit()

            self.assertEqual(result["local_drive_files"], 1)
            self.assertEqual(result["local_drive_skipped"], 1)
            summary_path = paths.google_mirror_dir / "drive-local" / "athena-drive-mirror-summary.md"
            summary_text = summary_path.read_text(encoding="utf-8")
            self.assertIn("skipped: Slow.md", summary_text)
            with connect_db(paths.db_path) as conn:
                rows = list(
                    conn.execute(
                        "SELECT kind, title FROM source_documents WHERE source_system = 'gdrive-local' ORDER BY title"
                    )
                )
            self.assertEqual(
                [(row["kind"], row["title"]) for row in rows],
                [
                    ("drive_file_summary", "Athena Drive Mirror Local Summary"),
                    ("drive_file", "Founder Os"),
                ],
            )

    def test_ensure_notebooklm_life_bundle_generates_bundle_from_life_docs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            paths = _test_paths(tmp)
            paths.life_dir.mkdir(parents=True, exist_ok=True)
            (paths.life_dir / "NORTH_STAR.md").write_text("# North Star\nProtect focus.", encoding="utf-8")
            (paths.life_dir / "CURRENT_SEASON.md").write_text("Builder season.", encoding="utf-8")

            bundle = ensure_notebooklm_life_bundle(paths.life_dir, paths.notebooklm_export_dir)

            self.assertIsNotNone(bundle)
            assert bundle is not None
            self.assertEqual(bundle.name, NOTEBOOKLM_LIFE_BUNDLE)
            content = bundle.read_text(encoding="utf-8")
            self.assertIn("Athena Life Context Bundle", content)
            self.assertIn("North Star", content)
            self.assertIn("Protect focus.", content)
            self.assertIn("Current Season", content)
            self.assertIn("Builder season.", content)

    def test_scan_project_repos_updates_projects(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            paths = _test_paths(tmp)
            ensure_db(paths=paths)
            repo_dir = tmp / "repo"
            repo_dir.mkdir(parents=True)
            subprocess.run(["git", "init", "-b", "main"], cwd=repo_dir, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_dir, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=repo_dir, check=True)
            (repo_dir / "README.md").write_text("hello")
            subprocess.run(["git", "add", "."], cwd=repo_dir, check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=repo_dir, check=True)

            with connect_db(paths.db_path) as conn:
                now = now_ts()
                conn.execute(
                    "INSERT INTO portfolios (id, slug, name, status, priority, review_cadence, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    ("port", "port", "Portfolio", "active", 0, "weekly", now, now),
                )
                conn.execute(
                    "INSERT INTO projects (id, portfolio_id, slug, name, kind, tier, status, health, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    ("proj", "port", "proj", "Project", "product", "core", "active", "red", now, now),
                )
                conn.execute(
                    "INSERT INTO project_repos (project_id, repo_name, repo_path, role, is_primary) VALUES (?, ?, ?, ?, ?)",
                    ("proj", "demo", str(repo_dir), "source", 1),
                )
                conn.commit()
                summary = scan_project_repos(conn)
                conn.commit()

            self.assertEqual(summary["scanned"], 1)
            self.assertEqual(summary["projects_updated"], 1)
            with connect_db(paths.db_path) as conn:
                repo_row = conn.execute("SELECT last_seen_branch, last_seen_commit, last_seen_dirty FROM project_repos").fetchone()
                self.assertTrue(repo_row["last_seen_branch"])
                self.assertTrue(repo_row["last_seen_commit"])
                self.assertEqual(repo_row["last_seen_dirty"], 0)
                project_row = conn.execute("SELECT health, last_real_progress_at FROM projects WHERE id = 'proj'").fetchone()
                self.assertEqual(project_row["health"], "green")
                self.assertIsNotNone(project_row["last_real_progress_at"])

    def test_refresh_awareness_briefs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            paths = _test_paths(tmp)
            ensure_db(paths=paths)
            with connect_db(paths.db_path) as conn:
                now = now_ts()
                conn.execute(
                    "INSERT INTO portfolios (id, slug, name, status, priority, review_cadence, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    ("port", "port", "Portfolio", "active", 0, "weekly", now, now),
                )
                conn.execute(
                    "INSERT INTO projects (id, portfolio_id, slug, name, kind, tier, status, health, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    ("proj", "port", "proj", "Project", "product", "core", "active", "green", now, now),
                )
                conn.execute(
                    """INSERT INTO tasks (id, title, owner, bucket, status, priority, portfolio_id, project_id, created_at, updated_at, last_touched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    ("task", "Sync", "ATHENA", "ATHENA", "queued", 1, "port", "proj", now, now, now),
                )
                conn.commit()
                result = refresh_awareness_briefs(conn)
                conn.commit()

            self.assertGreaterEqual(result["portfolios"], 1)
            self.assertGreaterEqual(result["projects"], 1)
            with connect_db(paths.db_path) as conn:
                scopes = {row["scope_kind"] for row in conn.execute("SELECT scope_kind FROM awareness_briefs")}
                self.assertIn("global", scopes)
                self.assertIn("portfolio", scopes)
                self.assertIn("project", scopes)

    def test_run_sync_google_ingests_local_notebook_exports(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            paths = _test_paths(tmp)
            ensure_db(paths=paths)
            paths.notebooklm_export_dir.mkdir(parents=True, exist_ok=True)
            (paths.notebooklm_export_dir / "life-sync.txt").write_text("Life update from mirrored export.")

            result = run_sync("google", paths=paths)

            self.assertEqual(result["command"], "google")
            self.assertEqual(result["notebook_exports"], 1)
            with connect_db(paths.db_path) as conn:
                row = conn.execute(
                    "SELECT source_system, kind, summary FROM source_documents WHERE path LIKE ?",
                    (f"%life-sync.txt",),
                ).fetchone()
                self.assertIsNotNone(row)
                self.assertEqual(row["source_system"], "NotebookLM")
                self.assertEqual(row["kind"], "notebooklm")
                self.assertEqual(row["summary"], "Life update from mirrored export.")

    def test_run_sync_google_seeds_life_bundle_when_notebook_exports_are_empty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            paths = _test_paths(tmp)
            ensure_db(paths=paths)
            paths.life_dir.mkdir(parents=True, exist_ok=True)
            (paths.life_dir / "NORTH_STAR.md").write_text("# North Star\nKeep the main thing the main thing.", encoding="utf-8")

            result = run_sync("google", paths=paths)

            self.assertEqual(result["command"], "google")
            self.assertEqual(result["notebook_exports"], 1)
            bundle = paths.notebooklm_export_dir / NOTEBOOKLM_LIFE_BUNDLE
            self.assertTrue(bundle.exists())
            with connect_db(paths.db_path) as conn:
                row = conn.execute(
                    "SELECT source_system, kind, summary FROM source_documents WHERE path = ?",
                    (str(bundle.resolve()),),
                ).fetchone()
                self.assertIsNotNone(row)
                assert row is not None
                self.assertEqual(row["source_system"], "NotebookLM")
                self.assertEqual(row["kind"], "notebooklm")
                self.assertEqual(row["summary"], "This file is generated from Athena's canonical local life docs.")

    def test_run_sync_weekly_brief_generates_file_and_source_document(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            paths = _test_paths(tmp)
            ensure_db(paths=paths)
            calendar_dir = paths.google_mirror_dir / "calendar"
            calendar_dir.mkdir(parents=True, exist_ok=True)
            agenda_path = calendar_dir / "upcoming-summary.md"
            agenda_path.write_text(
                "\n".join(
                    [
                        "## primary",
                        "",
                        "- 2026-04-03 10:00 — Founder review",
                        "- 2026-04-04 14:00 — Client call",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            with connect_db(paths.db_path) as conn:
                now = now_ts()
                conn.execute(
                    "INSERT INTO life_areas (id, slug, name, status, priority, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    ("area", "core", "Core", "active", 10, now, now),
                )
                conn.execute(
                    """
                    INSERT INTO life_goals (
                      id, life_area_id, slug, title, horizon, status, current_focus, supporting_rule,
                      risk_if_ignored, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "goal",
                        "area",
                        "goal",
                        "Protect founder focus",
                        "quarter",
                        "active",
                        "Keep DashoContent revenue work ahead of side quests",
                        "Revenue before random motion",
                        "Priority drift",
                        now,
                        now,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO portfolios (id, slug, name, status, priority, review_cadence, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    ("portfolio", "dasho", "DashoContent", "active", 50, "weekly", now, now),
                )
                conn.execute(
                    """
                    INSERT INTO projects (
                      id, life_area_id, life_goal_id, portfolio_id, slug, name, kind, tier, status, health,
                      current_goal, next_milestone, blocker, last_real_progress_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "project",
                        "area",
                        "goal",
                        "portfolio",
                        "brand-score",
                        "Brand score MVP",
                        "product",
                        "core",
                        "blocked",
                        "yellow",
                        "Ship the scoring pass",
                        "Lock first production test",
                        "Need final scoring rule decision",
                        now,
                        now,
                        now,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO tasks (
                      id, title, owner, bucket, status, priority, project_id, portfolio_id, life_goal_id,
                      blocker, requires_approval, created_at, updated_at, last_touched_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "task",
                        "Approve scoring note",
                        "ATHENA",
                        "BLOCKED",
                        "blocked",
                        10,
                        "project",
                        "portfolio",
                        "goal",
                        "Waiting on founder decision",
                        1,
                        now,
                        now,
                        now,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO outbox_items (
                      id, task_id, project_id, provider, account_label, to_recipients, subject, body_text, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "outbox",
                        "task",
                        "project",
                        "gmail",
                        "athena",
                        "founder@example.com",
                        "Need your sign-off",
                        "Please approve the scoring decision.",
                        "needs_approval",
                        now,
                        now,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO source_documents (
                      id, kind, title, path, source_system, is_authoritative, last_synced_at, summary, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "agenda",
                        "calendar_agenda",
                        "Upcoming Calendar Agenda",
                        str(agenda_path.resolve()),
                        "gcal",
                        0,
                        now,
                        "2 mirrored calendar events",
                        now,
                        now,
                    ),
                )
                conn.commit()

            result = run_sync("weekly-brief", paths=paths)

            brief_path = Path(str(result["weekly_brief_path"]))
            self.assertTrue(brief_path.exists())
            self.assertTrue((paths.briefs_dir / LATEST_WEEKLY_CEO_BRIEF).exists())
            content = brief_path.read_text(encoding="utf-8")
            self.assertIn("Athena CEO Weekly Brief", content)
            self.assertIn("Protect founder focus", content)
            self.assertIn("Founder review", content)
            with connect_db(paths.db_path) as conn:
                row = conn.execute(
                    "SELECT kind, source_system, summary FROM source_documents WHERE kind = ?",
                    (WEEKLY_CEO_BRIEF_KIND,),
                ).fetchone()
                weekly_brief = conn.execute(
                    "SELECT content FROM awareness_briefs WHERE scope_kind = 'global' AND brief_type = 'weekly_ceo'"
                ).fetchone()
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(row["kind"], WEEKLY_CEO_BRIEF_KIND)
            self.assertEqual(row["source_system"], "athena")
            self.assertIn("Protect founder focus", row["summary"])
            self.assertIsNotNone(weekly_brief)


if __name__ == "__main__":
    unittest.main()
