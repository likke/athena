# Athena Assistant Runtime

Athena is not only a local operating system and compounding wiki.

It also has an assistant/runtime layer that executes work through OpenClaw.

## Layers

1. Founder operating system
2. Compounding wiki / memory system
3. Assistant execution layer

## Assistant execution layer responsibilities

- receive work from Telegram and other OpenClaw channels
- read live task state before acting
- use local tools, browser flows, and sync jobs when needed
- persist task/chat state back to SQLite
- maintain founder-facing outputs and wiki outputs
- run safely under approval constraints
- support long-running/scheduled maintenance

## Relationship to scheduled jobs

The assistant layer triggers or explains the same jobs that cron/launchd can run automatically.

Important commands:
- `athena-sync all`
- `athena-sync weekly-brief`
- `athena-wiki all`
- `athena-board`

## Portability requirement

Athena should be deployable on another machine with the required repos, environment files, credentials, scheduler entries, and markdown/runtime configuration documented explicitly.

The private companion repo is the authoritative place for machine-transfer details, templates, and sensitive-path setup.
