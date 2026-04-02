from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import AthenaPaths, default_paths
from .db import now_ts, query_all, query_one
from .source_docs import (
    choose_document_id,
    default_document_id,
    dedupe_source_documents,
    text_summary,
    upsert_source_document,
)

PHT = timezone(timedelta(hours=8))
WEEKLY_CEO_BRIEF_KIND = "weekly_ceo_brief"
LATEST_WEEKLY_CEO_BRIEF = "LATEST_WEEKLY_CEO_BRIEF.md"


def _ensure_dir(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def _pht(ts: int | None) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(ts, tz=PHT).strftime("%Y-%m-%d %H:%M %Z")


def _week_start(ts: int) -> date:
    current = datetime.fromtimestamp(ts, tz=PHT).date()
    return current - timedelta(days=current.weekday())


def _age_note(ts: int | None, *, now: int) -> str:
    if not ts:
        return "no recent progress recorded"
    delta = datetime.fromtimestamp(now, tz=PHT).date() - datetime.fromtimestamp(ts, tz=PHT).date()
    days = max(delta.days, 0)
    if days == 0:
        return "updated today"
    if days == 1:
        return "updated 1 day ago"
    return f"updated {days} days ago"


def _replace_brief(conn, scope_kind: str, scope_id: str, brief_type: str, content: str, ts: int) -> None:
    conn.execute(
        "DELETE FROM awareness_briefs WHERE scope_kind = ? AND scope_id = ? AND brief_type = ?",
        (scope_kind, scope_id, brief_type),
    )
    conn.execute(
        "INSERT INTO awareness_briefs (scope_kind, scope_id, brief_type, content, created_at) VALUES (?, ?, ?, ?, ?)",
        (scope_kind, scope_id, brief_type, content.strip(), ts),
    )


def _sentence(text: str) -> str:
    clean = text.strip().rstrip(".!?")
    if not clean:
        return ""
    return f"{clean}."


def _open_counts(conn) -> dict[str, int]:
    row = query_one(
        conn,
        """
        SELECT
          COALESCE(SUM(CASE WHEN status IN ('queued', 'in_progress', 'blocked', 'someday') THEN 1 ELSE 0 END), 0) AS open_tasks,
          COALESCE(SUM(CASE WHEN status = 'blocked' THEN 1 ELSE 0 END), 0) AS blocked_tasks,
          COALESCE(SUM(CASE WHEN status = 'in_progress' THEN 1 ELSE 0 END), 0) AS in_progress_tasks
        FROM tasks
        """,
    ) or {}
    return {
        "open_tasks": int(row.get("open_tasks") or 0),
        "blocked_tasks": int(row.get("blocked_tasks") or 0),
        "in_progress_tasks": int(row.get("in_progress_tasks") or 0),
    }


def _active_life_goals(conn) -> list[dict[str, Any]]:
    return query_all(
        conn,
        """
        SELECT
          g.id,
          g.title,
          g.current_focus,
          g.supporting_rule,
          g.risk_if_ignored,
          g.status_note,
          g.last_reviewed_at,
          la.name AS area_name,
          la.priority AS area_priority
        FROM life_goals g
        JOIN life_areas la ON la.id = g.life_area_id
        WHERE g.status = 'active'
        ORDER BY la.priority DESC, g.updated_at DESC
        LIMIT 6
        """,
    )


def _priority_projects(conn) -> list[dict[str, Any]]:
    return query_all(
        conn,
        """
        SELECT
          p.id,
          p.name,
          p.status,
          p.health,
          p.current_goal,
          p.next_milestone,
          p.blocker,
          p.rollup_summary,
          p.last_real_progress_at,
          p.last_reviewed_at,
          pf.name AS portfolio_name,
          pf.priority AS portfolio_priority
        FROM projects p
        JOIN portfolios pf ON pf.id = p.portfolio_id
        WHERE p.status IN ('active', 'blocked')
        ORDER BY
          pf.priority DESC,
          CASE p.status WHEN 'blocked' THEN 0 ELSE 1 END,
          CASE p.health WHEN 'red' THEN 0 WHEN 'yellow' THEN 1 ELSE 2 END,
          p.updated_at DESC
        LIMIT 8
        """,
    )


def _blocked_tasks(conn) -> list[dict[str, Any]]:
    return query_all(
        conn,
        """
        SELECT
          t.id,
          t.title,
          t.priority,
          t.blocker,
          t.next_action,
          t.requires_approval,
          t.requires_browser,
          COALESCE(p.name, '') AS project_name
        FROM tasks t
        LEFT JOIN projects p ON p.id = t.project_id
        WHERE t.status = 'blocked'
        ORDER BY t.priority DESC, t.last_touched_at ASC
        LIMIT 8
        """,
    )


def _approval_queue(conn) -> list[dict[str, Any]]:
    return query_all(
        conn,
        """
        SELECT
          o.id,
          o.subject,
          o.to_recipients,
          o.status,
          o.account_label,
          COALESCE(p.name, '') AS project_name
        FROM outbox_items o
        LEFT JOIN projects p ON p.id = o.project_id
        WHERE o.status IN ('needs_approval', 'approved')
        ORDER BY
          CASE o.status WHEN 'needs_approval' THEN 0 ELSE 1 END,
          o.updated_at DESC
        LIMIT 8
        """,
    )


def _capture_queue(conn) -> list[dict[str, Any]]:
    return query_all(
        conn,
        """
        SELECT raw_text, classification, note, created_at
        FROM captured_items
        WHERE status = 'new'
        ORDER BY created_at DESC
        LIMIT 5
        """,
    )


def _fresh_context(conn) -> list[dict[str, Any]]:
    return query_all(
        conn,
        """
        SELECT title, source_system, kind, summary, last_synced_at
        FROM source_documents
        WHERE kind != ?
          AND source_system NOT IN ('life-doc', 'athena')
        ORDER BY COALESCE(last_synced_at, updated_at) DESC, updated_at DESC
        LIMIT 6
        """,
        (WEEKLY_CEO_BRIEF_KIND,),
    )


def _calendar_agenda_lines(conn) -> list[str]:
    row = query_one(
        conn,
        """
        SELECT path
        FROM source_documents
        WHERE kind = 'calendar_agenda'
        ORDER BY COALESCE(last_synced_at, updated_at) DESC, updated_at DESC
        LIMIT 1
        """,
    )
    if not row or not row.get("path"):
        return []
    path = Path(str(row["path"])).expanduser().resolve()
    if not path.exists():
        return []
    lines = [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith("- ")
    ]
    return lines[:6]


def _executive_summary(
    *,
    counts: dict[str, int],
    life_goals: list[dict[str, Any]],
    projects: list[dict[str, Any]],
    blocked_tasks: list[dict[str, Any]],
    approvals: list[dict[str, Any]],
) -> list[str]:
    bullets: list[str] = []
    if life_goals:
        goal = life_goals[0]
        goal_title = _sentence(str(goal.get("title") or "Life focus"))
        focus = _sentence(str(goal.get("current_focus") or goal.get("supporting_rule") or goal.get("title") or ""))
        bullets.append(f"Life focus: {goal_title} {focus}".strip())
    if blocked_tasks:
        task = blocked_tasks[0]
        task_title = _sentence(str(task.get("title") or "Blocked task"))
        reason = _sentence(str(task.get("blocker") or task.get("next_action") or "Needs a decision"))
        bullets.append(f"Decision needed: {task_title} {reason}".strip())
    elif projects:
        project = projects[0]
        project_title = _sentence(str(project.get("name") or "Project focus"))
        next_step = _sentence(str(project.get("next_milestone") or project.get("current_goal") or "Needs momentum"))
        bullets.append(f"Project focus: {project_title} {next_step}".strip())
    if approvals:
        pending = sum(1 for row in approvals if row.get("status") == "needs_approval")
        approved = sum(1 for row in approvals if row.get("status") == "approved")
        if pending:
            bullets.append(f"Approval queue: {pending} email approval(s) are waiting on Fleire.")
        elif approved:
            bullets.append(f"Approval queue: {approved} approved email(s) are ready to send.")
    if counts.get("blocked_tasks", 0) and not blocked_tasks:
        bullets.append(f"Blocked work: {counts['blocked_tasks']} task(s) need review.")
    if not bullets:
        bullets.append("System is calm. No urgent escalations are open.")
    return bullets[:4]


def generate_weekly_ceo_brief(
    conn,
    *,
    paths: AthenaPaths | None = None,
    ts: int | None = None,
) -> dict[str, Any]:
    resolved_paths = paths or default_paths()
    generated_at = ts or now_ts()
    week_of = _week_start(generated_at)
    counts = _open_counts(conn)
    life_goals = _active_life_goals(conn)
    projects = _priority_projects(conn)
    blocked_tasks = _blocked_tasks(conn)
    approvals = _approval_queue(conn)
    captures = _capture_queue(conn)
    fresh_context = _fresh_context(conn)
    calendar_agenda = _calendar_agenda_lines(conn)
    executive = _executive_summary(
        counts=counts,
        life_goals=life_goals,
        projects=projects,
        blocked_tasks=blocked_tasks,
        approvals=approvals,
    )

    lines = [
        "# Athena CEO Weekly Brief",
        "",
        f"- week_of: {week_of.isoformat()}",
        f"- generated_at: {_pht(generated_at)}",
        f"- open_tasks: {counts['open_tasks']}",
        f"- blocked_tasks: {counts['blocked_tasks']}",
        f"- in_progress_tasks: {counts['in_progress_tasks']}",
        f"- approvals_waiting: {sum(1 for row in approvals if row.get('status') == 'needs_approval')}",
        "",
        "## Executive summary",
        "",
    ]
    lines.extend(f"- {bullet}" for bullet in executive)
    lines.extend(["", "## Life alignment", ""])
    if life_goals:
        for goal in life_goals:
            focus = str(goal.get("current_focus") or "No current focus recorded").strip()
            rule = str(goal.get("supporting_rule") or "No explicit supporting rule.").strip()
            risk = str(goal.get("risk_if_ignored") or "Risk not documented.").strip()
            lines.extend(
                [
                    f"- {goal['title']} [{goal['area_name']}]",
                    f"  - focus: {focus}",
                    f"  - rule: {rule}",
                    f"  - risk: {risk}",
                ]
            )
    else:
        lines.append("- No active life goals are recorded.")

    lines.extend(["", "## Portfolio snapshot", ""])
    if projects:
        for project in projects:
            status = f"{project['status']}/{project['health']}"
            goal = str(project.get("current_goal") or "No current goal recorded").strip()
            milestone = str(project.get("next_milestone") or "No next milestone recorded").strip()
            blocker = str(project.get("blocker") or "No current blocker.").strip()
            lines.extend(
                [
                    f"- {project['portfolio_name']} / {project['name']} [{status}]",
                    f"  - goal: {goal}",
                    f"  - next milestone: {milestone}",
                    f"  - blocker: {blocker}",
                    f"  - progress: {_age_note(project.get('last_real_progress_at'), now=generated_at)}",
                ]
            )
    else:
        lines.append("- No active or blocked projects are recorded.")

    lines.extend(["", "## Decisions and approvals", ""])
    if blocked_tasks:
        for task in blocked_tasks[:5]:
            reason = str(task.get("blocker") or task.get("next_action") or "Needs review").strip()
            flags: list[str] = []
            if task.get("requires_approval"):
                flags.append("approval")
            if task.get("requires_browser"):
                flags.append("browser")
            flag_text = f" [{', '.join(flags)}]" if flags else ""
            project_name = f" ({task['project_name']})" if task.get("project_name") else ""
            lines.append(f"- Blocked task: {task['title']}{project_name}{flag_text} — {reason}")
    else:
        lines.append("- No blocked tasks are open.")
    if approvals:
        for item in approvals[:5]:
            recipient = str(item.get("to_recipients") or "").strip()
            project_name = f" ({item['project_name']})" if item.get("project_name") else ""
            lines.append(f"- Email {item['status']}: {item['subject']}{project_name} -> {recipient}")
    else:
        lines.append("- No outbox approvals are waiting.")
    if captures:
        lines.append(f"- Inbox pressure: {len(captures)} new capture(s) still need triage.")

    lines.extend(["", "## Calendar pressure", ""])
    if calendar_agenda:
        lines.extend(calendar_agenda)
    else:
        lines.append("- No mirrored calendar agenda is available yet.")

    lines.extend(["", "## Fresh context imported", ""])
    if fresh_context:
        for item in fresh_context:
            generated_note = _pht(item.get("last_synced_at"))
            prefix = f"{item['source_system']} / {item['title']}"
            summary = str(item.get("summary") or "No summary recorded.").strip()
            lines.append(f"- {prefix} ({generated_note or 'unknown sync'}) — {summary}")
    else:
        lines.append("- No recent external context has been mirrored.")

    lines.extend(["", "## Suggested focus for next 7 days", ""])
    lines.append(f"1. {executive[0]}")
    if len(executive) > 1:
        lines.append(f"2. {executive[1]}")
    else:
        lines.append("2. Keep project and task state current so Athena stays trustworthy.")
    if len(executive) > 2:
        lines.append(f"3. {executive[2]}")
    else:
        lines.append("3. Clear one blocker or approval bottleneck before it compounds.")

    content = "\n".join(lines).strip() + "\n"
    briefs_dir = _ensure_dir(resolved_paths.briefs_dir)
    versioned_path = briefs_dir / f"weekly-ceo-brief-{week_of.isoformat()}.md"
    latest_path = briefs_dir / LATEST_WEEKLY_CEO_BRIEF
    versioned_path.write_text(content, encoding="utf-8")
    latest_path.write_text(content, encoding="utf-8")

    title = f"Athena CEO Weekly Brief - Week of {week_of.isoformat()}"
    summary = " | ".join(executive[:3]).strip()
    doc_id = choose_document_id(
        conn,
        versioned_path,
        default_document_id("weekly-ceo-brief", versioned_path),
    )
    upsert_source_document(
        conn,
        doc_id=doc_id,
        kind=WEEKLY_CEO_BRIEF_KIND,
        title=title,
        path=versioned_path,
        source_system="athena",
        is_authoritative=False,
        summary=summary or text_summary(content),
    )
    dedupe_source_documents(conn, versioned_path, doc_id)
    _replace_brief(conn, "global", "global", "weekly_ceo", summary or text_summary(content), generated_at)
    return {
        "title": title,
        "summary": summary or text_summary(content),
        "path": str(versioned_path),
        "latest_path": str(latest_path),
        "week_of": week_of.isoformat(),
        "generated_at": generated_at,
    }


def list_weekly_ceo_briefs(conn, *, limit: int = 8) -> dict[str, Any]:
    rows = query_all(
        conn,
        """
        SELECT id, title, path, summary, source_system, last_synced_at
        FROM source_documents
        WHERE kind = ?
        ORDER BY COALESCE(last_synced_at, updated_at) DESC, updated_at DESC
        LIMIT ?
        """,
        (WEEKLY_CEO_BRIEF_KIND, limit),
    )
    for row in rows:
        row["generated_at_formatted"] = _pht(row.get("last_synced_at"))
    latest = rows[0] if rows else None
    if latest and latest.get("path"):
        path = Path(str(latest["path"])).expanduser().resolve()
        latest["content"] = path.read_text(encoding="utf-8") if path.exists() else ""
    return {"items": rows, "latest": latest}
