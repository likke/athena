from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import AthenaPaths, default_paths
from .source_docs import iter_text_files


@dataclass(frozen=True)
class KnowledgeBasePaths:
    root: Path
    config_dir: Path
    automation_dir: Path
    indexes_dir: Path
    outputs_dir: Path
    scripts_dir: Path
    sources_dir: Path
    state_dir: Path
    wiki_dir: Path
    connector_state_dir: Path
    connector_summary_path: Path
    wiki_health_path: Path
    wiki_refresh_status_path: Path
    wiki_refresh_summary_path: Path


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def resolve_kb_paths(
    *,
    paths: AthenaPaths | None = None,
    kb_root: Path | None = None,
) -> KnowledgeBasePaths:
    resolved_paths = paths or default_paths()
    root = (kb_root or (resolved_paths.workspace_telegram_root / "knowledge-base")).expanduser().resolve()
    outputs_dir = root / "outputs"
    state_dir = root / "state"
    return KnowledgeBasePaths(
        root=root,
        config_dir=root / "config",
        automation_dir=root / "automation",
        indexes_dir=root / "indexes",
        outputs_dir=outputs_dir,
        scripts_dir=root / "scripts",
        sources_dir=root / "sources",
        state_dir=state_dir,
        wiki_dir=root / "wiki",
        connector_state_dir=state_dir / "connectors",
        connector_summary_path=outputs_dir / "connector-status.md",
        wiki_health_path=outputs_dir / "wiki-health.md",
        wiki_refresh_status_path=state_dir / "wiki-refresh-status.json",
        wiki_refresh_summary_path=outputs_dir / "wiki-refresh-status.md",
    )


