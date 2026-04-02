# Athena

Athena is a local-first operating system for Fleire Castro.

It keeps three layers in one place:

- life context
- portfolio and project status
- execution truth

The app is designed to work against the same SQLite database already used by the live OpenClaw/Athena task-routing flow, so Telegram can keep using the current runtime while the board, sync jobs, and repo/project status live in a real codebase.

## What This Repo Contains

- `athena.taskctl`: DB-first task state read/write helper
- `athena.render_markdown`: generated compatibility views for Telegram bucket files
- `athena.sync`: life-doc, awareness-brief, and repo-status sync commands
- `athena.server`: local HTTP dashboard / board

## Default Data Paths

By default Athena reads and writes:

- DB: `~/.openclaw/workspace/system/task-ledger/tasks.sqlite`
- life docs: `~/.openclaw/workspace/life/`
- generated task views: `~/.openclaw/workspace-telegram/task-system/`

These can be overridden with environment variables when needed.

## Quick Start

```bash
cd /Users/fleirecastro/athena
python3 -m athena.taskctl current
python3 -m athena.sync all
python3 -m athena.server --host 127.0.0.1 --port 8765
```

Then open `http://127.0.0.1:8765`.

## Goals

- one source of truth for active work
- reliable life-aware context without browser-only fragility
- portfolio-aware status across DashoContent, Thirdclips, OpenClaw, personal brand, and small projects
- a local board Athena can read and Fleire can inspect
