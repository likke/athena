from __future__ import annotations

import json
import re
import shutil
import subprocess
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .config import AthenaPaths, default_paths
from .google import (
    UrlLibTransport,
    _auth_headers,
    _decode_b64url,
    _gmail_attachment_names,
    _gmail_body,
    _gmail_header,
    ensure_access_token,
    load_sync_settings,
    resolve_gmail_account,
)

NOTETAKER_SENDERS = (
    ("fathom", "from:no-reply@fathom.video"),
    ("gemini", "from:gemini-notes@google.com"),
    ("read", "from:executiveassistant@e.read.ai"),
    ("fireflies", "from:no-reply@fireflies.ai"),
    ("otter", "from:notifications@otter.ai"),
    ("grain", "from:team@grain.com"),
    ("tldv", "from:hello@tldv.io"),
    ("avoma", "from:no-reply@app.avoma.com"),
)
BACKUP_SUBJECT_QUERY = '(subject:(recap) OR subject:(notes) OR subject:("meeting report") OR subject:(transcript))'


@dataclass(frozen=True)
class TranscriptTarget:
    slug: str
    name: str
    aliases: tuple[str, ...]


PRESET_TARGETS = {
    "lorna": TranscriptTarget(
        slug="lorna",
        name="Lorna",
        aliases=(
            '"lorna bondoc"',
            '"check-in: yoveo x dashocontent"',
            '"yoveo x dashocontent"',
        ),
    ),
    "penn": TranscriptTarget(
        slug="penn",
        name="Penn",
        aliases=(
            '"jynx penn"',
            '"dashocontent x penn designs"',
            '"penn designs"',
        ),
    ),
    "fortify": TranscriptTarget(
        slug="fortify",
        name="Fortify",
        aliases=(
            '"brand card presentation - fortify"',
            '"fortify"',
        ),
    ),
    "dennis prosperna": TranscriptTarget(
        slug="dennis-prosperna",
        name="Dennis Prosperna",
        aliases=(
            '"dennis prosperna"',
            '"dashocontent x rooted enterprise"',
            '"rooted enterprise"',
        ),
    ),
    "dennis-prosperna": TranscriptTarget(
        slug="dennis-prosperna",
        name="Dennis Prosperna",
        aliases=(
            '"dennis prosperna"',
            '"dashocontent x rooted enterprise"',
            '"rooted enterprise"',
        ),
    ),
    "ibarra": TranscriptTarget(
        slug="ibarra",
        name="Ibarra",
        aliases=(
            "\"dashocontent x ibarra's party venue\"",
            "\"ibarra's party venue\"",
        ),
    ),
    "july social": TranscriptTarget(
        slug="july-social",
        name="July Social",
        aliases=(
            '"the july social x dashocontent: platform introduction"',
            '"the july social"',
            '"july social"',
        ),
    ),
    "july-social": TranscriptTarget(
        slug="july-social",
        name="July Social",
        aliases=(
            '"the july social x dashocontent: platform introduction"',
            '"the july social"',
            '"july social"',
        ),
    ),
    "pam": TranscriptTarget(
        slug="pam",
        name="Pam",
        aliases=(
            '"pam bernard"',
            '"j&j beauty"',
            '"contentdash x j&j beauty"',
        ),
    ),
}


def _slugify(value: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9]+", "-", value.strip().lower()).strip("-")
    return clean or "untitled"


def _iso_now() -> str:
    return datetime.now(tz=timezone.utc).replace(microsecond=0).isoformat()


def build_targets(raw_targets: Iterable[str]) -> list[TranscriptTarget]:
    targets: list[TranscriptTarget] = []
    for raw in raw_targets:
        clean = str(raw).strip()
        if not clean:
            continue
        preset = PRESET_TARGETS.get(clean.lower())
        if preset:
            targets.append(preset)
            continue
        targets.append(
            TranscriptTarget(
                slug=_slugify(clean),
                name=clean,
                aliases=(f'"{clean}"',),
            )
        )
    if not targets:
        raise ValueError("At least one target is required.")
    return targets


def _gmail_list_messages(
    *,
    query: str,
    access_token: str,
    transport: UrlLibTransport,
    max_results: int,
) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    page_token: str | None = None
    while len(messages) < max_results:
        params: dict[str, Any] = {
            "q": query,
            "maxResults": min(100, max_results - len(messages)),
        }
        if page_token:
            params["pageToken"] = page_token
        listing = transport.request_json(
            "GET",
            "https://gmail.googleapis.com/gmail/v1/users/me/messages?" + urllib.parse.urlencode(params),
            headers=_auth_headers(access_token),
        )
        batch = listing.get("messages") or []
        messages.extend(batch)
        page_token = listing.get("nextPageToken")
        if not page_token or not batch:
            break
    return messages


def _gmail_fetch_message(
    *,
    message_id: str,
    access_token: str,
    transport: UrlLibTransport,
) -> dict[str, Any]:
    return transport.request_json(
        "GET",
        f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{urllib.parse.quote(message_id)}?format=full",
        headers=_auth_headers(access_token),
    )


