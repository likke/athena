# Athena Wiki Experience

Athena's wiki should feel less like a file folder and more like a founder-specific Wikipedia.

## Product intent

The wiki experience should make it easy to:
- browse concept pages
- follow links between pages
- inspect backlinks
- discover missing pages
- see structural quality and stale areas

## Current implementation direction

The local Athena server now includes a `/wiki` browse surface on top of the markdown wiki pages.

Current live local entrypoints:
- wiki home: `http://127.0.0.1:8765/wiki`
- wiki page route pattern: `http://127.0.0.1:8765/wiki/page/<slug>`
- verified implementation run: `http://127.0.0.1:8766/wiki`
- verified example page: `http://127.0.0.1:8766/wiki/page/athena-fleire-os`

Initial goals:
- wiki directory/index
- linked page view
- backlinks on each page
- readable article-like layout

Next upgrades:
- search
- category/tag pages
- infobox-style metadata panels
- recent changes view
- page quality badges
- promotion queue visibility from the wiki itself

## Standard

Athena's wiki should become a deeply linked, founder-specific, LLM-maintained knowledge system for Fleire Castro.

The browse surface should answer the practical question: "where is the live wiki URL?" without forcing someone to inspect server code or route handlers. The README and deployment docs should always point to the active wiki entrypoint.
