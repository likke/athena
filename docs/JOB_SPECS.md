# Athena Wiki Job Specs

This document defines the first operational jobs for Athena's LLM-run wiki.

Each job should be:
- repeatable
- idempotent when possible
- observable through files/logs/reports
- safe to run on a schedule

## 1. `wiki-import-refresh`

### Purpose
Refresh source intake from approved source streams and normalize new material into source wrappers.

### Inputs
- Drive mirror files
- Gmail-derived source material
- NotebookLM exports
- transcript bundles
- manual source drops

### Outputs
- new/updated source wrappers in `knowledge-base/sources/`
- refreshed intake indexes in `knowledge-base/indexes/`
- maintenance entries in `knowledge-base/log.md`
- optional delta summary in `knowledge-base/outputs/`

### Success conditions
- new sources are discoverable
- provenance is preserved
- duplicate/version candidates are surfaced

## 2. `wiki-lint`

### Purpose
Detect structural degradation in the knowledge base.

### Checks
- orphan wiki pages
- missing frontmatter
- missing `# Sources`
- duplicate titles
- unreferenced source wrappers
- optional broken internal links

### Outputs
- `knowledge-base/outputs/wiki-lint-report.md`
- summary counts by severity

### Success conditions
- report is generated
- severe issues are visible without manual inspection

## 3. `wiki-promotion-queue`

### Purpose
Rank unpromoted source wrappers by likely value so Athena knows what to synthesize next.

### Candidate scoring inputs
- topic belongs to a priority hub
- recency
- strategic importance
- repeated mentions across sources
- duplicate/version ambiguity
- existing link opportunities
- explicit founder/business relevance

### Outputs
- `knowledge-base/outputs/wiki-promotion-queue.md`
- queue buckets:
  - `promote_now`
  - `review_soon`
  - `archive_or_reference_only`

### Success conditions
- top queue items are obviously higher-value than the median backlog item
- queue is small enough to act on daily

## 4. `wiki-promote-top`

### Purpose
Convert the highest-value queued items into durable wiki pages or hub-page expansions.

### Inputs
- promotion queue
- selected source wrappers
- existing hub pages

### Outputs
- new wiki pages
- expanded hub pages
- improved backlinks/citations
- reduced unreferenced-wrapper count

### Success conditions
- promoted pages are easier to use than the raw sources
- sources are cited
- redundancy is reduced, not increased

## 5. `wiki-stale-scan`

### Purpose
Identify important pages that should be refreshed.

### Signals
- stale `updated_at`
- newly arrived related sources
- hub-page status with no recent maintenance
- pages with high strategic importance but low recent touch

### Outputs
- `knowledge-base/outputs/wiki-stale-pages.md`
- buckets:
  - `refresh_now`
  - `refresh_this_week`
  - `monitor`

### Success conditions
- high-value pages do not quietly decay

## 6. `wiki-daily-digest`

### Purpose
Summarize what Athena learned or changed in the wiki during the day.

### Inputs
- import delta
- lint delta
- promotion delta
- stale scan results
- important changed pages

### Outputs
- `knowledge-base/outputs/wiki-daily-digest.md`
- concise founder-facing summary

### Success conditions
- a founder can understand the day’s net knowledge movement in under 2 minutes

## 7. `wiki-weekly-review`

### Purpose
Produce a weekly review of wiki health and strategic knowledge progress.

### Inputs
- daily digests
- promotion queue history
- stale page scans
- lint history
- important page changes

### Outputs
- `knowledge-base/outputs/wiki-weekly-review.md`
- weekly review section for founder briefing

### Success conditions
- strategic improvements and knowledge debt are both visible

## Scheduling guidance

Recommended cadence:
- `wiki-import-refresh`: hourly or on source-change event
- `wiki-lint`: twice daily
- `wiki-promotion-queue`: daily
- `wiki-promote-top`: daily
- `wiki-stale-scan`: twice daily
- `wiki-daily-digest`: daily
- `wiki-weekly-review`: weekly

## Guardrails

Do not let automation:
- overwrite raw sources
- remove provenance
- create duplicate wiki pages for the same concept
- silently promote low-value noise into strategic hubs
- claim synthesis quality without visible citations
