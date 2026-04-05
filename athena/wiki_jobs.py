from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable

try:
    from .config import AthenaPaths, default_paths
except ImportError:  # pragma: no cover - supports direct script execution
    import sys

    PACKAGE_ROOT = Path(__file__).resolve().parents[1]
    if str(PACKAGE_ROOT) not in sys.path:
        sys.path.insert(0, str(PACKAGE_ROOT))
    from athena.config import AthenaPaths, default_paths


DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
LINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
PRIORITY_HUBS = {
    "DashoContent": 30,
    "Brand Compliance Scoring MVP": 25,
    "Funding": 16,
    "Athena Fleire OS": 16,
    "Health and Recovery": 14,
    "Family": 14,
    "Unfireable": 10,
    "The Wicked": 10,
}
PRIORITY_KEYWORDS = {
    "dasho": 12,
    "brand compliance": 12,
    "pricing": 10,
    "revenue": 10,
    "outbound": 9,
    "waitlist": 9,
    "security": 8,
    "trust": 8,
    "fund": 8,
    "grant": 8,
    "founder": 7,
    "product": 7,
    "market": 6,
    "sales": 6,
    "onboarding": 6,
    "white-label": 6,
    "compliance": 6,
}
STALE_HUB_BONUS = {
    "DashoContent": 30,
    "Brand Compliance Scoring MVP": 24,
    "Funding": 14,
    "Athena Fleire OS": 12,
    "Health and Recovery": 10,
    "Family": 10,
}


@dataclass
class SourceWrapper:
    path: Path
    rel_path: str
    title: str
    frontmatter: dict[str, object]
    body: str


@dataclass
class WikiPage:
    path: Path
    rel_path: str
    title: str
    frontmatter: dict[str, object]
    body: str


@dataclass
class PromotionCandidate:
    wrapper: SourceWrapper
    score: int
    reasons: list[str]
    suggested_targets: list[str]
    bucket: str


@dataclass
class StalePageCandidate:
    page: WikiPage
    score: int
    age_days: int | None
    new_sources_count: int
    reasons: list[str]
    bucket: str


@dataclass
class JobResult:
    name: str
    output_path: Path
    summary: list[str]


@dataclass
class MissingPageCandidate:
    title: str
    mentions: int
    reasons: list[str]


@dataclass
class QualityAuditRow:
    page: WikiPage
    score: int
    findings: list[str]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Athena compounding-wiki jobs.")
    parser.add_argument("--workspace-telegram-root", help="Override workspace-telegram root.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("promotion-queue", help="Build a ranked queue of source wrappers to promote.")
    subparsers.add_parser("stale-scan", help="Find stale or under-maintained strategic wiki pages.")
    subparsers.add_parser("daily-digest", help="Write a daily founder-facing knowledge delta digest.")
    subparsers.add_parser("index-rebuild", help="Rebuild a lightweight wiki index for discovery.")
    subparsers.add_parser("backlinks-rebuild", help="Recompute backlinks between wiki pages.")
    subparsers.add_parser("breakdown", help="Flag wiki pages that are likely too bloated or overloaded.")
    subparsers.add_parser("missing-pages", help="Identify likely wiki pages that should exist but do not yet.")
    subparsers.add_parser("quality-audit", help="Audit wiki pages for structural quality issues.")
    subparsers.add_parser("all", help="Run promotion queue, stale scan, and daily digest in sequence.")
    return parser.parse_args()


def resolve_paths(args: argparse.Namespace) -> AthenaPaths:
    paths = default_paths()
    if args.workspace_telegram_root:
        root = Path(args.workspace_telegram_root).expanduser().resolve()
        return AthenaPaths(
            repo_root=paths.repo_root,
            openclaw_root=paths.openclaw_root,
            workspace_root=paths.workspace_root,
            workspace_telegram_root=root,
            db_path=paths.db_path,
            briefs_dir=paths.briefs_dir,
            life_dir=paths.life_dir,
            notebooklm_export_dir=paths.notebooklm_export_dir,
            google_dir=paths.google_dir,
            google_settings_path=paths.google_settings_path,
            google_client_secrets_path=paths.google_client_secrets_path,
            google_token_path=paths.google_token_path,
            google_mirror_dir=paths.google_mirror_dir,
            task_view_dir=paths.task_view_dir,
            ledger_path=paths.ledger_path,
            local_ledger_path=paths.local_ledger_path,
            schema_path=paths.schema_path,
        )
    return paths


def kb_dir(paths: AthenaPaths) -> Path:
    return paths.workspace_telegram_root / "knowledge-base"


def wiki_dir(paths: AthenaPaths) -> Path:
    return kb_dir(paths) / "wiki"


def source_dir(paths: AthenaPaths) -> Path:
    return kb_dir(paths) / "sources"


def outputs_dir(paths: AthenaPaths) -> Path:
    path = kb_dir(paths) / "outputs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_simple_yaml_value(raw: str):
    raw = raw.strip()
    if not raw:
        return ""
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1].strip()
        if not inner:
            return []
        return [item.strip().strip('"\'') for item in inner.split(",") if item.strip()]
    if re.fullmatch(r"\d+", raw):
        return int(raw)
    return raw.strip('"\'')


