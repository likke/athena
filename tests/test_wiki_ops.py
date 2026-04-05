from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from athena.config import default_paths
from athena.wiki_ops import connector_status, repo_discover, skill_read, wiki_health, wiki_refresh


def _test_paths(tmp_dir: Path):
    paths = default_paths()
    overrides = {
        "db_path": tmp_dir / "tasks.sqlite",
        "workspace_root": tmp_dir / "workspace",
        "workspace_telegram_root": tmp_dir / "workspace-telegram",
        "briefs_dir": tmp_dir / "workspace" / "system" / "briefs",
        "life_dir": tmp_dir / "workspace" / "life",
        "notebooklm_export_dir": tmp_dir / "workspace" / "life" / "notebooklm-exports",
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


class WikiOpsTests(unittest.TestCase):
    def test_skill_read_and_repo_discover(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            paths = _test_paths(tmp)
            skill_dir = paths.workspace_telegram_root / "skills" / "garmin"
            skill_dir.mkdir(parents=True, exist_ok=True)
            skill_path = skill_dir / "SKILL.md"
            skill_path.write_text("# Garmin\nRead Garmin data.\n", encoding="utf-8")

            repo_dir = paths.workspace_telegram_root / "tmp-athena-repo"
            (repo_dir / ".git").mkdir(parents=True, exist_ok=True)
            (repo_dir / "README.md").write_text("# Athena Repo\n", encoding="utf-8")

            result = skill_read("garmin", paths=paths, workspace="telegram")
            repos = repo_discover(paths=paths, root=paths.workspace_telegram_root, max_depth=3)

            self.assertTrue(result["ok"])
            self.assertEqual(Path(result["path"]).resolve(), skill_path.resolve())
            self.assertIn("Read Garmin data.", result["content"])
            self.assertTrue(repos["ok"])
            self.assertEqual(repos["repo_count"], 1)
            self.assertEqual(repos["repos"][0]["path"], str(repo_dir.resolve()))
            self.assertIn(str((repo_dir / "README.md").resolve()), repos["repos"][0]["docs"])

    def test_connector_status_and_wiki_health_write_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            paths = _test_paths(tmp)
            kb_root = paths.workspace_telegram_root / "knowledge-base"
            (kb_root / "config").mkdir(parents=True, exist_ok=True)
            (kb_root / "indexes").mkdir(parents=True, exist_ok=True)
            (kb_root / "wiki").mkdir(parents=True, exist_ok=True)
            (kb_root / "sources").mkdir(parents=True, exist_ok=True)
            (kb_root / "outputs").mkdir(parents=True, exist_ok=True)

            drive_folder = tmp / "Athena Drive Mirror"
            drive_folder.mkdir(parents=True, exist_ok=True)
            (drive_folder / "Founder OS.md").write_text("# Founder OS\n", encoding="utf-8")
            paths.google_settings_path.parent.mkdir(parents=True, exist_ok=True)
            paths.google_settings_path.write_text(
                (
                    "{\n"
                    '  "drive": {\n'
                    '    "local_folders": [\n'
                    f'      {{"name": "Athena Drive Mirror", "path": "{drive_folder}"}}\n'
                    "    ]\n"
                    "  }\n"
                    "}\n"
                ),
                encoding="utf-8",
            )
            drive_summary = paths.google_mirror_dir / "drive-local" / "athena-drive-mirror-summary.md"
            drive_summary.parent.mkdir(parents=True, exist_ok=True)
            drive_summary.write_text("- mirrored_files: 1\n- skipped_files: 0\n", encoding="utf-8")
            gmail_summary = paths.google_mirror_dir / "gmail" / "inbox-summary.md"
            gmail_summary.parent.mkdir(parents=True, exist_ok=True)
            gmail_summary.write_text("# Inbox Summary\n", encoding="utf-8")
            notebook_summary = paths.google_mirror_dir / "notebooklm" / "notebooklm-exports-summary.md"
            notebook_summary.parent.mkdir(parents=True, exist_ok=True)
            notebook_summary.write_text("# NotebookLM Summary\n", encoding="utf-8")
            notebook_source = tmp / "notebook.md"
            notebook_source.write_text("# Notebook\n", encoding="utf-8")
            transcript_source = tmp / "transcript.md"
            transcript_source.write_text("# Transcript\n", encoding="utf-8")
            gmail_source = tmp / "gmail.md"
            gmail_source.write_text("# Gmail Source\n", encoding="utf-8")

            (kb_root / "config" / "gmail-sources.json").write_text(
                (
                    "{\n"
                    '  "sources": [\n'
                    f'    {{"id": "gmail_summary", "title": "Inbox Summary", "source_path": "{gmail_source}", "target_path": "knowledge-base/sources/gmail/inbox-summary.md" }},\n'
                    '    {"id": "gmail_query", "title": "Alerts", "source_query": "failure OR urgent", "target_path": "knowledge-base/sources/gmail/alerts.md"}\n'
                    "  ]\n"
                    "}\n"
                ),
                encoding="utf-8",
            )
            (kb_root / "config" / "notebooklm-sources.json").write_text(
                f'{{"sources": [{{"id": "nb", "title": "Notebook", "source_path": "{notebook_source}", "target_path": "knowledge-base/sources/notebooklm/notebook.md"}}]}}\n',
                encoding="utf-8",
            )
            (kb_root / "config" / "transcript-sources.json").write_text(
                f'{{"sources": [{{"id": "tx", "title": "Transcript", "source_path": "{transcript_source}", "target_path": "knowledge-base/sources/transcripts/transcript.md"}}]}}\n',
                encoding="utf-8",
            )
            (kb_root / "sources" / "gmail").mkdir(parents=True, exist_ok=True)
            (kb_root / "sources" / "notebooklm").mkdir(parents=True, exist_ok=True)
            (kb_root / "sources" / "transcripts").mkdir(parents=True, exist_ok=True)
            (kb_root / "sources" / "gmail" / "inbox-summary.md").write_text("---\nstatus: imported\n---\n", encoding="utf-8")
            (kb_root / "sources" / "gmail" / "alerts.md").write_text("---\nstatus: imported\n---\n", encoding="utf-8")
            (kb_root / "sources" / "notebooklm" / "notebook.md").write_text("---\nstatus: imported\n---\n", encoding="utf-8")
            (kb_root / "sources" / "transcripts" / "transcript.md").write_text("---\nstatus: imported\n---\n", encoding="utf-8")
            (kb_root / "indexes" / "source-index.md").write_text("# Source Index\n", encoding="utf-8")
            (kb_root / "indexes" / "wiki-index.md").write_text("# Wiki Index\n", encoding="utf-8")
            (kb_root / "wiki" / "Home.md").write_text("---\ntitle: Home\nstatus: active\nupdated_at: 2026-04-05\ntags: []\n---\n\n# Sources\n", encoding="utf-8")
            (kb_root / "outputs" / "wiki-lint-report.md").write_text("# Lint\n", encoding="utf-8")

            with mock.patch(
                "athena.wiki_ops._garmin_status",
                return_value={
                    "name": "garmin",
                    "status": "degraded",
                    "last_message": "Garmin returned an incomplete all-zero profile.",
                },
            ):
                connectors = connector_status(paths=paths, kb_root=kb_root, write=True)
                health = wiki_health(paths=paths, kb_root=kb_root, write=True)

            self.assertTrue(connectors["ok"])
            self.assertEqual(connectors["connectors"]["drive_mirror"]["status"], "healthy")
            self.assertEqual(connectors["connectors"]["gmail"]["query_defined_sources"], 1)
            self.assertEqual(connectors["connectors"]["gmail"]["missing_wrappers"], 0)
            self.assertEqual(connectors["connectors"]["gmail"]["wrapper_status_counts"]["imported"], 2)
            self.assertTrue((kb_root / "state" / "connectors" / "gmail.json").exists())
            self.assertTrue((kb_root / "outputs" / "connector-status.md").exists())
            self.assertTrue(health["indexes_ok"])
            self.assertEqual(health["connector_states"]["garmin"], "degraded")
            self.assertTrue((kb_root / "outputs" / "wiki-health.md").exists())

    def test_wiki_refresh_runs_full_pipeline_and_writes_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            paths = _test_paths(tmp)
            kb_root = paths.workspace_telegram_root / "knowledge-base"
            scripts_dir = kb_root / "scripts"
            scripts_dir.mkdir(parents=True, exist_ok=True)
            for relative in ("config", "indexes", "outputs", "sources", "wiki"):
                (kb_root / relative).mkdir(parents=True, exist_ok=True)

            script_names = (
                "import_drive_mirror.py",
                "import_notebooklm.py",
                "import_gmail.py",
                "import_transcripts.py",
                "compile_kb.py",
                "lint_wiki.py",
            )
            for name in script_names:
                (scripts_dir / name).write_text("print('ok')\n", encoding="utf-8")

            with mock.patch(
                "athena.wiki_ops.connector_status",
                return_value={
                    "ok": True,
                    "connectors": {
                        "drive_mirror": {"status": "healthy"},
                        "gmail": {"status": "healthy"},
                        "notebooklm": {"status": "healthy"},
                        "transcripts": {"status": "healthy"},
                        "garmin": {"status": "degraded"},
                    },
                },
            ), mock.patch(
                "athena.wiki_ops.wiki_health",
                return_value={
                    "ok": True,
                    "indexes_ok": True,
                    "connector_states": {
                        "drive_mirror": "healthy",
                        "gmail": "healthy",
                    },
                },
            ):
                result = wiki_refresh(paths=paths, kb_root=kb_root)

            self.assertTrue(result["ok"])
            self.assertEqual([step["name"] for step in result["steps"]], [Path(name).stem for name in script_names])
            self.assertTrue((kb_root / "state" / "wiki-refresh-status.json").exists())
            self.assertTrue((kb_root / "outputs" / "wiki-refresh-status.md").exists())


if __name__ == "__main__":
    unittest.main()
