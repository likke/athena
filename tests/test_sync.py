from __future__ import annotations

import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from athena.config import default_paths
from athena.db import connect_db, ensure_db, now_ts
from athena.sync import (
    NOTEBOOKLM_LIFE_BUNDLE,
    ensure_notebooklm_life_bundle,
    refresh_awareness_briefs,
    run_sync,
    scan_project_repos,
    sync_life_docs,
    sync_notebooklm_exports,
)


def _test_paths(tmp_dir: Path):
    paths = default_paths()
    overrides = {
        "db_path": tmp_dir / "tasks.sqlite",
        "workspace_root": tmp_dir / "workspace",
        "workspace_telegram_root": tmp_dir / "workspace-telegram",
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


if __name__ == "__main__":
    unittest.main()