def parse_frontmatter(text: str) -> tuple[dict[str, object], str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}, text
    raw_frontmatter = text[4:end]
    body = text[end + 5 :]
    data: dict[str, object] = {}
    for line in raw_frontmatter.splitlines():
        if not line.strip() or line.lstrip().startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        data[key.strip()] = parse_simple_yaml_value(value)
    return data, body


def load_wiki_pages(paths: AthenaPaths) -> list[WikiPage]:
    pages: list[WikiPage] = []
    for path in sorted(wiki_dir(paths).glob("*.md")):
        text = path.read_text(encoding="utf-8")
        frontmatter, body = parse_frontmatter(text)
        rel = path.relative_to(kb_dir(paths)).as_posix()
        pages.append(WikiPage(path=path, rel_path=rel, title=str(frontmatter.get("title") or path.stem), frontmatter=frontmatter, body=body))
    return pages


def load_source_wrappers(paths: AthenaPaths) -> list[SourceWrapper]:
    wrappers: list[SourceWrapper] = []
    for path in sorted(source_dir(paths).rglob("*.md")):
        text = path.read_text(encoding="utf-8")
        frontmatter, body = parse_frontmatter(text)
        rel = path.relative_to(kb_dir(paths)).as_posix()
        wrappers.append(SourceWrapper(path=path, rel_path=rel, title=str(frontmatter.get("title") or path.stem), frontmatter=frontmatter, body=body))
    return wrappers


def wiki_text(pages: Iterable[WikiPage]) -> str:
    return "\n".join(f"{page.rel_path}\n{page.path.name}\n{page.title}\n{page.body}" for page in pages)


def links_in(text: str) -> set[str]:
    return {match.strip() for match in LINK_RE.findall(text)}


def parse_iso_date(value: object) -> date | None:
    if not isinstance(value, str) or not value:
        return None
    match = DATE_RE.search(value)
    if not match:
        return None
    try:
        return date.fromisoformat(match.group(0))
    except ValueError:
        return None


def days_since(value: object) -> int | None:
    parsed = parse_iso_date(value)
    if parsed is None:
        return None
    return (datetime.now(timezone.utc).date() - parsed).days


def title_key(value: str) -> str:
    return re.sub(r"\s+", " ", value.casefold()).strip()


def body_candidate_topics(wrapper: SourceWrapper) -> list[str]:
    topics: list[str] = []
    capture = False
    for line in wrapper.body.splitlines():
        stripped = line.strip()
        if stripped == "# Candidate Wiki Topics":
            capture = True
            continue
        if capture and stripped.startswith("# "):
            break
        if capture and stripped.startswith("- "):
            topics.append(stripped[2:].strip())
    return [topic for topic in topics if topic]


def source_is_referenced(wrapper: SourceWrapper, all_wiki_text: str) -> bool:
    return wrapper.rel_path in all_wiki_text or wrapper.path.name in all_wiki_text


def source_duplicate_factor(wrapper: SourceWrapper, duplicates: dict[str, int]) -> int:
    return max(0, duplicates.get(title_key(wrapper.title), 1) - 1)


