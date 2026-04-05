from __future__ import annotations

from pathlib import Path
from typing import Iterable

from .db import now_ts, query_one, slugify

TEXT_SUFFIXES = {".md", ".markdown", ".txt", ".json"}


def text_summary(content: str) -> str:
    fallback = ""
    for line in content.splitlines():
        clean = line.strip()
        if not clean or clean == "---":
            continue
        if clean.startswith("#"):
            if not fallback:
                fallback = clean.lstrip("#").strip()
            continue
        if clean.startswith("- "):
            return clean[2:].strip()
        if clean:
            return clean
    return fallback


def title_from_path(path: Path) -> str:
    return path.stem.replace("_", " ").replace("-", " ").title()


def path_variants(path: Path) -> tuple[str, ...]:
    variants = {
        str(path),
        str(path.expanduser()),
        str(path.expanduser().resolve()),
    }
    for candidate in list(variants):
        if candidate.startswith("/private/var/"):
            variants.add(candidate.removeprefix("/private"))
        elif candidate.startswith("/var/"):
            variants.add(f"/private{candidate}")
    return tuple(sorted(variants))


def choose_document_id(conn, path: Path, default_id: str) -> str:
    variants = path_variants(path)
    placeholders = ", ".join(["?"] * len(variants))
    existing = query_one(
        conn,
        f"""
        SELECT id
        FROM source_documents
        WHERE path IN ({placeholders})
        ORDER BY
          CASE source_system
            WHEN 'life-doc' THEN 0
            WHEN 'local_markdown' THEN 1
            WHEN 'NotebookLM' THEN 2
            ELSE 3
          END,
          updated_at DESC,
          created_at DESC
        LIMIT 1
        """,
        variants,
    )
    return str(existing["id"]) if existing else default_id


def upsert_source_document(
    conn,
    *,
    doc_id: str,
    kind: str,
    title: str,
    path: Path,
    source_system: str,
    is_authoritative: bool,
    summary: str,
    external_url: str | None = None,
) -> None:
    now = now_ts()
    normalized_path = path.expanduser().resolve()
    conn.execute(
        """
        INSERT INTO source_documents (id, kind, title, path, external_url, source_system, is_authoritative, last_synced_at, summary, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
          kind = excluded.kind,
          title = excluded.title,
          path = excluded.path,
          external_url = excluded.external_url,
          source_system = excluded.source_system,
          is_authoritative = excluded.is_authoritative,
          last_synced_at = excluded.last_synced_at,
          summary = excluded.summary,
          updated_at = excluded.updated_at
        """,
        (
            doc_id,
            kind,
            title,
            str(normalized_path),
            external_url,
            source_system,
            int(is_authoritative),
            now,
            summary,
            now,
            now,
        ),
    )


def dedupe_source_documents(conn, path: Path, keep_id: str) -> None:
    variants = path_variants(path)
    placeholders = ", ".join(["?"] * len(variants))
    conn.execute(
        f"DELETE FROM source_documents WHERE path IN ({placeholders}) AND id != ?",
        (*variants, keep_id),
    )


def iter_text_files(root: Path, *, suffixes: Iterable[str] = TEXT_SUFFIXES, recursive: bool = False) -> list[Path]:
    if not root.exists():
        return []
    allowed = {suffix.lower() for suffix in suffixes}
    files: list[Path] = []
    iterator = root.rglob("*") if recursive else root.iterdir()
    for entry in sorted(iterator):
        if not entry.is_file():
            continue
        if entry.suffix.lower() not in allowed:
            continue
        files.append(entry)
    return files


def default_document_id(prefix: str, path: Path) -> str:
    path_slug = slugify(path.expanduser().resolve().as_posix())
    return f"{prefix}-{path_slug}"
