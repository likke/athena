from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from athena.config import default_paths
from athena.wiki_jobs import (
    build_backlinks_rebuild,
    build_breakdown,
    build_daily_digest,
    build_index_rebuild,
    build_missing_pages,
    build_promotion_queue,
    build_quality_audit,
    build_stale_scan,
)


def _test_paths(tmp_dir: Path):
    paths = default_paths()
    workspace_root = tmp_dir / "workspace"
    workspace_telegram_root = tmp_dir / "workspace-telegram"
    return replace(
        paths,
        workspace_root=workspace_root,
        workspace_telegram_root=workspace_telegram_root,
        db_path=tmp_dir / "tasks.sqlite",
        briefs_dir=workspace_root / "system" / "briefs",
        life_dir=tmp_dir / "life",
        notebooklm_export_dir=tmp_dir / "life" / "notebooklm-exports",
        google_dir=workspace_root / "system" / "google",
        google_settings_path=workspace_root / "system" / "google" / "settings.json",
        google_client_secrets_path=workspace_root / "system" / "google" / "client_secret.json",
        google_token_path=workspace_root / "system" / "google" / "token.json",
        google_mirror_dir=workspace_root / "system" / "google-mirror",
        task_view_dir=workspace_telegram_root / "task-system",
        ledger_path=workspace_root / "system" / "task-ledger" / "telegram-1937792843.md",
        local_ledger_path=workspace_telegram_root / "task-system" / "TELEGRAM_LEDGER.md",
    )