def source_priority_score(wrapper: SourceWrapper, duplicates: dict[str, int]) -> PromotionCandidate | None:
    text = f"{wrapper.title}\n{wrapper.rel_path}\n{wrapper.body}".casefold()
    score = 0
    reasons: list[str] = []

    for hub, bonus in PRIORITY_HUBS.items():
        if hub.casefold() in text:
            score += bonus
            reasons.append(f"matches priority hub: {hub}")

    for keyword, bonus in PRIORITY_KEYWORDS.items():
        if keyword in text:
            score += bonus
            reasons.append(f"keyword: {keyword}")

    imported_age = days_since(wrapper.frontmatter.get("imported_at"))
    if imported_age is not None:
        if imported_age <= 3:
            score += 8
            reasons.append("recent import")
        elif imported_age <= 14:
            score += 4
            reasons.append("still fresh")

    duplicate_factor = source_duplicate_factor(wrapper, duplicates)
    if duplicate_factor:
        score += min(duplicate_factor * 5, 15)
        reasons.append(f"duplicate/version cluster x{duplicate_factor + 1}")

    suggested_targets = body_candidate_topics(wrapper)
    target_hits = [topic for topic in suggested_targets if topic in PRIORITY_HUBS]
    if target_hits:
        score += 8
        reasons.append("has canonical target page")

    if score >= 45:
        bucket = "promote_now"
    elif score >= 25:
        bucket = "review_soon"
    else:
        bucket = "archive_or_reference_only"

    return PromotionCandidate(
        wrapper=wrapper,
        score=score,
        reasons=dedupe_list(reasons),
        suggested_targets=suggested_targets,
        bucket=bucket,
    )