def _ensure_parent(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _safe_read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _safe_read_text(path: Path) -> str:
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def _tail_lines(path: Path, *, keep: int = 40) -> list[str]:
    text = _safe_read_text(path)
    if not text:
        return []
    return text.splitlines()[-keep:]


def _wrapper_status(path: Path) -> str | None:
    text = _safe_read_text(path)
    if not text:
        return None
    match = re.search(r"^status:\s*(.+?)\s*$", text, flags=re.MULTILINE)
    if not match:
        return None
    return match.group(1).strip().strip("'\"")


def _resolve_target_wrapper_path(root: Path, target_path: str) -> Path:
    candidate = Path(target_path)
    if candidate.is_absolute():
        return candidate.resolve()
    if candidate.parts and candidate.parts[0] == root.name:
        return (root.parent / candidate).resolve()
    return (root / candidate).resolve()


def skill_read(
    skill_name: str,
    *,
    paths: AthenaPaths | None = None,
    workspace: str = "telegram",
) -> dict[str, Any]:
    resolved_paths = paths or default_paths()
    base = resolved_paths.workspace_telegram_root if workspace == "telegram" else resolved_paths.workspace_root
    skill_path = (base / "skills" / skill_name / "SKILL.md").expanduser().resolve()
    if not skill_path.exists():
        raise FileNotFoundError(f"Skill not found: {skill_path}")
    content = skill_path.read_text(encoding="utf-8")
    return {
        "ok": True,
        "skill": skill_name,
        "workspace": workspace,
        "path": str(skill_path),
        "content": content,
        "line_count": len(content.splitlines()),
    }


def repo_discover(
    *,
    root: Path | None = None,
    max_depth: int = 4,
    paths: AthenaPaths | None = None,
) -> dict[str, Any]:
    resolved_paths = paths or default_paths()
    search_root = (root or resolved_paths.workspace_telegram_root).expanduser().resolve()
    repos: list[dict[str, Any]] = []
    seen: set[str] = set()

    for current_root, dirnames, filenames in os.walk(search_root):
        current = Path(current_root)
        depth = 0 if current == search_root else len(current.relative_to(search_root).parts)
        if depth > max_depth:
            dirnames[:] = []
            continue

        has_git = ".git" in dirnames
        has_pyproject = "pyproject.toml" in filenames
        if has_git or has_pyproject:
            current_str = str(current)
            if current_str in seen:
                continue
            seen.add(current_str)
            docs: list[str] = []
            for doc in sorted(current.rglob("*")):
                try:
                    rel = doc.relative_to(current)
                except ValueError:
                    continue
                if len(rel.parts) > 3 or not doc.is_file():
                    continue
                if (
                    doc.name == "README.md"
                    or doc.name == "ARCHITECTURE.md"
                    or doc.name == "pyproject.toml"
                    or (len(rel.parts) >= 2 and rel.parts[0] == "docs" and doc.suffix.lower() == ".md")
                    or doc.suffix.lower() == ".json"
                ):
                    docs.append(str(doc))
            repos.append(
                {
                    "path": current_str,
                    "has_git": has_git,
                    "has_pyproject": has_pyproject,
                    "doc_count": len(docs),
                    "docs": docs[:50],
                }
            )
            dirnames[:] = [d for d in dirnames if d != ".git"]

    repos.sort(key=lambda item: item["path"])
    preferred = None
    kb_repo = resolve_kb_paths(paths=resolved_paths).root / "tmp-athena-repo"
    if kb_repo.exists():
        preferred = str(kb_repo.resolve())
    elif (Path("/Users/fleirecastro/athena")).exists():
        preferred = str(Path("/Users/fleirecastro/athena").resolve())

    return {
        "ok": True,
        "root": str(search_root),
        "max_depth": max_depth,
        "repo_count": len(repos),
        "preferred_repo": preferred,
        "repos": repos,
    }


def _parse_summary_count(text: str, label: str) -> int:
    match = re.search(rf"-\s+{re.escape(label)}:\s+(\d+)", text)
    return int(match.group(1)) if match else 0


def _load_drive_local_folders(paths: AthenaPaths) -> list[dict[str, str]]:
    raw = _safe_read_json(paths.google_settings_path)
    drive_cfg = raw.get("drive") or {}
    folders = drive_cfg.get("local_folders") or []
    return [folder for folder in folders if isinstance(folder, dict)]


def _drive_mirror_status(paths: AthenaPaths, kb_paths: KnowledgeBasePaths) -> dict[str, Any]:
    folders = _load_drive_local_folders(paths)
    folder_path = None
    folder_name = None
    if folders:
        folder_name = str(folders[0].get("name") or "Athena Drive Mirror")
        raw_path = str(folders[0].get("path") or "").strip()
        if raw_path:
            folder_path = Path(raw_path).expanduser().resolve()
    summary_path = paths.google_mirror_dir / "drive-local" / "athena-drive-mirror-summary.md"
    summary_text = _safe_read_text(summary_path)
    mirrored_files = _parse_summary_count(summary_text, "mirrored_files")
    skipped_files = _parse_summary_count(summary_text, "skipped_files")
    local_count = len(iter_text_files(folder_path, recursive=True)) if folder_path and folder_path.exists() else 0
    status = "healthy"
    if not folder_path or not folder_path.exists():
        status = "blocked"
    elif skipped_files:
        status = "degraded"
    return {
        "name": "drive_mirror",
        "status": status,
        "folder_name": folder_name,
        "folder_path": str(folder_path) if folder_path else None,
        "configured": bool(folder_path),
        "available": bool(folder_path and folder_path.exists()),
        "summary_path": str(summary_path),
        "summary_exists": summary_path.exists(),
        "local_text_like_files": local_count,
        "mirrored_files": mirrored_files or local_count,
        "skipped_files": skipped_files,
        "last_summary_at": summary_path.stat().st_mtime if summary_path.exists() else None,
    }


def _source_config_status(config_path: Path, name: str, *, root: Path | None = None) -> dict[str, Any]:
    config = _safe_read_json(config_path)
    entries = config.get("sources") or []
    existing_paths = 0
    missing_paths = 0
    query_only = 0
    wrapper_status_counts: dict[str, int] = {}
    missing_wrappers = 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        source_path = entry.get("source_path")
        if source_path:
            if Path(str(source_path)).expanduser().exists():
                existing_paths += 1
            else:
                missing_paths += 1
        elif entry.get("source_query"):
            query_only += 1
        target_path = str(entry.get("target_path") or "").strip()
        if target_path and root is not None:
            wrapper_path = _resolve_target_wrapper_path(root, target_path)
            status = _wrapper_status(wrapper_path) if wrapper_path.exists() else None
            if status:
                wrapper_status_counts[status] = wrapper_status_counts.get(status, 0) + 1
            else:
                missing_wrappers += 1
    status = "healthy" if entries else "unknown"
    if missing_paths or missing_wrappers:
        status = "degraded"
    if any(key in wrapper_status_counts for key in ("missing_source", "query_error", "invalid_source")):
        status = "degraded"
    return {
        "name": name,
        "status": status,
        "config_path": str(config_path),
        "configured_sources": len(entries),
        "existing_source_paths": existing_paths,
        "missing_source_paths": missing_paths,
        "query_defined_sources": query_only,
        "wrapper_status_counts": wrapper_status_counts,
        "missing_wrappers": missing_wrappers,
    }


def _gmail_status(paths: AthenaPaths, kb_paths: KnowledgeBasePaths) -> dict[str, Any]:
    status = _source_config_status(kb_paths.config_dir / "gmail-sources.json", "gmail", root=kb_paths.root)
    inbox_summary = paths.google_mirror_dir / "gmail" / "inbox-summary.md"
    status.update(
        {
            "inbox_summary_path": str(inbox_summary),
            "inbox_summary_exists": inbox_summary.exists(),
        }
    )
    if not inbox_summary.exists() and status["configured_sources"]:
        status["status"] = "degraded"
    return status


def _notebooklm_status(paths: AthenaPaths, kb_paths: KnowledgeBasePaths) -> dict[str, Any]:
    status = _source_config_status(kb_paths.config_dir / "notebooklm-sources.json", "notebooklm", root=kb_paths.root)
    summary_path = paths.google_mirror_dir / "notebooklm" / "notebooklm-exports-summary.md"
    status.update(
        {
            "summary_path": str(summary_path),
            "summary_exists": summary_path.exists(),
            "export_dir": str(paths.notebooklm_export_dir),
            "export_dir_exists": paths.notebooklm_export_dir.exists(),
        }
    )
    if not summary_path.exists() and status["configured_sources"]:
        status["status"] = "degraded"
    return status


def _transcript_status(kb_paths: KnowledgeBasePaths) -> dict[str, Any]:
    return _source_config_status(kb_paths.config_dir / "transcript-sources.json", "transcripts", root=kb_paths.root)


def _garmin_status() -> dict[str, Any]:
    script_path = Path("/Users/fleirecastro/scripts/garmin-sync.py")
    log_path = Path("/Users/fleirecastro/.openclaw/logs/garmin-sync.log")
    lines = [
        line
        for line in _tail_lines(log_path, keep=200)
        if line.strip() and "NotOpenSSLWarning" not in line
    ]
    last_message = lines[-1] if lines else ""
    last_success = next((line for line in reversed(lines) if line.startswith("Synced Garmin data for ")), None)
    recent_env_error = next((line for line in reversed(lines) if "Missing required env vars" in line), None)
    recent_write_error = any("field type conflict" in line or "ApiException: (422)" in line for line in lines)

    status = "unknown"
    error_type = None
    if "incomplete all-zero profile" in last_message:
        status = "degraded"
        error_type = "source_incomplete"
    elif recent_env_error and not last_success:
        status = "blocked"
        error_type = "missing_env"
    elif recent_write_error:
        status = "degraded"
        error_type = "sink_conflict"
    elif last_success:
        status = "healthy"

    return {
        "name": "garmin",
        "status": status,
        "script_path": str(script_path),
        "script_exists": script_path.exists(),
        "log_path": str(log_path),
        "log_exists": log_path.exists(),
        "last_message": last_message or None,
        "last_success": last_success,
        "recent_env_error": recent_env_error,
        "recent_write_error": recent_write_error,
        "error_type": error_type,
        "env_present_in_current_process": {
            "GARMIN_EMAIL": bool(os.environ.get("GARMIN_EMAIL")),
            "GARMIN_PASSWORD": bool(os.environ.get("GARMIN_PASSWORD")),
        },
    }


def connector_status(
    *,
    name: str | None = None,
    paths: AthenaPaths | None = None,
    kb_root: Path | None = None,
    write: bool = True,
) -> dict[str, Any]:
    resolved_paths = paths or default_paths()
    kb = resolve_kb_paths(paths=resolved_paths, kb_root=kb_root)
    statuses = {
        "drive_mirror": _drive_mirror_status(resolved_paths, kb),
        "gmail": _gmail_status(resolved_paths, kb),
        "notebooklm": _notebooklm_status(resolved_paths, kb),
        "transcripts": _transcript_status(kb),
        "garmin": _garmin_status(),
    }

    if write:
        kb.connector_state_dir.mkdir(parents=True, exist_ok=True)
        for key, value in statuses.items():
            path = kb.connector_state_dir / f"{key}.json"
            _ensure_parent(path).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        lines = ["# Connector Status", "", f"Updated: {_now_iso()}", ""]
        for key in ("drive_mirror", "gmail", "notebooklm", "transcripts", "garmin"):
            item = statuses[key]
            lines.append(f"- **{key}** — `{item['status']}`")
            if key == "drive_mirror":
                lines.append(f"  - folder: `{item.get('folder_path')}`")
                lines.append(f"  - mirrored_files: `{item.get('mirrored_files')}`")
                lines.append(f"  - skipped_files: `{item.get('skipped_files')}`")
            elif key == "garmin":
                if item.get("last_success"):
                    lines.append(f"  - last_success: `{item['last_success']}`")
                if item.get("last_message"):
                    lines.append(f"  - last_message: {item['last_message']}")
            else:
                lines.append(f"  - configured_sources: `{item.get('configured_sources')}`")
                if item.get("missing_source_paths"):
                    lines.append(f"  - missing_source_paths: `{item['missing_source_paths']}`")
                if item.get("query_defined_sources"):
                    lines.append(f"  - query_defined_sources: `{item['query_defined_sources']}`")
                if item.get("missing_wrappers"):
                    lines.append(f"  - missing_wrappers: `{item['missing_wrappers']}`")
                if item.get("wrapper_status_counts"):
                    lines.append(f"  - wrapper_status_counts: `{json.dumps(item['wrapper_status_counts'], sort_keys=True)}`")
        lines.append("")
        _ensure_parent(kb.connector_summary_path).write_text("\n".join(lines), encoding="utf-8")

    if name:
        if name not in statuses:
            raise ValueError(f"Unknown connector: {name}")
        return {"ok": True, "generated_at": _now_iso(), "connector": statuses[name]}
    return {"ok": True, "generated_at": _now_iso(), "connectors": statuses}


def wiki_health(
    *,
    paths: AthenaPaths | None = None,
    kb_root: Path | None = None,
    write: bool = True,
) -> dict[str, Any]:
    resolved_paths = paths or default_paths()
    kb = resolve_kb_paths(paths=resolved_paths, kb_root=kb_root)
    connectors = connector_status(paths=resolved_paths, kb_root=kb.root, write=write)["connectors"]
    indexes = sorted(p.name for p in kb.indexes_dir.glob("*.md"))
    source_wrappers = len(list(kb.sources_dir.rglob("*.md")))
    wiki_pages = len(list(kb.wiki_dir.glob("*.md")))
    health = {
        "ok": True,
        "generated_at": _now_iso(),
        "kb_root": str(kb.root),
        "source_wrapper_count": source_wrappers,
        "wiki_page_count": wiki_pages,
        "index_files": indexes,
        "indexes_ok": all((kb.indexes_dir / name).exists() for name in ("source-index.md", "wiki-index.md")),
        "lint_report_exists": (kb.outputs_dir / "wiki-lint-report.md").exists(),
        "connector_states": {key: item["status"] for key, item in connectors.items()},
    }
    if write:
        lines = [
            "# Wiki Health",
            "",
            f"Updated: {health['generated_at']}",
            "",
            f"- kb_root: `{kb.root}`",
            f"- source_wrapper_count: `{source_wrappers}`",
            f"- wiki_page_count: `{wiki_pages}`",
            f"- indexes_ok: `{health['indexes_ok']}`",
            f"- lint_report_exists: `{health['lint_report_exists']}`",
            "",
            "## Connector states",
            "",
        ]
        for key, value in health["connector_states"].items():
            lines.append(f"- `{key}`: `{value}`")
        lines.append("")
        _ensure_parent(kb.wiki_health_path).write_text("\n".join(lines), encoding="utf-8")
    return health


def _run_python_step(script_path: Path, *, cwd: Path) -> dict[str, Any]:
    if not script_path.exists():
        return {
            "name": script_path.stem,
            "ok": False,
            "returncode": None,
            "stdout": "",
            "stderr": f"Missing script: {script_path}",
        }
    result = subprocess.run(
        [sys.executable, str(script_path)],
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )
    return {
        "name": script_path.stem,
        "ok": result.returncode == 0,
        "returncode": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }


def wiki_refresh(
    *,
    paths: AthenaPaths | None = None,
    kb_root: Path | None = None,
) -> dict[str, Any]:
    resolved_paths = paths or default_paths()
    kb = resolve_kb_paths(paths=resolved_paths, kb_root=kb_root)
    kb.outputs_dir.mkdir(parents=True, exist_ok=True)
    kb.state_dir.mkdir(parents=True, exist_ok=True)

    steps = [
        kb.scripts_dir / "import_drive_mirror.py",
        kb.scripts_dir / "import_notebooklm.py",
        kb.scripts_dir / "import_gmail.py",
        kb.scripts_dir / "import_transcripts.py",
        kb.scripts_dir / "compile_kb.py",
        kb.scripts_dir / "lint_wiki.py",
    ]
    results = [_run_python_step(script, cwd=kb.root.parent) for script in steps]
    connector_snapshot = connector_status(paths=resolved_paths, kb_root=kb.root, write=True)
    health_snapshot = wiki_health(paths=resolved_paths, kb_root=kb.root, write=True)
    status = {
        "ok": all(step["ok"] for step in results),
        "generated_at": _now_iso(),
        "kb_root": str(kb.root),
        "steps": results,
        "connector_summary_path": str(kb.connector_summary_path),
        "wiki_health_path": str(kb.wiki_health_path),
    }
    _ensure_parent(kb.wiki_refresh_status_path).write_text(
        json.dumps(
            {
                **status,
                "connector_snapshot": connector_snapshot,
                "health_snapshot": health_snapshot,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    lines = [
        "# Wiki Refresh Status",
        "",
        f"Updated: {status['generated_at']}",
        "",
        f"- ok: `{status['ok']}`",
        f"- kb_root: `{kb.root}`",
        "",
        "## Steps",
        "",
    ]
    for step in results:
        lines.append(f"- **{step['name']}** — `{'ok' if step['ok'] else 'failed'}`")
        if step["stdout"]:
            lines.append(f"  - stdout: {step['stdout']}")
        if step["stderr"]:
            lines.append(f"  - stderr: {step['stderr']}")
    lines.append("")
    lines.append(f"- connector_status: `{kb.connector_summary_path}`")
    lines.append(f"- wiki_health: `{kb.wiki_health_path}`")
    lines.append("")
    _ensure_parent(kb.wiki_refresh_summary_path).write_text("\n".join(lines), encoding="utf-8")
    return status