def _gmail_html(payload: dict[str, Any]) -> str:
    html_chunks: list[str] = []

    def walk(part: dict[str, Any]) -> None:
        mime_type = str(part.get("mimeType") or "")
        body = part.get("body") or {}
        data = body.get("data")
        if mime_type.startswith("multipart/"):
            for child in part.get("parts") or []:
                walk(child)
            return
        if mime_type == "text/html" and data:
            text = _decode_b64url(str(data))
            if text:
                html_chunks.append(text)
        for child in part.get("parts") or []:
            walk(child)

    walk(payload)
    return "\n".join(html_chunks)


def _extract_links(body_text: str, html_text: str) -> list[str]:
    urls: list[str] = []
    for match in re.findall(r'https?://[^\s<>"\')]+', body_text):
        urls.append(match.rstrip(").,"))
    for match in re.findall(r'href=["\'](https?://[^"\']+)["\']', html_text):
        urls.append(match)

    filtered: list[str] = []
    for url in urls:
        lower = url.lower()
        if any(
            token in lower
            for token in (
                "fonts.googleapis.com",
                "fonts.gstatic.com",
                "support.google.com/accounts",
                "accounts.google.com/tos",
            )
        ):
            continue
        if url not in filtered:
            filtered.append(url)
    return filtered


def _service_name(sender: str) -> str:
    lower = sender.lower()
    if "fathom" in lower:
        return "Fathom"
    if "gemini" in lower:
        return "Gemini"
    if "read.ai" in lower or "read meeting report" in lower:
        return "Read AI"
    if "fireflies" in lower:
        return "Fireflies"
    if "otter" in lower:
        return "Otter"
    if "grain" in lower:
        return "Grain"
    if "tldv" in lower:
        return "tl;dv"
    if "avoma" in lower:
        return "Avoma"
    return "Unknown"


def _target_query_fragments(target: TranscriptTarget) -> str:
    return "(" + " OR ".join(target.aliases) + ")"


def _target_queries(target: TranscriptTarget) -> list[tuple[str, str]]:
    aliases = _target_query_fragments(target)
    queries: list[tuple[str, str]] = []
    for label, sender in NOTETAKER_SENDERS:
        queries.append((label, f"{sender} {aliases}"))
    queries.append(("subject-backup", f"{BACKUP_SUBJECT_QUERY} {aliases}"))
    return queries


def _bundle_markdown(target: TranscriptTarget, items: list[dict[str, Any]], dest_dir: Path, account_email: str) -> str:
    lines = [
        f"# {target.name}",
        "",
        f"- generated_at: {_iso_now()}",
        f"- source_account: {account_email}",
        f"- matched_messages: {len(items)}",
        f"- destination_folder: {dest_dir}",
        "",
        "Important:",
        "- These are AI notetaker emails and extracted links found through Gmail API search.",
        "- Direct email attachments were not present on these messages when fetched.",
        "- Recording and notes links are included when they were extractable from the email body/html.",
        "",
    ]

    if not items:
        lines.append("No matching messages were found.")
        return "\n".join(lines) + "\n"

    for index, item in enumerate(items, start=1):
        lines.extend(
            [
                f"## Item {index}",
                f"- service: {item['service']}",
                f"- subject: {item['subject']}",
                f"- from: {item['from']}",
                f"- to: {item['to']}",
                f"- date: {item['date']}",
                f"- message_id: {item['message_id']}",
                f"- thread_id: {item['thread_id']}",
                f"- gmail_url: {item['gmail_url']}",
            ]
        )
        if item["attachment_names"]:
            lines.append(f"- attachment_names: {', '.join(item['attachment_names'])}")
        else:
            lines.append("- attachment_names: none")
        if item["links"]:
            lines.append("- extracted_links:")
            for link in item["links"]:
                lines.append(f"  - {link}")
        else:
            lines.append("- extracted_links: none")
        lines.extend(
            [
                "",
                "### Snippet",
                item["snippet"] or "(none)",
                "",
                "### Body",
                item["body"] or "(no body text extracted)",
                "",
            ]
        )

    return "\n".join(lines).rstrip() + "\n"


def _readme_markdown(folder_name: str, manifest: dict[str, Any]) -> str:
    lines = [
        f"# {folder_name}",
        "",
        f"- generated_at: {manifest['generated_at']}",
        f"- source_account: {manifest['account_email']}",
        f"- destination: {manifest['destination_path']}",
        "- output_type: one markdown bundle per target plus this manifest",
        "",
        "## Coverage",
    ]
    for row in manifest["targets"]:
        lines.append(f"- {row['name']}: {row['matched_messages']} messages, {row['bundle_file']}")
    lines.extend(
        [
            "",
            "## Notes",
            "- These files were assembled from Gmail API results, not the managed browser profile.",
            "- Where available, Fathom/Gemini/Read AI links were extracted into each bundle.",
            "- If a direct recording file was not attached to the email, the bundle includes the email transcript/notes plus the available meeting links.",
            "- `.txt` and `.docx` copies are written beside each markdown bundle for easier phone preview in Google Drive when local conversion is available.",
            "",
        ]
    )
    return "\n".join(lines)


