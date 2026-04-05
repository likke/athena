# gstack x Athena x OpenClaw

## Purpose

This document records how Athena uses gstack as the methodology layer for coding work executed through OpenClaw-managed coding sessions.

Athena does not need to copy all of gstack. The important part is routing work into the right level of planning, review, QA, and release discipline.

## Current model

### 1) Simple
Use the direct coding path for:
- typos
- one-file edits
- obvious config changes

### 2) Medium
Use gstack-lite discipline for:
- multi-file changes
- feature work with an obvious approach
- refactors and skill updates

Expectations:
- read relevant files first
- write a short plan
- implement carefully
- self-review before reporting done

### 3) Heavy
Use a named gstack skill when the job is specialized:
- `/review` for code review
- `/qa` or `/qa-only` for testing a live surface
- `/cso` for security review
- `/benchmark` for performance comparisons
- `/investigate` for structured debugging
- `/ship` for release execution

### 4) Full feature flow
For meaningful feature delivery, Athena should default to:

`/office-hours` -> `/plan-eng-review` -> implementation -> `/review` -> `/qa` or `/qa-only` -> `/ship`

### 5) Plan only
If the user wants planning without implementation:

`/office-hours` -> `/autoplan`

Save the plan, report the path, and stop.

## Why Athena uses this

Athena is both:
- a founder operating system
- an execution layer that can dispatch coding work

That means the coding workflow must be:
- consistent across sessions
- explicit enough to survive delegation
- review-heavy enough to reduce sloppy AI output
- browser-aware for real QA when needed

## Browser guidance

For browser-heavy QA and verification, prefer:
- `/connect-chrome` when a visible browser context matters
- `/browse` for structured browsing inside coding flows

## Notes

- The goal is not to force gstack on trivial tasks.
- The goal is to standardize the methodology for non-trivial work.
- Athena should preserve repo-specific instructions and append methodology, not overwrite local rules.
