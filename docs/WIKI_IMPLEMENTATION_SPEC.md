# Athena Wiki Implementation Spec

This is the code-facing implementation contract for Athena's compounding-wiki jobs.

## Currently implemented jobs

1. `wiki-promotion-queue`
2. `wiki-stale-scan`
3. `wiki-daily-digest`
4. `wiki-index-rebuild`
5. `wiki-backlinks-rebuild`
6. `wiki-breakdown`
7. `wiki-missing-pages`
8. `wiki-quality-audit`

The goal is to make these jobs runnable now, schedulable next, and replaceable later with better heuristics without changing the outward contract.

## Entry point

CLI:
- `athena-wiki promotion-queue`
- `athena-wiki stale-scan`
- `athena-wiki daily-digest`
- `athena-wiki index-rebuild`
- `athena-wiki backlinks-rebuild`
- `athena-wiki breakdown`
- `athena-wiki missing-pages`
- `athena-wiki quality-audit`
- `athena-wiki all`

Module:
- `athena/wiki_jobs.py`

Installed script:
- `pyproject.toml` -> `athena-wiki = "athena.wiki_jobs:main"`

## Default file locations

The current implementation assumes the active knowledge base lives in the Telegram workspace:

- knowledge base root: `~/.openclaw/workspace-telegram/knowledge-base/`
- wiki pages: `~/.openclaw/workspace-telegram/knowledge-base/wiki/`
- source wrappers: `~/.openclaw/workspace-telegram/knowledge-base/sources/`
- generated outputs: `~/.openclaw/workspace-telegram/knowledge-base/outputs/`

The CLI supports overriding the workspace root:
- `athena-wiki --workspace-telegram-root /custom/path promotion-queue`

## Output files

- `knowledge-base/outputs/wiki-promotion-queue.md`
- `knowledge-base/outputs/wiki-stale-pages.md`
- `knowledge-base/outputs/wiki-daily-digest.md`
- `knowledge-base/outputs/wiki-index.md`
- `knowledge-base/outputs/wiki-backlinks.md`
- `knowledge-base/outputs/wiki-breakdown-report.md`
- `knowledge-base/outputs/wiki-missing-pages.md`
- `knowledge-base/outputs/wiki-quality-audit.md`

## Job 1: `wiki-promotion-queue`

Purpose:
- rank unreferenced source wrappers by likely strategic value

Current heuristics:
- priority hubs and keywords
- recency from `imported_at`
- duplicate/version cluster size
- candidate wiki topics in the wrapper body

## Job 2: `wiki-stale-scan`

Purpose:
- find important wiki pages likely to need refresh

Current heuristics:
- older `updated_at`
- strategic-hub titles
- priority/business/revenue tags
- newer related source wrappers
- thin sourcing

## Job 3: `wiki-daily-digest`

Purpose:
- summarize structural health plus action queues in a founder-readable daily output

Dependencies:
- `wiki-lint-report.md`
- `wiki-promotion-queue.md`
- `wiki-stale-pages.md`

## Job 4: `wiki-index-rebuild`

Purpose:
- rebuild a lightweight wiki index for discovery and navigation

## Job 5: `wiki-backlinks-rebuild`

Purpose:
- recompute backlinks between wiki pages to improve relationship traversal

## Job 6: `wiki-breakdown`

Purpose:
- flag pages likely to be too dense, too large, or too source-heavy and worth splitting

## Job 7: `wiki-missing-pages`

Purpose:
- surface likely wiki pages that should exist based on source candidate topics and links

## Job 8: `wiki-quality-audit`

Purpose:
- score wiki pages for structural quality so weak pages are visible without manual review

## Recommended orchestration order

1. `wiki-import-refresh`
2. `wiki-lint`
3. `wiki-promotion-queue`
4. `wiki-stale-scan`
5. `wiki-daily-digest`
6. `wiki-index-rebuild`
7. `wiki-backlinks-rebuild`
8. `wiki-breakdown`
9. `wiki-missing-pages`
10. `wiki-quality-audit`

## Current implementation boundaries

This version is intentionally file-based and heuristic-heavy.

It does **not yet**:
- write queue history to SQLite
- auto-promote pages
- auto-publish dashboard widgets
- run on a scheduler by itself

That is acceptable for now because the immediate goal is stable job contracts plus useful outputs.
