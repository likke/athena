# Athena Operational Improvement Plan

This plan turns Athena's compounding wiki direction into concrete jobs, scheduled tasks, and implementation milestones.

## Objective

Make Athena operate as an LLM-run wiki that:
- ingests source material reliably
- preserves provenance
- maintains structural health automatically
- promotes important knowledge into durable pages
- surfaces what changed and what needs attention

## Design principle

Athena should do as much maintenance as possible automatically, and only ask the founder for decisions that are actually strategic, ambiguous, or external.

---

## 1. Core job types

### A. Intake jobs
These bring new material into Athena.

Examples:
- Gmail mirror intake
- Drive mirror intake
- NotebookLM export intake
- transcript bundle intake
- manual source drop intake

Outputs:
- raw mirrored artifacts
- normalized source wrappers
- updated intake indexes
- import events appended to the maintenance log

### B. Maintenance jobs
These keep the wiki healthy.

Examples:
- wiki lint
- orphan page detection
- missing-sources detection
- duplicate/version candidate detection
- stale page detection
- unreferenced source wrapper reporting

Outputs:
- lint reports
- health reports
- prioritized cleanup queues

### C. Promotion jobs
These convert valuable source material into durable wiki pages.

Examples:
- promote top unreferenced source wrappers
- expand high-value hub pages
- merge overlapping pages
- add citations and backlinks
- summarize version clusters into canonical pages

Outputs:
- new or improved wiki pages
- reduced unreferenced wrapper count
- stronger strategic hubs

### D. Synthesis jobs
These create founder-facing views.

Examples:
- weekly CEO brief
- project status summary
- new-knowledge digest
- strategic changes digest
- decision log refresh

Outputs:
- weekly brief
- concise updates
- priorities and risks surfaced from the wiki

### E. Governance jobs
These prevent silent drift.

Examples:
- schema conformance checks
- source provenance checks
- protected-file policy checks
- generation-diff review before publish

Outputs:
- warnings
- blocked publish actions when rules fail

---

## 2. Proposed scheduled tasks

### Every hour
1. ingest new lightweight sources when available
   - Gmail mirror delta
   - Drive mirror delta
   - NotebookLM export delta
2. append intake events to log
3. refresh intake indexes

### Twice daily
1. run wiki lint
2. run duplicate/version candidate scan
3. compute unreferenced source wrapper queue
4. compute stale strategic pages queue

### Daily
1. promotion pass on top 3-10 high-value source wrappers
2. refresh wiki index
3. write a short daily knowledge delta summary
4. mark pages that need human review

### Weekly
1. regenerate weekly CEO brief
2. generate strategic knowledge digest:
   - what changed
   - what was promoted
   - what remains messy
3. review duplicate/version clusters
4. review top project hubs for freshness

### Monthly
1. architecture review of the wiki layer
2. archive or consolidate low-value noise
3. refresh canonical hub pages
4. update repo docs if the real system changed materially

---

## 3. Current implementation priorities

### Priority 1 — Formalize the maintenance loop
Needed now:
- add scheduled execution entry points for:
  - import jobs
  - lint jobs
  - promotion queue jobs
  - digest jobs
- define outputs and file locations for each job
- ensure every run is idempotent where possible

### Priority 2 — Build a promotion queue
Athena needs a simple scoring model for source wrappers.

Candidate inputs:
- business relevance
- founder relevance
- recency
- link density
- duplicate/version ambiguity
- whether the source belongs to a top-priority hub like DashoContent

Outputs:
- `high_priority`
- `review_soon`
- `archive_only`

### Priority 3 — Add stale-page detection
Strategic pages should not silently age out.

Signals:
- old `updated_at`
- high traffic/reference count but stale content
- new related sources arrived after last update

### Priority 4 — Add canonical-page enforcement
Some topics should converge into durable hubs rather than fragment.

Examples:
- DashoContent
- Brand Compliance Scoring MVP
- Funding
- founder operating system
- health and recovery

### Priority 5 — Founder-facing summaries
Athena should explain change without forcing manual inspection.

Needed summaries:
- what Athena learned today
- what got promoted
- what needs a decision
- where the wiki is degrading

---

## 4. Concrete jobs to implement next

### Job 1: `wiki-import-refresh`
Purpose:
- refresh Drive mirror imports and source wrappers
- update intake indexes and maintenance log

### Job 2: `wiki-lint`
Purpose:
- run structural health checks
- emit a report and severity summary

### Job 3: `wiki-promotion-queue`
Purpose:
- score unreferenced source wrappers
- produce a ranked promotion queue

### Job 4: `wiki-promote-top`
Purpose:
- promote the top N queue items into wiki pages or hub-page expansions
- reduce wrapper backlog

### Job 5: `wiki-stale-scan`
Purpose:
- identify important pages that should be refreshed

### Job 6: `wiki-daily-digest`
Purpose:
- summarize net wiki changes, new knowledge, and risk areas

### Job 7: `wiki-weekly-review`
Purpose:
- combine health, promotion, backlog, and strategic changes into a weekly review packet

---

## 5. Suggested first implementation sequence

### Phase 1 — stabilize
- document architecture and current state
- define job names and outputs
- make lint and import jobs repeatable
- standardize log entries

### Phase 2 — operationalize
- build promotion queue generator
- build stale-page scanner
- build daily digest
- build weekly wiki review

### Phase 3 — automate
- attach schedules
- reduce manual prompting
- let Athena maintain the wiki by default and escalate only when judgment is needed

### Phase 4 — founder-facing productization
- expose wiki health and promotion queue in the local board
- surface digests in Telegram or brief packets
- make strategic hub freshness visible

---

## 6. Immediate next steps

1. define the job specs in code-facing form
2. implement `wiki-promotion-queue`
3. implement `wiki-stale-scan`
4. implement `wiki-daily-digest`
5. wire schedules for import + lint + queue + digest
6. run the next promotion batch focused on DashoContent revenue/GTM
7. add a founder-visible health summary to the board

## Bottom line

Athena is improving when source growth causes better structure.

The next operational milestone is to make that improvement loop scheduled, measurable, and mostly automatic.
