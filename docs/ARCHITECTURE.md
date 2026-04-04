# Athena Architecture

Athena now has two tightly linked layers:

1. **Founder Operating System**
2. **Compounding Wiki / Memory System**

They share local state, but solve different problems.

## 1. Founder Operating System

This layer handles active execution.

Core responsibilities:
- task and project state in SQLite
- founder context and weekly brief generation
- outbox and draft approval flow
- local server / board for inspection and operations
- Google-aware sync and mirroring

Representative modules:
- `athena.taskctl`
- `athena.sync`
- `athena.synthesis`
- `athena.outbox`
- `athena.google`
- `athena.server`

## 2. Compounding Wiki / Memory System

This layer handles long-term knowledge quality.

Its job is not just to store information, but to improve the structure of information over time.

Core responsibilities:
- import mirrored and captured source material
- normalize sources into wrappers with provenance
- identify duplicates, alternates, and revisions
- promote high-value material into durable wiki pages
- maintain indexes for discovery and navigation
- append operational history to a log
- lint the wiki so structure remains healthy

Representative files and scripts:
- `knowledge-base/SCHEMA.md`
- `knowledge-base/log.md`
- `knowledge-base/scripts/import_drive_mirror.py`
- `knowledge-base/scripts/lint_wiki.py`
- `knowledge-base/wiki/`
- `knowledge-base/indexes/`
- `knowledge-base/sources/`

## Shared Source of Truth

Athena is local-first.

Primary writable truth lives locally:
- SQLite for active execution state
- local files for knowledge, life context, and mirrored materials

Google, NotebookLM, email, and Drive are awareness and intake layers, not the core writable source of truth.

## Information Flow

### Execution flow

External inputs and founder decisions feed:
- tasks
- projects
- outbox items
- weekly reviews
- dashboards

### Knowledge flow

External artifacts feed:
1. raw sources
2. source wrappers
3. synthesized wiki pages
4. generated indexes
5. append-only maintenance log

## Why this is an improvement

A plain sync tool accumulates data.
A compounding system improves the usability of data.

Athena is improving when:
- context is easier to retrieve
- important knowledge is better linked
- decision pages get clearer over time
- provenance remains intact
- the wiki gets structurally healthier instead of messier

Athena is regressing when:
- sources pile up without promotion
- pages lose citations
- duplicate material multiplies without resolution
- the system becomes harder to navigate than the raw source folders

## Current state

Current work suggests **improvement, not regression**.

Why:
- the wiki now has schema, linting, indexes, and an append-only log
- orphan/frontmatter/source-section issues were driven to zero
- several Drive mirror sources were promoted into strategic DashoContent pages
- the repo docs are being updated to reflect the real architecture

What still needs work:
- many source wrappers remain unpromoted
- some duplicate/version groups still need resolution
- the repo codebase and docs are not yet fully unified around the new model

## Practical principle

Athena should become more useful as it touches more material.

If more data makes Athena noisier, it is failing.
If more data makes Athena clearer, faster, and more decision-useful, it is working.