def dedupe_list(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered


def build_promotion_queue(paths: AthenaPaths) -> JobResult:
    pages = load_wiki_pages(paths)
    wrappers = load_source_wrappers(paths)
    combined = wiki_text(pages)

    duplicates: dict[str, int] = {}
    for wrapper in wrappers:
        key = title_key(wrapper.title)
        duplicates[key] = duplicates.get(key, 0) + 1

    candidates: list[PromotionCandidate] = []
    for wrapper in wrappers:
        if source_is_referenced(wrapper, combined):
            continue
        candidates.append(source_priority_score(wrapper, duplicates))

    candidates = sorted(candidates, key=lambda item: (-item.score, item.wrapper.title.casefold()))
    promote_now = [item for item in candidates if item.bucket == "promote_now"]
    review_soon = [item for item in candidates if item.bucket == "review_soon"]
    archive_only = [item for item in candidates if item.bucket == "archive_or_reference_only"]

    output_path = outputs_dir(paths) / "wiki-promotion-queue.md"
    lines = [
        "# Wiki Promotion Queue",
        "",
        f"Generated: {datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')}",
        "",
        "## Summary",
        "",
        f"- total_candidates: {len(candidates)}",
        f"- promote_now: {len(promote_now)}",
        f"- review_soon: {len(review_soon)}",
        f"- archive_or_reference_only: {len(archive_only)}",
        "",
        "## Promotion guidance",
        "",
        "- Prefer linking into existing hubs before creating a new top-level wiki page.",
        "- Resolve obvious duplicate/version clusters while promoting.",
        "- Favor DashoContent, revenue, pricing, outbound, waitlist, IP, funding, and founder-operating-system material first.",
        "",
    ]

    for heading, bucket_items in (
        ("promote_now", promote_now),
        ("review_soon", review_soon),
        ("archive_or_reference_only", archive_only),
    ):
        lines.extend([f"## {heading}", ""])
        if not bucket_items:
            lines.append("- None")
            lines.append("")
            continue
        for item in bucket_items:
            targets = ", ".join(item.suggested_targets[:4]) if item.suggested_targets else "(none yet)"
            reasons = "; ".join(item.reasons[:5]) if item.reasons else "heuristic backlog item"
            lines.append(f"- score: {item.score} | `{item.wrapper.rel_path}`")
            lines.append(f"  - title: {item.wrapper.title}")
            lines.append(f"  - suggested_targets: {targets}")
            lines.append(f"  - reasons: {reasons}")
        lines.append("")

    output_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return JobResult(
        name="wiki-promotion-queue",
        output_path=output_path,
        summary=[
            f"total_candidates={len(candidates)}",
            f"promote_now={len(promote_now)}",
            f"review_soon={len(review_soon)}",
        ],
    )


def count_recent_related_sources(page: WikiPage, wrappers: list[SourceWrapper]) -> int:
    title_cf = page.title.casefold()
    count = 0
    page_updated = parse_iso_date(page.frontmatter.get("updated_at"))
    for wrapper in wrappers:
        imported = parse_iso_date(wrapper.frontmatter.get("imported_at"))
        if page_updated and imported and imported <= page_updated:
            continue
        text = f"{wrapper.title}\n{wrapper.body}".casefold()
        if title_cf in text:
            count += 1
            continue
        if any(link.casefold() == title_cf for link in links_in(wrapper.body)):
            count += 1
    return count


def stale_score(page: WikiPage, wrappers: list[SourceWrapper]) -> StalePageCandidate:
    age_days = days_since(page.frontmatter.get("updated_at"))
    score = 0
    reasons: list[str] = []

    if age_days is None:
        score += 20
        reasons.append("missing updated_at")
    else:
        if age_days >= 30:
            score += 25
            reasons.append(f"stale: {age_days}d since update")
        elif age_days >= 14:
            score += 15
            reasons.append(f"aging: {age_days}d since update")
        elif age_days >= 7:
            score += 8
            reasons.append(f"cooling: {age_days}d since update")

    if page.title in STALE_HUB_BONUS:
        score += STALE_HUB_BONUS[page.title]
        reasons.append(f"strategic hub: {page.title}")

    tags = page.frontmatter.get("tags")
    if isinstance(tags, list):
        if any(str(tag).casefold() in {"priority", "business", "revenue"} for tag in tags):
            score += 10
            reasons.append("priority-tagged page")

    related_new_sources = count_recent_related_sources(page, wrappers)
    if related_new_sources:
        score += min(related_new_sources * 8, 24)
        reasons.append(f"{related_new_sources} newer related source(s)")

    source_count = page.frontmatter.get("source_count")
    if isinstance(source_count, int) and source_count <= 1:
        score += 6
        reasons.append("thin sourcing")

    if score >= 55:
        bucket = "refresh_now"
    elif score >= 28:
        bucket = "refresh_this_week"
    else:
        bucket = "monitor"

    return StalePageCandidate(
        page=page,
        score=score,
        age_days=age_days,
        new_sources_count=related_new_sources,
        reasons=dedupe_list(reasons),
        bucket=bucket,
    )


def build_stale_scan(paths: AthenaPaths) -> JobResult:
    pages = load_wiki_pages(paths)
    wrappers = load_source_wrappers(paths)
    candidates = sorted((stale_score(page, wrappers) for page in pages), key=lambda item: (-item.score, item.page.title.casefold()))
    refresh_now = [item for item in candidates if item.bucket == "refresh_now"]
    refresh_week = [item for item in candidates if item.bucket == "refresh_this_week"]
    monitor = [item for item in candidates if item.bucket == "monitor"]

    output_path = outputs_dir(paths) / "wiki-stale-pages.md"
    lines = [
        "# Wiki Stale Page Scan",
        "",
        f"Generated: {datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')}",
        "",
        "## Summary",
        "",
        f"- total_pages_scanned: {len(candidates)}",
        f"- refresh_now: {len(refresh_now)}",
        f"- refresh_this_week: {len(refresh_week)}",
        f"- monitor: {len(monitor)}",
        "",
    ]

    for heading, bucket_items in (("refresh_now", refresh_now), ("refresh_this_week", refresh_week), ("monitor", monitor)):
        lines.extend([f"## {heading}", ""])
        if not bucket_items:
            lines.append("- None")
            lines.append("")
            continue
        for item in bucket_items:
            age_text = f"{item.age_days}d" if item.age_days is not None else "unknown"
            reasons = "; ".join(item.reasons[:5]) if item.reasons else "heuristic watch item"
            lines.append(f"- score: {item.score} | [[{item.page.title}]]")
            lines.append(f"  - path: `{item.page.rel_path}`")
            lines.append(f"  - age: {age_text}")
            lines.append(f"  - newer_related_sources: {item.new_sources_count}")
            lines.append(f"  - reasons: {reasons}")
        lines.append("")

    output_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return JobResult(
        name="wiki-stale-scan",
        output_path=output_path,
        summary=[
            f"total_pages_scanned={len(candidates)}",
            f"refresh_now={len(refresh_now)}",
            f"refresh_this_week={len(refresh_week)}",
        ],
    )


def read_report_counts(path: Path) -> dict[str, int]:
    if not path.exists():
        return {}
    counts: dict[str, int] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped.startswith("-") or ":" not in stripped:
            continue
        name, value = stripped[1:].split(":", 1)
        name = name.strip()
        value = value.strip()
        if re.fullmatch(r"\d+", value):
            counts[name] = int(value)
    return counts


def top_queue_items(queue_path: Path, limit: int = 5) -> list[str]:
    if not queue_path.exists():
        return []
    items: list[str] = []
    for line in queue_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("- score:") and "`" in stripped:
            items.append(stripped)
        if len(items) >= limit:
            break
    return items


def top_stale_items(stale_path: Path, limit: int = 5) -> list[str]:
    if not stale_path.exists():
        return []
    items: list[str] = []
    for line in stale_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("- score:") and "[[" in stripped:
            items.append(stripped)
        if len(items) >= limit:
            break
    return items


def build_daily_digest(paths: AthenaPaths) -> JobResult:
    lint_counts = read_report_counts(outputs_dir(paths) / "wiki-lint-report.md")
    queue_counts = read_report_counts(outputs_dir(paths) / "wiki-promotion-queue.md")
    stale_counts = read_report_counts(outputs_dir(paths) / "wiki-stale-pages.md")
    queue_preview = top_queue_items(outputs_dir(paths) / "wiki-promotion-queue.md")
    stale_preview = top_stale_items(outputs_dir(paths) / "wiki-stale-pages.md")

    output_path = outputs_dir(paths) / "wiki-daily-digest.md"
    lines = [
        "# Wiki Daily Digest",
        "",
        f"Generated: {datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')}",
        "",
        "## Health snapshot",
        "",
        f"- wiki_pages: {lint_counts.get('wiki_pages', 'unknown')}",
        f"- source_wrappers: {lint_counts.get('source_wrappers', 'unknown')}",
        f"- unreferenced_source_wrappers: {lint_counts.get('unreferenced_source_wrappers', 'unknown')}",
        f"- orphan_pages: {lint_counts.get('orphan_pages', 'unknown')}",
        f"- pages_missing_frontmatter: {lint_counts.get('pages_missing_frontmatter', 'unknown')}",
        f"- pages_missing_sources_section: {lint_counts.get('pages_missing_sources_section', 'unknown')}",
        "",
        "## Action queue",
        "",
        f"- promote_now: {queue_counts.get('promote_now', 'unknown')}",
        f"- review_soon: {queue_counts.get('review_soon', 'unknown')}",
        f"- refresh_now: {stale_counts.get('refresh_now', 'unknown')}",
        f"- refresh_this_week: {stale_counts.get('refresh_this_week', 'unknown')}",
        "",
        "## Highest-leverage promotion candidates",
        "",
    ]
    lines.extend(queue_preview or ["- No queue preview available yet"]) 
    lines.extend(["", "## Pages most likely to need refresh", ""])
    lines.extend(stale_preview or ["- No stale-page preview available yet"])
    lines.extend([
        "",
        "## Interpretation",
        "",
        "- Promotion work should focus on high-value unreferenced wrappers that strengthen existing canonical hubs first.",
        "- Stale work should focus on strategic pages whose last update lags behind incoming source material.",
        "- A good day reduces wrapper backlog, refreshes hub pages, or improves wiki health without increasing structural debt.",
    ])

    output_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return JobResult(
        name="wiki-daily-digest",
        output_path=output_path,
        summary=[
            f"promote_now={queue_counts.get('promote_now', 'unknown')}",
            f"refresh_now={stale_counts.get('refresh_now', 'unknown')}",
            f"unreferenced_source_wrappers={lint_counts.get('unreferenced_source_wrappers', 'unknown')}",
        ],
    )


def existing_titles_set(pages: Iterable[WikiPage], wrappers: Iterable[SourceWrapper] = ()) -> set[str]:
    titles = {title_key(page.title) for page in pages}
    titles.update(title_key(wrapper.title) for wrapper in wrappers)
    return titles


def build_index_rebuild(paths: AthenaPaths) -> JobResult:
    pages = load_wiki_pages(paths)
    output_path = outputs_dir(paths) / "wiki-index.md"
    tag_map: dict[str, list[str]] = {}
    for page in pages:
        tags = page.frontmatter.get("tags")
        if not isinstance(tags, list):
            continue
        for tag in tags:
            tag_map.setdefault(str(tag), []).append(page.title)

    lines = [
        "# Wiki Index",
        "",
        f"Generated: {datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')}",
        "",
        "## Pages",
        "",
    ]
    for page in sorted(pages, key=lambda item: item.title.casefold()):
        lines.append(f"- [[{page.title}]] — `{page.rel_path}`")

    lines.extend(["", "## Tags", ""])
    if not tag_map:
        lines.append("- No tags found")
    else:
        for tag in sorted(tag_map, key=str.casefold):
            titles = ", ".join(f"[[{title}]]" for title in sorted(set(tag_map[tag]), key=str.casefold))
            lines.append(f"- {tag}: {titles}")

    output_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return JobResult(
        name="wiki-index-rebuild",
        output_path=output_path,
        summary=[f"pages_indexed={len(pages)}", f"tags_indexed={len(tag_map)}"],
    )


def build_backlinks_rebuild(paths: AthenaPaths) -> JobResult:
    pages = load_wiki_pages(paths)
    title_lookup = {title_key(page.title): page for page in pages}
    backlinks: dict[str, list[str]] = {page.title: [] for page in pages}
    for page in pages:
        for link in links_in(page.body):
            target = title_lookup.get(title_key(link))
            if target is None or target.title == page.title:
                continue
            backlinks[target.title].append(page.title)

    output_path = outputs_dir(paths) / "wiki-backlinks.md"
    lines = [
        "# Wiki Backlinks",
        "",
        f"Generated: {datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')}",
        "",
    ]
    for page in sorted(pages, key=lambda item: item.title.casefold()):
        refs = sorted(set(backlinks[page.title]), key=str.casefold)
        lines.extend([f"## [[{page.title}]]", ""])
        if refs:
            for ref in refs:
                lines.append(f"- linked_from: [[{ref}]]")
        else:
            lines.append("- linked_from: none")
        lines.append("")

    output_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    linked_pages = sum(1 for refs in backlinks.values() if refs)
    return JobResult(
        name="wiki-backlinks-rebuild",
        output_path=output_path,
        summary=[f"pages_scanned={len(pages)}", f"pages_with_backlinks={linked_pages}"],
    )


def build_breakdown(paths: AthenaPaths) -> JobResult:
    pages = load_wiki_pages(paths)
    output_path = outputs_dir(paths) / "wiki-breakdown-report.md"
    candidates: list[tuple[WikiPage, list[str], int]] = []
    for page in pages:
        findings: list[str] = []
        score = 0
        body_length = len(page.body)
        if body_length >= 2500:
            findings.append(f"long_body:{body_length}")
            score += 20
        elif body_length >= 1200:
            findings.append(f"growing_body:{body_length}")
            score += 10

        link_count = len(links_in(page.body))
        if link_count >= 8:
            findings.append(f"dense_links:{link_count}")
            score += 15
        elif link_count >= 4:
            findings.append(f"moderate_links:{link_count}")
            score += 8

        source_count = page.frontmatter.get("source_count")
        if isinstance(source_count, int) and source_count >= 8:
            findings.append(f"many_sources:{source_count}")
            score += 15
        elif isinstance(source_count, int) and source_count >= 4:
            findings.append(f"growing_sources:{source_count}")
            score += 8

        if findings:
            candidates.append((page, findings, score))

    candidates.sort(key=lambda item: (-item[2], item[0].title.casefold()))
    lines = [
        "# Wiki Breakdown Report",
        "",
        f"Generated: {datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')}",
        "",
        "## Pages likely to benefit from splitting or restructuring",
        "",
    ]
    if not candidates:
        lines.append("- None")
    else:
        for page, findings, score in candidates:
            lines.append(f"- score: {score} | [[{page.title}]]")
            lines.append(f"  - path: `{page.rel_path}`")
            lines.append(f"  - findings: {'; '.join(findings)}")
    lines.append("")

    output_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return JobResult(
        name="wiki-breakdown",
        output_path=output_path,
        summary=[f"pages_flagged={len(candidates)}"],
    )


def build_missing_pages(paths: AthenaPaths) -> JobResult:
    pages = load_wiki_pages(paths)
    wrappers = load_source_wrappers(paths)
    known_titles = existing_titles_set(pages, wrappers)
    candidates: dict[str, MissingPageCandidate] = {}

    def note(title: str, reason: str) -> None:
        key = title_key(title)
        if not key or key in known_titles:
            return
        candidate = candidates.get(key)
        if candidate is None:
            candidates[key] = MissingPageCandidate(title=title.strip(), mentions=1, reasons=[reason])
        else:
            candidate.mentions += 1
            candidate.reasons = dedupe_list([*candidate.reasons, reason])

    for wrapper in wrappers:
        for topic in body_candidate_topics(wrapper):
            note(topic, f"candidate_topic from {wrapper.path.name}")
        for link in links_in(wrapper.body):
            note(link, f"linked in {wrapper.path.name}")

    ranked = sorted(candidates.values(), key=lambda item: (-item.mentions, item.title.casefold()))
    output_path = outputs_dir(paths) / "wiki-missing-pages.md"
    lines = [
        "# Wiki Missing Pages",
        "",
        f"Generated: {datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')}",
        "",
        "## Candidate pages that do not exist yet",
        "",
    ]
    if not ranked:
        lines.append("- None")
    else:
        for item in ranked:
            lines.append(f"- mentions: {item.mentions} | [[{item.title}]]")
            lines.append(f"  - reasons: {'; '.join(item.reasons[:4])}")
    lines.append("")

    output_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return JobResult(
        name="wiki-missing-pages",
        output_path=output_path,
        summary=[f"missing_page_candidates={len(ranked)}"],
    )


def quality_score(page: WikiPage) -> QualityAuditRow:
    findings: list[str] = []
    score = 100
    if not page.frontmatter:
        findings.append("missing_frontmatter")
        score -= 35
    if page.frontmatter.get("updated_at") is None:
        findings.append("missing_updated_at")
        score -= 15
    if "# Sources" not in page.body:
        findings.append("missing_sources_section")
        score -= 25
    if not links_in(page.body):
        findings.append("no_internal_links")
        score -= 10
    source_count = page.frontmatter.get("source_count")
    if source_count in (None, 0, "0"):
        findings.append("missing_or_zero_source_count")
        score -= 15
    return QualityAuditRow(page=page, score=max(score, 0), findings=findings or ["passes_baseline_checks"])


def build_quality_audit(paths: AthenaPaths) -> JobResult:
    rows = sorted((quality_score(page) for page in load_wiki_pages(paths)), key=lambda item: (item.score, item.page.title.casefold()))
    output_path = outputs_dir(paths) / "wiki-quality-audit.md"
    lines = [
        "# Wiki Quality Audit",
        "",
        f"Generated: {datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')}",
        "",
    ]
    for row in rows:
        lines.append(f"- score: {row.score} | [[{row.page.title}]]")
        lines.append(f"  - path: `{row.page.rel_path}`")
        lines.append(f"  - findings: {'; '.join(row.findings)}")
    lines.append("")

    output_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    failing = sum(1 for row in rows if row.score < 100)
    return JobResult(
        name="wiki-quality-audit",
        output_path=output_path,
        summary=[f"pages_audited={len(rows)}", f"pages_with_findings={failing}"],
    )


def run_command(paths: AthenaPaths, command: str) -> list[JobResult]:
    if command == "promotion-queue":
        return [build_promotion_queue(paths)]
    if command == "stale-scan":
        return [build_stale_scan(paths)]
    if command == "daily-digest":
        results = []
        queue_path = outputs_dir(paths) / "wiki-promotion-queue.md"
        stale_path = outputs_dir(paths) / "wiki-stale-pages.md"
        if not queue_path.exists():
            results.append(build_promotion_queue(paths))
        if not stale_path.exists():
            results.append(build_stale_scan(paths))
        results.append(build_daily_digest(paths))
        return results
    if command == "index-rebuild":
        return [build_index_rebuild(paths)]
    if command == "backlinks-rebuild":
        return [build_backlinks_rebuild(paths)]
    if command == "breakdown":
        return [build_breakdown(paths)]
    if command == "missing-pages":
        return [build_missing_pages(paths)]
    if command == "quality-audit":
        return [build_quality_audit(paths)]
    if command == "all":
        promotion = build_promotion_queue(paths)
        stale = build_stale_scan(paths)
        digest = build_daily_digest(paths)
        index = build_index_rebuild(paths)
        backlinks = build_backlinks_rebuild(paths)
        breakdown = build_breakdown(paths)
        missing = build_missing_pages(paths)
        quality = build_quality_audit(paths)
        return [promotion, stale, digest, index, backlinks, breakdown, missing, quality]
    raise ValueError(f"Unknown command: {command}")


def main() -> int:
    args = parse_args()
    paths = resolve_paths(args)
    results = run_command(paths, args.command)
    for result in results:
        print(f"{result.name}: {result.output_path}")
        for line in result.summary:
            print(f"  - {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
