# Athena

Athena is a local-first operating system, an OpenClaw-run assistant execution layer, **and** an LLM-maintained compounding wiki for Fleire Castro.

The original job was to keep three layers in one place:

- life context
- portfolio and project status
- execution truth

That is still true, but the current project is broader now:

- an assistant/runtime layer that executes work through OpenClaw and persists operational state
- portable deployment docs so Athena can be recreated on another machine with less guesswork
- a live local wiki browsing layer on top of the markdown knowledge base

- Google-aware mirrors for Gmail, Calendar, Drive, and NotebookLM exports
- Gmail draft approval and send tracking through a local outbox queue
- a continuously maintained knowledge base that turns raw source material into clearer, linked, decision-useful memory over time

The practical idea is close to Karpathy's style of a personal, evolving substrate for thought: Athena should not just store files or sync data. It should continuously improve the structure of memory by promoting important material into better-organized notes, indexes, summaries, and decision pages.

The app is designed to work against the same SQLite database already used by the live OpenClaw/Athena task-routing flow, so Telegram can keep using the current runtime while the board, sync jobs, and repo/project status live in a real codebase.

## Current Project Direction

Athena now has two tightly connected roles:

1. **operating system**
   - tasks, projects, life docs, briefs, outbox, dashboards, sync jobs
2. **compounding memory system**
   - source ingestion, provenance tracking, source wrappers, promoted wiki pages, indexes, append-only logs, and linting

The second role matters because raw accumulation is not enough. Athena should make the knowledge base more useful every week by:

- importing new source material
- preserving provenance
- detecting duplicates and version candidates
- promoting high-value source material into durable wiki pages
- maintaining indexes for navigation
- keeping an append-only operational log
- linting the wiki so structure does not silently degrade

This is already live in the current DashoContent knowledge-base work. Athena is no longer only a dashboard and sync layer; it is becoming a self-maintaining founder knowledge system.

## Core Documents

- `README.md`: high-level product and operating model
- `docs/ARCHITECTURE.md`: how the operating system layer and compounding wiki layer fit together
- `docs/COMPOUNDING_WIKI.md`: the current knowledge-system model
- `docs/CURRENT_STATE.md`: what is live now vs still incomplete
- `docs/IMPLEMENTATION_PLAN.md`: operational roadmap for the LLM-run wiki
- `docs/JOB_SPECS.md`: concrete job definitions for scheduled wiki maintenance
- `docs/WIKI_IMPLEMENTATION_SPEC.md`: code-facing contract for the first wiki jobs and scheduling hooks

## What This Repo Contains

- `athena.taskctl`: DB-first task state read/write helper
- `athena.outbox`: Gmail draft, approval, reject, and send state
- `athena.render_markdown`: generated compatibility views for Telegram bucket files
- `athena.sync`: life-doc, awareness-brief, repo-status, and mirror sync commands
- `athena.synthesis`: weekly CEO brief generation from local Athena state
- `athena.google`: local Google OAuth and Gmail / Calendar / Drive / NotebookLM helpers
- `athena.server`: local HTTP dashboard / board with batch email approvals and a briefs view
- `athena.wiki_jobs`: first runnable compounding-wiki jobs for promotion queueing, stale-page scans, and daily digests
- `knowledge-base/`: the compounding wiki layer where Athena promotes source material into durable notes
- `knowledge-base/scripts/import_drive_mirror.py`: source-wrapper generation plus wiki index/log refresh for Drive mirror imports
- `knowledge-base/scripts/lint_wiki.py`: structural validation for the wiki layer
- `knowledge-base/SCHEMA.md`: the operating contract for the knowledge system

## Default Data Paths

By default Athena reads and writes:

- DB: `~/.openclaw/workspace/system/task-ledger/tasks.sqlite`
- weekly briefs: `~/.openclaw/workspace/system/briefs/`
- life docs: `~/.openclaw/workspace/life/`
- Google config: `~/.openclaw/workspace/system/google/`
- Google mirror cache: `~/.openclaw/workspace/system/google-mirror/`
- generated task views: `~/.openclaw/workspace-telegram/task-system/`
- knowledge base workspace: `~/.openclaw/workspace-telegram/knowledge-base/`

These can be overridden with environment variables when needed.

## Quick Start

```bash
cd /Users/fleirecastro/athena
python3 -m athena.taskctl current
python3 -m athena.sync all
python3 -m athena.sync weekly-brief
python3 -m athena.server --host 127.0.0.1 --port 8765
```

Then open `http://127.0.0.1:8765`.

## Google Setup

Athena's own task, project, and life state stays local and writable. Google is only an awareness and source-import layer.

1. In Google Cloud, enable the Gmail API, Google Calendar API, and Google Drive API.
2. Create a Desktop OAuth client.
3. Save the downloaded client secrets JSON to:

```bash
~/.openclaw/workspace/system/google/client_secret.json
```

4. Create the local settings template:

```bash
cd /Users/fleirecastro/athena
python3 -m athena.google init-settings
```

5. Check the current Google auth status:

```bash
python3 -m athena.google status
```

6. Edit `~/.openclaw/workspace/system/google/settings.json` and replace the placeholder folder IDs. Keep `"include_granted_scopes": false` unless you intentionally want Google to reuse older grants from the same client.