class WikiJobsTests(unittest.TestCase):
    def _seed_workspace(self, paths) -> None:
        kb = paths.workspace_telegram_root / "knowledge-base"
        wiki = kb / "wiki"
        sources = kb / "sources" / "manual" / "drive-mirror"
        outputs = kb / "outputs"
        wiki.mkdir(parents=True, exist_ok=True)
        sources.mkdir(parents=True, exist_ok=True)
        outputs.mkdir(parents=True, exist_ok=True)

        (wiki / "DashoContent.md").write_text(
            """---

title: DashoContent
page_type: wiki_page
status: active
updated_at: 2026-03-01
source_count: 1
tags: [dashocontent, business, revenue, priority]
---

# Summary

Top priority business.

# Sources

- [[Drive Mirror Imports]]
""",
            encoding="utf-8",
        )
        (wiki / "Drive Mirror Imports.md").write_text(
            """---

title: Drive Mirror Imports
page_type: wiki_page
status: active
updated_at: 2026-04-05
source_count: 2
tags: [sources]
---

# Summary

Inbox for mirrored sources.

# Sources

- `knowledge-base/sources/manual/drive-mirror/2026-04-05-dashocontent-onboarding-playbook.md`
""",
            encoding="utf-8",
        )
        (wiki / "Founder OS.md").write_text(
            """---
title: Founder OS
page_type: wiki_page
status: active
updated_at: 2026-03-20
source_count: 6
tags: [operations, systems]
---

# Summary

Athena operating system page with many links.

[[DashoContent]] [[Drive Mirror Imports]] [[Athena Fleire OS]] [[Decision Log]] [[Weekly Brief]] [[Operations]]

# Sources

- internal note
""",
            encoding="utf-8",
        )
        (wiki / "Loose Note.md").write_text(
            """# Summary

Missing structure on purpose.
""",
            encoding="utf-8",
        )

        (sources / "2026-04-05-dashocontent-pricing-playbook.md").write_text(
            """---

title: DashoContent pricing playbook
imported_at: 2026-04-05
---

# Summary
Pricing and revenue design for DashoContent.

# Candidate Wiki Topics
- DashoContent
- Pricing Strategy
""",
            encoding="utf-8",
        )
        (sources / "2026-04-05-dashocontent-onboarding-playbook.md").write_text(
            """---

title: DashoContent onboarding playbook
imported_at: 2026-04-05
---

# Summary
This one is already referenced from the wiki.
""",
            encoding="utf-8",
        )
        (sources / "2026-04-05-founder-finance-note.md").write_text(
            """---

title: Founder finance note
imported_at: 2026-04-04
---

# Summary
A lower-priority finance note.

# Candidate Wiki Topics
- Pricing Strategy
- Decision Log
""",
            encoding="utf-8",
        )

        (outputs / "wiki-lint-report.md").write_text(
            """# Wiki Lint Report

- wiki_pages: 2
- source_wrappers: 3
- orphan_pages: 0
- pages_missing_frontmatter: 0
- pages_missing_sources_section: 0
- duplicate_titles: 0
- unreferenced_source_wrappers: 2
""",
            encoding="utf-8",
        )

    def test_build_promotion_queue_prioritizes_strategic_unreferenced_wrappers(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = _test_paths(Path(tmpdir))
            self._seed_workspace(paths)

            result = build_promotion_queue(paths)
            content = result.output_path.read_text(encoding="utf-8")

            self.assertEqual(result.name, "wiki-promotion-queue")
            self.assertIn("promote_now", content)
            self.assertIn("DashoContent pricing playbook", content)
            self.assertNotIn("2026-04-05-dashocontent-onboarding-playbook.md`", content)

    def test_build_stale_scan_flags_old_strategic_pages(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = _test_paths(Path(tmpdir))
            self._seed_workspace(paths)

            result = build_stale_scan(paths)
            content = result.output_path.read_text(encoding="utf-8")

            self.assertEqual(result.name, "wiki-stale-scan")
            self.assertIn("refresh_now", content)
            self.assertIn("[[DashoContent]]", content)

    def test_build_daily_digest_combines_health_queue_and_stale_views(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = _test_paths(Path(tmpdir))
            self._seed_workspace(paths)
            build_promotion_queue(paths)
            build_stale_scan(paths)

            result = build_daily_digest(paths)
            content = result.output_path.read_text(encoding="utf-8")

            self.assertEqual(result.name, "wiki-daily-digest")
            self.assertIn("Health snapshot", content)
            self.assertIn("Highest-leverage promotion candidates", content)
            self.assertIn("Pages most likely to need refresh", content)

    def test_build_index_rebuild_lists_pages_and_tags(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = _test_paths(Path(tmpdir))
            self._seed_workspace(paths)

            result = build_index_rebuild(paths)
            content = result.output_path.read_text(encoding="utf-8")

            self.assertEqual(result.name, "wiki-index-rebuild")
            self.assertIn("[[DashoContent]]", content)
            self.assertIn("priority", content)

    def test_build_backlinks_rebuild_tracks_referrers(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = _test_paths(Path(tmpdir))
            self._seed_workspace(paths)

            result = build_backlinks_rebuild(paths)
            content = result.output_path.read_text(encoding="utf-8")

            self.assertEqual(result.name, "wiki-backlinks-rebuild")
            self.assertIn("## [[DashoContent]]", content)
            self.assertIn("linked_from: [[Founder OS]]", content)

    def test_build_breakdown_flags_dense_pages(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = _test_paths(Path(tmpdir))
            self._seed_workspace(paths)

            result = build_breakdown(paths)
            content = result.output_path.read_text(encoding="utf-8")

            self.assertEqual(result.name, "wiki-breakdown")
            self.assertIn("[[Founder OS]]", content)

    def test_build_missing_pages_surfaces_candidate_topics_without_pages(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = _test_paths(Path(tmpdir))
            self._seed_workspace(paths)

            result = build_missing_pages(paths)
            content = result.output_path.read_text(encoding="utf-8")

            self.assertEqual(result.name, "wiki-missing-pages")
            self.assertIn("[[Pricing Strategy]]", content)

    def test_build_quality_audit_flags_structural_gaps(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = _test_paths(Path(tmpdir))
            self._seed_workspace(paths)

            result = build_quality_audit(paths)
            content = result.output_path.read_text(encoding="utf-8")

            self.assertEqual(result.name, "wiki-quality-audit")
            self.assertIn("[[Loose Note]]", content)
            self.assertIn("missing_frontmatter", content)


if __name__ == "__main__":
    unittest.main()
