# OpenClaw Integration

Athena's assistant layer runs through OpenClaw.

## Expected workspace-side files

In the Telegram workspace, Athena depends on a small set of default guidance files such as:
- `AGENTS.md`
- `SOUL.md`
- `IDENTITY.md`
- `USER.md`
- `MEMORY.md`
- `TOOLS.md`
- `task-system/turn-update.template.json`
- `knowledge-base/wiki/README.md`

These should be treated as part of the deployable runtime contract, not as accidental local leftovers.

## Runtime contract

OpenClaw provides:
- channel ingress/egress
- tool execution
- approval gating
- memory access
- browser access when required

Athena provides:
- founder-specific operating rules
- state persistence into SQLite
- knowledge/wiki synthesis
- local server/UI surfaces
- scheduled job orchestration and explanations