By default Athena now requests the `athena-google-full` profile, which includes:

- Gmail manage/compose/send
- Drive full access
- Docs
- Sheets
- Calendar
- basic Google identity scopes

If you want a narrower setup, change the `oauth.profile` value in `settings.json`.

If you do not want live contacts right now, set:

```json
{
  "contacts": {
    "enabled": false
  }
}
```

The default settings template sets `"include_granted_scopes": false` so Athena asks only for the scopes you requested, instead of inheriting unrelated scopes from older consent history on the same Google OAuth client.

7. Generate the auth URL and local PKCE session:

```bash
python3 -m athena.google auth-url
```

You can also force a specific profile or additional scopes:

```bash
python3 -m athena.google auth-url --profile athena-google-full
```

8. Open the printed URL, approve access, then copy the `code=` value from the redirected browser URL.

9. Exchange that code for a local token:

```bash
python3 -m athena.google exchange-code "<PASTE_CODE_HERE>"
```

10. Re-check status:

```bash
python3 -m athena.google status
```

11. Run the Google mirror:

```bash
python3 -m athena.sync google
python3 -m athena.sync all
```

12. If you need to discover or verify Drive folder IDs later:

```bash
python3 -m athena.google list-folders --query "NotebookLM"
python3 -m athena.google list-folders --query "Athena"
```

### Gmail troubleshooting

If OAuth succeeds but Gmail requests return `403 accessNotConfigured`, the Google Cloud project behind the Desktop OAuth client has not enabled the Gmail API yet. Enable it in Google Cloud Console for that same project, then retry the sync.

### Calendar troubleshooting

If Google sync reports a `calendar_error` with `accessNotConfigured`, the Google Cloud project behind the Desktop OAuth client has not enabled the Google Calendar API yet. Enable it for that same project, wait a few minutes, then rerun the sync.

### What gets mirrored

- Gmail inbox messages into `~/.openclaw/workspace/system/google-mirror/gmail/`
- upcoming calendar agenda and events into `~/.openclaw/workspace/system/google-mirror/calendar/`
- text-capable Drive files into `~/.openclaw/workspace/system/google-mirror/drive/`
- NotebookLM export files from a Drive folder into `~/.openclaw/workspace/life/notebooklm-exports/`
- a generated `ATHENA_LIFE_CONTEXT_BUNDLE.md` into `~/.openclaw/workspace/life/notebooklm-exports/` when that folder would otherwise be empty

The important rule is: NotebookLM is not the source of truth. Athena mirrors the useful parts into local files and then ingests those into the normal `source_documents` layer.

## Compounding Wiki Model

Athena's knowledge-base is designed as an LLM-maintained compounding wiki.

Core layers:

1. **raw sources**
   - mirrored or captured artifacts that should remain immutable
2. **source wrappers**
   - normalized metadata + provenance pages for imported material
3. **wiki pages**
   - synthesized, durable topic pages meant for fast founder use
4. **indexes**
   - generated navigation and intake overviews
5. **append-only log**
   - operational history of important wiki maintenance events

Current operational rules:

- preserve provenance instead of flattening it away
- treat duplicates and revisions explicitly
- promote only the highest-value source material into durable pages
- keep pages easy to scan and source-cited
- lint the wiki regularly so structure stays healthy

This is the core of the current Athena project: not just collecting memory, but improving the shape of memory.

A simple test:

- if Athena accumulates more data but becomes noisier, it is regressing
- if Athena accumulates more data and becomes easier to use, it is improving

Right now the project direction is toward the second outcome.

## Weekly CEO Brief

Athena can generate a founder-facing weekly packet from the local life, portfolio, execution, outbox, and mirrored calendar layers.

```bash
python3 -m athena.sync weekly-brief
```

That writes:

- a versioned brief into `~/.openclaw/workspace/system/briefs/`
- a convenience copy at `LATEST_WEEKLY_CEO_BRIEF.md` in that same folder
- a `weekly_ceo_brief` source document in the SQLite DB
- a cheap global `weekly_ceo` awareness brief for chat loads

The board also exposes this at `http://127.0.0.1:8765/briefs`, and running the weekly review from the board regenerates the latest brief.

## Email Outbox

Athena now has a local `outbox_items` queue in the same SQLite database as tasks and projects.

- create Gmail drafts locally through the board or `taskctl`
- approve several queued emails at once
- send only approved drafts
- track draft ids, send state, errors, and sent timestamps

Useful commands:

```bash
python3 -m athena.taskctl queue-email --account athena --to "person@example.com" --subject "Follow-up" --body "Draft body"
python3 -m athena.taskctl approve-outbox outbox-follow-up
python3 -m athena.taskctl send-outbox
python3 -m athena.google status --account primary
```

If you configure multiple Gmail identities in `~/.openclaw/workspace/system/google/settings.json`, Athena can keep separate sender labels and optional account-specific token files. The default setup should use the real primary mailbox for auth and treat `athena@thirdteam.org` as an optional send-as identity unless it is provisioned as a true separate inbox.

## Goals

- one source of truth for active work
- reliable life-aware context without browser-only fragility
- portfolio-aware status across DashoContent, Thirdclips, OpenClaw, personal brand, and small projects
- a local board Athena can read and Fleire can inspect