def _write_readable_copies(path: Path) -> dict[str, str | bool]:
    txt_path = path.with_suffix(".txt")
    txt_path.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")

    docx_path = path.with_suffix(".docx")
    textutil = shutil.which("textutil")
    if not textutil:
        return {
            "txt_path": str(txt_path),
            "docx_path": str(docx_path),
            "docx_created": False,
        }
    subprocess.run(
        [textutil, "-convert", "docx", str(txt_path), "-output", str(docx_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return {
        "txt_path": str(txt_path),
        "docx_path": str(docx_path),
        "docx_created": docx_path.exists(),
    }


def _default_destination(paths: AthenaPaths, account_email: str) -> Path:
    settings = load_sync_settings(paths)
    cloud_root = Path.home() / "Library" / "CloudStorage" / f"GoogleDrive-{account_email}" / "My Drive"
    preferred_names = [spec.name for spec in settings.drive_folders if spec.name]
    for name in preferred_names + ["Athena Drive Mirror"]:
        candidate = cloud_root / name
        if candidate.exists():
            return candidate
    fallback = paths.workspace_root / "system" / "transcript-exports"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def collect_call_transcripts(
    *,
    targets: Iterable[str],
    destination: str | Path | None = None,
    paths: AthenaPaths | None = None,
    transport: UrlLibTransport | None = None,
    account_label: str | None = None,
    max_results_per_query: int = 25,
) -> dict[str, Any]:
    resolved_paths = paths or default_paths()
    resolved_targets = build_targets(targets)
    transport = transport or UrlLibTransport()
    account = resolve_gmail_account(resolved_paths, account_label=account_label)
    access_token = ensure_access_token(resolved_paths, transport=transport, account_label=account.label)

    export_root = (
        Path(destination).expanduser().resolve()
        if destination
        else _default_destination(resolved_paths, account.email)
    )
    folder_name = f"Collected Call Transcripts - {datetime.now().strftime('%Y-%m-%d')}"
    root_dir = export_root / folder_name
    root_dir.mkdir(parents=True, exist_ok=True)

    manifest: dict[str, Any] = {
        "ok": True,
        "generated_at": _iso_now(),
        "account_label": account.label,
        "account_email": account.email,
        "destination_path": str(root_dir),
        "targets": [],
    }

    for target in resolved_targets:
        by_id: dict[str, dict[str, Any]] = {}
        query_hits: list[dict[str, Any]] = []
        for query_label, query in _target_queries(target):
            messages = _gmail_list_messages(
                query=query,
                access_token=access_token,
                transport=transport,
                max_results=max_results_per_query,
            )
            query_hits.append({"label": query_label, "query": query, "hits": len(messages)})
            for message_stub in messages:
                message_id = str(message_stub.get("id") or "").strip()
                if not message_id or message_id in by_id:
                    continue
                detail = _gmail_fetch_message(
                    message_id=message_id,
                    access_token=access_token,
                    transport=transport,
                )
                payload = detail.get("payload") or {}
                body = _gmail_body(payload)
                html = _gmail_html(payload)
                sender = _gmail_header(payload, "From")
                by_id[message_id] = {
                    "message_id": message_id,
                    "thread_id": str(detail.get("threadId") or message_stub.get("threadId") or "").strip(),
                    "subject": _gmail_header(payload, "Subject") or f"Gmail message {message_id}",
                    "from": sender,
                    "to": _gmail_header(payload, "To"),
                    "date": _gmail_header(payload, "Date"),
                    "snippet": str(detail.get("snippet") or "").strip(),
                    "body": body,
                    "attachment_names": _gmail_attachment_names(payload),
                    "gmail_url": f"https://mail.google.com/mail/u/0/#all/{str(detail.get('threadId') or message_stub.get('threadId') or message_id).strip()}",
                    "service": _service_name(sender),
                    "links": _extract_links(body, html),
                }

        items = sorted(by_id.values(), key=lambda item: (item["date"], item["subject"]))
        bundle_name = f"{target.slug}.md"
        bundle_path = root_dir / bundle_name
        bundle_path.write_text(
            _bundle_markdown(target, items, root_dir, account.email),
            encoding="utf-8",
        )
        readable = _write_readable_copies(bundle_path)
        manifest["targets"].append(
            {
                "name": target.name,
                "slug": target.slug,
                "matched_messages": len(items),
                "bundle_file": bundle_name,
                "txt_file": Path(str(readable["txt_path"])).name,
                "docx_file": Path(str(readable["docx_path"])).name if readable["docx_created"] else None,
                "queries": query_hits,
                "subjects": [item["subject"] for item in items],
            }
        )

    readme_path = root_dir / "README.md"
    readme_path.write_text(_readme_markdown(root_dir.name, manifest), encoding="utf-8")
    _write_readable_copies(readme_path)
    (root_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest
