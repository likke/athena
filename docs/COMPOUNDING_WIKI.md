# Athena Compounding Wiki

Athena's current knowledge-system direction is to behave less like a passive note archive and more like an LLM-maintained compounding wiki.

## Why this exists

A founder accumulates too much raw material for a flat note store to remain useful:

- emails
- meeting notes
- transcripts
- Drive research
- NotebookLM exports
- product strategy documents
- operating docs

If those artifacts only pile up, recall degrades. Athena's job is to continuously convert that pile into a more useful memory system.

## Core model

### 1. Raw sources
Immutable artifacts and mirrored files.

Examples:
- Gmail-derived notes
- Drive mirror files
- transcript exports
- NotebookLM exports

### 2. Source wrappers
Normalized source pages that preserve:
- title
- path
- import timestamp
- provenance
- duplicate/version clues
- lightweight synthesis

### 3. Wiki pages
Durable pages meant for actual operating use.

Examples:
- `DashoContent`
- `Brand Compliance Scoring MVP`
- `Funding`
- `DashoContent Onboarding`
- `DashoContent Security and Trust`

### 4. Indexes
Generated navigation artifacts such as:
- wiki index
- manual ingestion index
- source stream indexes

### 5. Append-only log
A maintenance history of what changed and why.

### 6. Linting
Structural checks that prevent silent wiki decay.

## Working rules

- raw sources are preserved, not overwritten
- provenance is mandatory
- duplicates and revisions are surfaced explicitly
- not every source deserves promotion
- promoted pages should become clearer and more connected over time
- source citations should remain easy to verify

## Current proof point

The current DashoContent knowledge-base work already demonstrates the model:

- Drive Mirror imports normalized into source wrappers
- a formal schema for the wiki layer
- generated indexes
- append-only log
- wiki linting
- source promotion into strategic pages

This is the beginning of Athena as a founder-grade memory substrate, not just a sync tool.
