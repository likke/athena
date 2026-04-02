# Athena

Athena is a local-first operating system for Fleire Castro.

It keeps three layers in one place:

- life context
- portfolio and project status
- execution truth
- Google-aware mirrors for Gmail, Drive, and NotebookLM exports

The app is designed to work against the same SQLite database already used by the live OpenClaw/Athena task-routing flow, so Telegram can keep using the current runtime while the board, sync jobs, and repo/project status live in a real codebase.

## What This Repo Contains

- `athena.taskctl`: DB-first task state read/write helper
- `athena.render_markdown`: generated compatibility views for Telegram bucket files
- `athena.sync`: life-doc, awareness-brief, and repo-status sync commands
- `athena.google`: local Google OAuth and Gmail / Drive / NotebookLM mirror helpers
- `athena.server`: local HTTP dashboard / board

## Default Data Paths

By default Athena reads and writes:

- DB: `~/.openclaw/workspace/system/task-ledger/tasks.sqlite`
- life docs: `~/.openclaw/workspace/life/`
- Google config: `~/.openclaw/workspace/system/google/`
- Google mirror cache: `~/.openclaw/workspace/system/google-mirror/`
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

## Google Setup

Athena's own task, project, and life state stays local and writable. Google is only an awareness and source-import layer.

1. In Google Cloud, enable the Gmail API and Google Drive API.
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

6. Edit `~/.openclaw/workspace/system/google/settings.json` and replace the placeholder folder IDs.

By default Athena now requests the `athena-google-full` profile, which includes:

- Gmail manage/compose/send
- Drive full access
- Docs
- Sheets
- Calendar
- Contacts read-only
- basic Google identity scopes

If you want a narrower setup, change the `oauth.profile` value in `settings.json`.

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

### What gets mirrored

- Gmail inbox messages into `~/.openclaw/workspace/system/google-mirror/gmail/`
- text-capable Drive files into `~/.openclaw/workspace/system/google-mirror/drive/`
- NotebookLM export files from a Drive folder into `~/.openclaw/workspace/life/notebooklm-exports/`

The important rule is: NotebookLM is not the source of truth. Athena mirrors the useful parts into local files and then ingests those into the normal `source_documents` layer.

## Goals

- one source of truth for active work
- reliable life-aware context without browser-only fragility
- portfolio-aware status across DashoContent, Thirdclips, OpenClaw, personal brand, and small projects
- a local board Athena can read and Fleire can inspect
