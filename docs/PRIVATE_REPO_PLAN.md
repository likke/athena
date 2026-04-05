# Athena Private Repo Plan

This document defines how Athena should be split between the main code repo and a private operations repo.

## Goals

- Keep `athena-os` as the canonical code and public-safe implementation repo.
- Create a private companion repo for operational architecture, setup manifests, and private markdown docs.
- Preserve the compounding wiki model while adding the missing high-value maintenance jobs identified during the audit.

## Repo Roles

### Main repo: `athena-os`

Purpose:
- installable Athena package
- tests
- wiki jobs
- public-safe documentation
- code-facing implementation specs

Keep here:
- `athena/`
- `tests/`
- `pyproject.toml`
- `README.md`
- `docs/COMPOUNDING_WIKI.md`
- `docs/WIKI_IMPLEMENTATION_SPEC.md`
- `docs/JOB_SPECS.md`
- `docs/IMPLEMENTATION_PLAN.md`
- `docs/CURRENT_STATE.md`
- public-safe architecture overview

### Private repo: `athena-private`

Purpose:
- internal architecture and operations
- setup/config JSON templates and manifests
- private markdown docs and runbooks
- local deployment and environment notes
- repo-sync manifest for controlled copying/mirroring

Recommended structure:

```text
athena-private/
  README.md
  docs/
    ARCHITECTURE.md
    OPERATIONS.md
    SETUP.md
    DATA_FLOWS.md
    RUNBOOKS/
  config/
    setup/
    manifests/
    templates/
  knowledge/
    internal/
    runbooks/
    notes/
  scripts/
  repo-sync-manifest.json
```

## Source-of-Truth Rules

### `athena-os` is canonical for
- executable code
- tests
- CLI entry points
- wiki-job contracts
- code-adjacent implementation docs

### `athena-private` is canonical for
- full operational architecture
- local environment setup
- private runbooks
- private markdown knowledge/configuration docs
- structured setup manifests/templates

### Split/sanitize rule
Files that matter to both repos should exist in two forms when needed:
- public-safe version in `athena-os`
- full operational version in `athena-private`

## Real files already present in `athena-os`

Confirmed from current repo state:
- `README.md`
- `pyproject.toml`
- `docs/ARCHITECTURE.md`
- `docs/COMPOUNDING_WIKI.md`
- `docs/WIKI_IMPLEMENTATION_SPEC.md`
- `athena/wiki_jobs.py`
- `tests/test_wiki_jobs.py`

## Classification pass

### Keep in `athena-os`
- package code under `athena/`
- tests under `tests/`
- `pyproject.toml`
- `README.md`
- `docs/COMPOUNDING_WIKI.md`
- `docs/WIKI_IMPLEMENTATION_SPEC.md`
- `docs/JOB_SPECS.md`
- `docs/IMPLEMENTATION_PLAN.md`
- `docs/CURRENT_STATE.md`

### Split/sanitize
- `docs/ARCHITECTURE.md`
  - keep a public-safe architecture overview in `athena-os`
  - place the fuller operational architecture in `athena-private/docs/ARCHITECTURE.md`
- `README.md`
  - keep product/operating overview in `athena-os`
  - private repo gets a focused internal README

### Move/copy into `athena-private`
- setup JSON templates and manifests
- local deployment/setup docs
- private runbooks
- internal operational markdowns
- repo-sync manifest

## Missing wiki-system work to include now

These came from the external audit and should be added to the implementation roadmap:

1. `wiki-index-rebuild`
2. `wiki-backlinks-rebuild`
3. `wiki-breakdown`
4. `wiki-missing-pages`
5. `wiki-quality-audit`

These should preserve the current rule that ingest and absorb remain separate stages.

## Recommended next branch

Create this branch in the current repo:
- `feature/private-repo-foundation`

## First PR scope

### In `athena-os`
Add:
- `docs/PRIVATE_REPO_PLAN.md`
- optionally `docs/REPO_BOUNDARIES.md`
- optionally a seed manifest template for private-repo sync

### In `athena-private`
Seed:
- `README.md`
- `docs/ARCHITECTURE.md`
- `docs/SETUP.md`
- `docs/OPERATIONS.md`
- `config/setup/*.template.json`
- `repo-sync-manifest.json`

## Suggested initial sync manifest

```json
{
  "version": 1,
  "sources": [
    {
      "source": "athena-os/docs/ARCHITECTURE.md",
      "target": "docs/ARCHITECTURE.md",
      "mode": "split_or_copy"
    },
    {
      "source": "athena-os/docs/COMPOUNDING_WIKI.md",
      "target": "docs/COMPOUNDING_WIKI.md",
      "mode": "copy"
    },
    {
      "source": "athena-os/docs/WIKI_IMPLEMENTATION_SPEC.md",
      "target": "docs/WIKI_IMPLEMENTATION_SPEC.md",
      "mode": "copy"
    }
  ]
}
```

## Execution order

1. create branch `feature/private-repo-foundation`
2. create private repo `athena-private`
3. seed private repo structure
4. copy the first wave of internal docs/templates
5. add the second-wave wiki jobs from the audit:
   - index rebuild
   - backlinks rebuild
   - breakdown
   - missing pages
   - quality audit
6. keep `athena-os` as canonical for executable code

## Practical principle

Athena should keep code close to code, and move sensitive operations/docs into the private companion repo.

That gives:
- clearer public repo boundaries
- safer operational documentation handling
- a better path for compounding wiki evolution without turning the code repo into a private dumping ground
