from __future__ import annotations

import argparse
import html
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

from .config import AthenaPaths, default_paths
from .db import connect_db, dashboard_snapshot, ensure_db, query_all, query_one
from .google import list_gmail_accounts
from .reviews import run_review_cycle
from .synthesis import list_weekly_ceo_briefs
from .sync import run_sync

try:
    from . import state as state_module
except ImportError:  # pragma: no cover - best effort during bootstrap
    class _StateStub:
        def _missing(self, *args, **kwargs):
            raise NotImplementedError("state module not implemented")

        capture_item = _missing
        create_task = _missing
        start_task = _missing
        block_task = _missing
        complete_task = _missing
        reopen_task = _missing
        update_project_status = _missing

    state_module = _StateStub()

try:
    from . import outbox as outbox_module
except ImportError:  # pragma: no cover - best effort during bootstrap
    class _OutboxStub:
        def _missing(self, *args, **kwargs):
            raise NotImplementedError("outbox module not implemented")

        create_email_outbox = _missing
        approve_outbox_items = _missing
        reject_outbox_items = _missing
        send_outbox_items = _missing

    outbox_module = _OutboxStub()


def _pht(ts: int | None) -> str:
    if not ts:
        return ""
    tz = timezone(timedelta(hours=8))
    return datetime.fromtimestamp(ts, tz=tz).strftime("%Y-%m-%d %H:%M %Z")


def _json_response(handler: BaseHTTPRequestHandler, body: Any) -> None:
    payload = json.dumps(body, indent=2)
    handler.send_response(HTTPStatus.OK)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(payload.encode("utf-8"))))
    handler.end_headers()
    handler.wfile.write(payload.encode("utf-8"))


@dataclass
class ServerState:
    paths: AthenaPaths


APP_PAGES: tuple[tuple[str, str, str, str], ...] = (
    ("/", "Overview", "Mission control for what matters right now.", "Athena OS"),
    ("/board", "Board", "Inbox, active work, blocked work, and done.", "Execution"),
    ("/inbox", "Inbox", "Capture raw thoughts, triage loose ends, and keep intake clean.", "Capture"),
    ("/outbox", "Outbox", "Draft, approve, and send email without losing track.", "Approvals"),
    ("/projects", "Projects", "Track portfolio health, milestones, and repo reality.", "Portfolio"),
    ("/briefs", "Briefs", "Weekly founder synthesis, continuity notes, and decision packets.", "Synthesis"),
    ("/context", "Context", "Life rules, source documents, briefs, and operating memory.", "Context"),
)

APP_PAGE_LOOKUP = {
    route: {
        "label": label,
        "description": description,
        "eyebrow": eyebrow,
    }
    for route, label, description, eyebrow in APP_PAGES
}


def _safe_page_route(route: str | None) -> str:
    clean = (route or "/").strip() or "/"
    return clean if clean in APP_PAGE_LOOKUP else "/"


def _hidden_redirect_input(current_path: str) -> str:
    safe = html.escape(_safe_page_route(current_path))
    return f'<input type="hidden" name="redirect_to" value="{safe}">'


def _nav_count(route: str, data: dict[str, Any]) -> int:
    counts = data.get("dashboard", {}).get("counts", {})
    inbox = data.get("dashboard", {}).get("inbox", {})
    outbox = data.get("dashboard", {}).get("outbox", {})
    mapping = {
        "/board": int(counts.get("open_tasks", 0) or 0),
        "/inbox": int(inbox.get("new_items", 0) or 0),
        "/outbox": int(outbox.get("outbox_needs_approval", 0) or 0),
        "/projects": len(data.get("projects", [])),
        "/briefs": len((data.get("weekly_briefs") or {}).get("items", [])),
        "/context": len(data.get("sources", [])),
    }
    return mapping.get(route, 0)


def _render_sidebar_nav(current_path: str, data: dict[str, Any]) -> str:
    current_project = str((data.get("chat") or {}).get("current_project_name") or "No active project")
    current_portfolio = str((data.get("chat") or {}).get("current_portfolio_name") or "No active portfolio")
    links: list[str] = []
    for route, label, description, _eyebrow in APP_PAGES:
        active = " active" if route == current_path else ""
        count = _nav_count(route, data)
        badge = f'<span class="nav-count">{count}</span>' if count else ""
        links.append(
            f"""
            <a class="nav-link{active}" href="{html.escape(route)}">
              <span class="nav-copy">
                <strong>{html.escape(label)}</strong>
                <small>{html.escape(description)}</small>
              </span>
              {badge}
            </a>
            """
        )
    return f"""
    <aside class="app-sidebar">
      <a class="brand-lockup" href="/">
        <div class="brand-mark">AF</div>
        <div>
          <p class="sidebar-kicker">Athena</p>
          <h1>Fleire OS</h1>
          <p>Local-first command center for life, portfolio, and execution.</p>
        </div>
      </a>
      <nav class="app-nav">
        {"".join(links)}
      </nav>
      <section class="sidebar-card">
        <p class="sidebar-kicker">Current focus</p>
        <strong>{html.escape(current_project)}</strong>
        <span>{html.escape(current_portfolio)}</span>
      </section>
    </aside>
    """


def _render_page_header(current_path: str) -> str:
    page = APP_PAGE_LOOKUP[_safe_page_route(current_path)]
    header_links = "".join(
        f'<a class="mini-link{" active" if route == current_path else ""}" href="{html.escape(route)}">{html.escape(label)}</a>'
        for route, label, _description, _eyebrow in APP_PAGES
    )
    return f"""
    <header class="page-header">
      <div>
        <p class="eyebrow">{html.escape(str(page["eyebrow"]))}</p>
        <h1>{html.escape(str(page["label"]))}</h1>
        <p>{html.escape(str(page["description"]))}</p>
      </div>
      <div class="header-links">
        {header_links}
      </div>
    </header>
    """


def _life_context(conn: sqlite3.Connection) -> dict[str, Any]:
    areas = query_all(
        conn,
        """
        SELECT id, name, status, priority, notes
        FROM life_areas
        ORDER BY priority DESC, updated_at DESC
        """,
    )
    goals = query_all(
        conn,
        """
        SELECT g.id, g.title, g.status, g.horizon, g.current_focus, g.supporting_rule, la.name AS area_name
        FROM life_goals g
        JOIN life_areas la ON la.id = g.life_area_id
        ORDER BY la.priority DESC, g.updated_at DESC
        """,
    )
    people = query_all(
        conn,
        """
        SELECT id, name, relationship_type, importance_score, contact_rule
        FROM people
        ORDER BY importance_score DESC, updated_at DESC
        """,
    )
    return {"areas": areas, "goals": goals, "people": people}


def _project_data(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return query_all(
        conn,
        """
        SELECT
          p.id,
          p.name,
          pf.name AS portfolio_name,
          p.status,
          p.health,
          p.derived_status,
          p.derived_health,
          p.current_goal,
          p.next_milestone,
          p.blocker,
          p.rollup_summary,
          p.completion_summary
        FROM projects p
        JOIN portfolios pf ON pf.id = p.portfolio_id
        ORDER BY pf.priority DESC, p.updated_at DESC
        LIMIT 40
        """,
    )


def _repo_data(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return query_all(
        conn,
        """
        SELECT
          pr.repo_name,
          pr.repo_path,
          COALESCE(p.name, '') AS project_name,
          COALESCE(pr.last_seen_branch, '') AS last_seen_branch,
          COALESCE(pr.last_seen_commit, '') AS last_seen_commit,
          COALESCE(pr.last_seen_dirty, 0) AS last_seen_dirty,
          pr.last_scanned_at
        FROM project_repos pr
        LEFT JOIN projects p ON p.id = pr.project_id
        ORDER BY p.updated_at DESC, pr.repo_name
        LIMIT 40
        """,
    )


def _task_data(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return query_all(
        conn,
        """
        SELECT
          t.id,
          t.title,
          t.owner,
          t.bucket,
          t.status,
          t.priority,
          t.next_action,
          t.blocker,
          t.last_touched_at,
          COALESCE(p.name, '') AS project_name,
          COALESCE(pf.name, '') AS portfolio_name
        FROM tasks t
        LEFT JOIN projects p ON p.id = t.project_id
        LEFT JOIN portfolios pf ON pf.id = t.portfolio_id
        ORDER BY
          CASE t.bucket
            WHEN 'ATHENA' THEN 0
            WHEN 'FLEIRE' THEN 1
            WHEN 'BLOCKED' THEN 2
            ELSE 3
          END,
          CASE t.status
            WHEN 'in_progress' THEN 0
            WHEN 'queued' THEN 1
            WHEN 'blocked' THEN 2
            ELSE 3
          END,
          t.priority DESC,
          t.last_touched_at DESC
        LIMIT 40
        """,
    )


def _source_documents(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return query_all(
        conn,
        """
        SELECT id, kind, title, path, external_url, source_system, is_authoritative, summary, last_synced_at
        FROM source_documents
        ORDER BY is_authoritative DESC, updated_at DESC, title
        LIMIT 20
        """,
    )


def _awareness_briefs(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return query_all(
        conn,
        """
        SELECT scope_kind, scope_id, brief_type, content, created_at
        FROM awareness_briefs
        ORDER BY created_at DESC
        LIMIT 10
        """,
    )


def _chat_context(conn: sqlite3.Connection) -> dict[str, Any]:
    state = query_one(
        conn,
        """
        SELECT
          channel,
          chat_id,
          current_capture_id,
          current_project_id,
          current_portfolio_id,
          current_task_id,
          last_user_intent,
          last_progress,
          pending_approval_task_id,
          updated_at
        FROM chat_state
        WHERE channel = 'telegram' AND chat_id = '1937792843'
        LIMIT 1
        """,
    )
    if state and state.get("updated_at"):
        state["updated_at_formatted"] = _pht(state["updated_at"])
    return state or {}


def _capture_data(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return query_all(
        conn,
        """
        SELECT
          id,
          raw_text,
          classification,
          status,
          note,
          linked_entity_kind,
          linked_entity_id,
          source_channel,
          source_chat_id,
          source_message_ref,
          updated_at
        FROM captured_items
        WHERE status IN ('new', 'triaged')
        ORDER BY updated_at DESC
        LIMIT 20
        """,
    )


def _kanban_data(conn: sqlite3.Connection) -> dict[str, list[dict[str, Any]]]:
    rows = query_all(
        conn,
        """
        SELECT
          bucket,
          t.id AS task_id,
          title,
          owner,
          t.status AS status,
          t.priority AS priority,
          t.next_action AS next_action,
          t.blocker AS blocker,
          COALESCE(p.name, '') AS project_name,
          COALESCE(pf.name, '') AS portfolio_name
        FROM tasks t
        LEFT JOIN projects p ON p.id = t.project_id
        LEFT JOIN portfolios pf ON pf.id = t.portfolio_id
        WHERE t.status IN ('queued', 'in_progress', 'blocked')
        ORDER BY t.priority DESC, t.last_touched_at DESC
        """,
    )
    buckets: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        bucket = str(row["bucket"] or "UNASSIGNED")
        buckets.setdefault(bucket, []).append(row)
    return buckets


def _closed_task_data(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return query_all(
        conn,
        """
        SELECT
          t.id,
          t.title,
          t.owner,
          t.status,
          t.resolution,
          t.completion_summary,
          t.closed_at,
          COALESCE(p.name, '') AS project_name
        FROM tasks t
        LEFT JOIN projects p ON p.id = t.project_id
        WHERE t.status IN ('done', 'cancelled')
        ORDER BY t.closed_at DESC, t.updated_at DESC
        LIMIT 12
        """,
    )


def _outbox_data(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return query_all(
        conn,
        """
        SELECT
          o.id,
          o.task_id,
          o.project_id,
          o.account_label,
          o.to_recipients,
          o.cc_recipients,
          o.subject,
          o.body_text,
          o.status,
          o.draft_id,
          o.external_ref,
          o.external_url,
          o.approval_note,
          o.error_message,
          o.sent_at,
          o.updated_at,
          COALESCE(t.title, '') AS task_title,
          COALESCE(p.name, '') AS project_name
        FROM outbox_items o
        LEFT JOIN tasks t ON t.id = o.task_id
        LEFT JOIN projects p ON p.id = o.project_id
        ORDER BY
          CASE o.status
            WHEN 'needs_approval' THEN 0
            WHEN 'approved' THEN 1
            WHEN 'drafting' THEN 2
            WHEN 'sending' THEN 3
            WHEN 'error' THEN 4
            WHEN 'sent' THEN 5
            ELSE 6
          END,
          o.updated_at DESC
        LIMIT 30
        """,
    )


def _gather_data(paths: AthenaPaths) -> dict[str, Any]:
    ensure_db(paths=paths)
    with connect_db(paths.db_path) as conn:
        return {
            "dashboard": dashboard_snapshot(paths.db_path),
            "life": _life_context(conn),
            "inbox": _capture_data(conn),
            "kanban": _kanban_data(conn),
            "closed_tasks": _closed_task_data(conn),
            "outbox": _outbox_data(conn),
            "projects": _project_data(conn),
            "repos": _repo_data(conn),
            "tasks": _task_data(conn),
            "sources": _source_documents(conn),
            "briefs": _awareness_briefs(conn),
            "weekly_briefs": list_weekly_ceo_briefs(conn),
            "chat": _chat_context(conn),
            "gmail_accounts": list_gmail_accounts(paths),
        }


def _render_items(items: list[dict[str, Any]], fields: list[str]) -> str:
    rows = []
    for item in items:
        row = "".join(
            f'<div class="item-field"><strong>{html.escape(field)}:</strong> {html.escape(str(item.get(field, "")))}</div>'
            if item.get(field) is not None
            else ""
            for field in fields
            if field in item
        )
        rows.append(f'<div class="item-card">{row}</div>')
    return "".join(rows) if rows else '<p class="muted">No records yet.</p>'


def _build_stat_grid(counts: dict[str, Any]) -> str:
    labels = [
        ("open_tasks", "Open tasks"),
        ("in_progress_tasks", "In progress"),
        ("queued_tasks", "Queued"),
        ("blocked_tasks", "Blocked"),
        ("closed_tasks", "Closed"),
        ("new_items", "Inbox new"),
        ("triaged_items", "Inbox triaged"),
        ("outbox_needs_approval", "Needs approval"),
        ("outbox_approved", "Approved send"),
    ]
    cards = []
    for key, title in labels:
        cards.append(
            f"""
            <div class="stat-card">
              <span>{html.escape(title)}</span>
              <strong>{html.escape(str(counts.get(key, 0)))}</strong>
            </div>
            """
        )
    return "".join(cards)


def _render_sync_controls(current_path: str) -> str:
    actions = [
        ("all", "Sync all context"),
        ("google", "Sync Google"),
        ("life", "Sync life"),
        ("repos", "Scan repos"),
        ("briefs", "Refresh briefs"),
        ("weekly-brief", "Generate CEO brief"),
    ]
    forms = []
    for command, label in actions:
        forms.append(
            f"""
            <form class="sync-control" method="post" action="/sync/{command}">
              {_hidden_redirect_input(current_path)}
              <button type="submit">{html.escape(label)}</button>
            </form>
            """
        )
    return "".join(forms)


def _render_review_controls(current_path: str) -> str:
    buttons = []
    for cadence, label in (
        ("daily", "Run daily review"),
        ("weekly", "Run weekly review"),
        ("monthly", "Run monthly review"),
    ):
        buttons.append(
            f"""
            <form class="sync-control" method="post" action="/reviews/{cadence}">
              {_hidden_redirect_input(current_path)}
              <button type="submit">{html.escape(label)}</button>
            </form>
            """
        )
    return "".join(buttons)


def _project_options(projects: list[dict[str, Any]], *, include_blank: bool = True) -> str:
    options: list[str] = []
    if include_blank:
        options.append('<option value="">No project</option>')
    for project in projects:
        label = f"{project['portfolio_name']} / {project['name']}"
        options.append(
            f'<option value="{html.escape(str(project["id"]))}">{html.escape(label)}</option>'
        )
    return "".join(options)


def _build_task_action_form(task_id: str, action: str, label: str, body: str, current_path: str) -> str:
    return f"""
    <form class="control-form" method="post" action="/tasks/{task_id}/{action}">
      {_hidden_redirect_input(current_path)}
      {body}
      <button type="submit">{html.escape(label)}</button>
    </form>
    """


def _build_start_form(task_id: str, next_action: str, current_path: str) -> str:
    return _build_task_action_form(
        task_id,
        "start",
        "Start",
        f"""
        <label class="control-label">Next action</label>
        <input type="text" name="next_action" value="{html.escape(next_action)}" placeholder="What happens next?">
        <p class="control-desc muted">Move task into active execution.</p>
        """,
        current_path,
    )


def _build_complete_form(task_id: str, summary: str, current_path: str) -> str:
    return _build_task_action_form(
        task_id,
        "complete",
        "Complete",
        f"""
        <label class="control-label">Completion note</label>
        <input type="text" name="summary" value="{html.escape(summary)}" placeholder="What shipped or was decided?" required>
        <label class="control-label">Evidence</label>
        <textarea name="evidence" rows="3" placeholder="One proof point per line: PR, screenshot, test run, doc, or shipped URL." required></textarea>
        <label class="control-label">Verified by</label>
        <input type="text" name="verified_by" placeholder="Optional verifier">
        <p class="control-desc muted">Tasks only close with evidence. One line per proof item.</p>
        """,
        current_path,
    )


def _build_block_form(task_id: str, blocker: str, next_action: str, current_path: str) -> str:
    return f"""
    <form class="control-form" method="post" action="/tasks/{task_id}/block">
      {_hidden_redirect_input(current_path)}
      <label class="control-label">Blocker</label>
      <input type="text" name="blocker" value="{html.escape(blocker)}" placeholder="Describe the blocker" required>
      <label class="control-label">Next action</label>
      <input type="text" name="next_action" value="{html.escape(next_action)}" placeholder="How do we unblock it?">
      <button type="submit">Block</button>
      <p class="control-desc muted">Capture what is preventing progress.</p>
    </form>
    """


def _build_reopen_form(task_id: str, reason: str, current_path: str) -> str:
    return _build_task_action_form(
        task_id,
        "reopen",
        "Reopen",
        f"""
        <label class="control-label">Reason</label>
        <input type="text" name="reason" value="{html.escape(reason)}" placeholder="Why is this back open?" required>
        <p class="control-desc muted">Clear the old completion and bring the task back.</p>
        """,
        current_path,
    )


def _build_task_controls(task: dict[str, Any], current_path: str) -> str:
    task_id = str(task["task_id"])
    title = str(task.get("title") or task_id)
    next_action = str(task.get("next_action") or "")
    blocker = str(task.get("blocker") or "")
    actions = "".join(
        [
            _build_start_form(task_id, next_action, current_path),
            _build_complete_form(task_id, f"Board completion: {title}", current_path),
            _build_block_form(task_id, blocker, next_action, current_path),
        ]
    )
    return f"""
    <details class="card-actions">
      <summary>Actions</summary>
      <div class="kanban-controls">{actions}</div>
    </details>
    """


def _render_quick_capture_form(current_path: str) -> str:
    return """
    <form class="capture-form" method="post" action="/captures/new">
      """ + _hidden_redirect_input(current_path) + """
      <label class="control-label">Capture something Athena should remember</label>
      <textarea name="raw_text" rows="3" placeholder="New task, life update, decision, blocker, or note..." required></textarea>
      <div class="form-grid">
        <input type="text" name="classification" placeholder="Optional classification">
        <input type="text" name="note" placeholder="Optional note">
      </div>
      <button type="submit">Capture</button>
    </form>
    """

def _render_trello_badges(*labels: tuple[str, str]) -> str:
    chips = []
    for tone, label in labels:
        if not label:
            continue
        chips.append(f'<span class="trello-badge {html.escape(tone)}">{html.escape(label)}</span>')
    return "".join(chips)


def _render_trello_task_card(task: dict[str, Any], current_path: str) -> str:
    project_name = str(task.get("project_name") or "")
    priority = str(task.get("priority") or "0")
    badges = _render_trello_badges(
        ("owner", str(task.get("owner") or "")),
        ("project", project_name),
        ("priority", f"P{priority}" if priority and priority != "0" else ""),
    )
    return f"""
    <article class="kanban-card trello-card">
      <div class="trello-badges">{badges}</div>
      <div class="kanban-title trello-title">{html.escape(str(task.get('title') or 'Untitled task'))}</div>
      <p class="kanban-copy trello-copy">{html.escape(str(task.get('next_action') or 'No next action yet.'))}</p>
      {f'<p class="kanban-copy muted">{html.escape(str(task.get("blocker") or ""))}</p>' if task.get("blocker") else ''}
      {_build_task_controls(task, current_path)}
    </article>
    """


def _render_trello_capture_card(item: dict[str, Any], project_options: str, current_path: str) -> str:
    default_title = (str(item.get("raw_text") or "Captured item").strip().splitlines()[0])[:96]
    badges = _render_trello_badges(
        ("capture", str(item.get("status") or "")),
        ("capture", str(item.get("classification") or "capture")),
    )
    return f"""
    <article class="kanban-card trello-card capture-card">
      <div class="trello-badges">{badges}</div>
      <div class="kanban-title trello-title">{html.escape(default_title or 'Captured item')}</div>
      <p class="kanban-copy trello-copy">{html.escape(str(item.get('raw_text') or ''))}</p>
      <details class="card-actions">
        <summary>Convert to task</summary>
        <form class="control-form" method="post" action="/captures/{html.escape(str(item['id']))}/task">
          {_hidden_redirect_input(current_path)}
          <label class="control-label">Task title</label>
          <input type="text" name="title" value="{html.escape(default_title)}" required>
          <label class="control-label">Owner</label>
          <select name="owner">
            <option value="ATHENA">ATHENA</option>
            <option value="FLEIRE">FLEIRE</option>
          </select>
          <label class="control-label">Project</label>
          <select name="project_id">{project_options}</select>
          <button type="submit">Create task</button>
        </form>
      </details>
    </article>
    """


def _render_trello_closed_card(task: dict[str, Any], current_path: str) -> str:
    badges = _render_trello_badges(
        ("done", str(task.get("status") or "")),
        ("project", str(task.get("project_name") or "")),
    )
    return f"""
    <article class="kanban-card trello-card done-card">
      <div class="trello-badges">{badges}</div>
      <div class="kanban-title trello-title">{html.escape(str(task.get('title') or 'Closed task'))}</div>
      <p class="kanban-copy trello-copy">{html.escape(str(task.get('completion_summary') or 'Closed without summary.'))}</p>
      <details class="card-actions">
        <summary>Reopen</summary>
        {_build_reopen_form(str(task['id']), f"Follow-up needed for {task.get('title') or task['id']}", current_path)}
      </details>
    </article>
    """


def _render_board_lists(
    kanban: dict[str, list[dict[str, Any]]],
    inbox: list[dict[str, Any]],
    closed_tasks: list[dict[str, Any]],
    project_options: str,
    current_path: str,
) -> str:
    columns: list[tuple[str, str]] = []
    if inbox:
        inbox_cards = "".join(_render_trello_capture_card(item, project_options, current_path) for item in inbox)
        columns.append(("Inbox", inbox_cards))

    preferred = ["ATHENA", "FLEIRE", "BLOCKED", "SOMEDAY"]
    seen: set[str] = set()
    for bucket in preferred + sorted(kanban):
        if bucket in seen or bucket not in kanban:
            continue
        seen.add(bucket)
        cards = "".join(_render_trello_task_card(task, current_path) for task in kanban[bucket])
        columns.append((bucket.title(), cards))

    if closed_tasks:
        done_cards = "".join(_render_trello_closed_card(task, current_path) for task in closed_tasks)
        columns.append(("Done", done_cards))

    if not columns:
        return '<div class="empty-board">No lists yet.</div>'

    rendered = []
    for title, cards in columns:
        count = cards.count('class="kanban-card')
        rendered.append(
            f"""
            <section class="kanban-column trello-list">
              <div class="trello-list-header">
                <h3>{html.escape(title)}</h3>
                <span class="trello-list-count">{count}</span>
              </div>
              <div class="trello-list-body">
                {cards}
              </div>
            </section>
            """
        )
    return "".join(rendered)


def _render_project_control(projects: list[dict[str, Any]], current_path: str) -> str:
    if not projects:
        return '<p class="muted">No projects yet.</p>'
    options = _project_options(projects, include_blank=False)
    return f"""
    <form class="capture-form" method="post" action="/projects/update">
      {_hidden_redirect_input(current_path)}
      <label class="control-label">Project</label>
      <select name="project_id" required>{options}</select>
      <div class="form-grid">
        <select name="status">
          <option value="">Keep status</option>
          <option value="queued">queued</option>
          <option value="active">active</option>
          <option value="blocked">blocked</option>
          <option value="done">done</option>
          <option value="cancelled">cancelled</option>
        </select>
        <select name="health">
          <option value="">Keep health</option>
          <option value="green">green</option>
          <option value="yellow">yellow</option>
          <option value="red">red</option>
          <option value="unknown">unknown</option>
        </select>
      </div>
      <div class="form-grid">
        <input type="text" name="current_goal" placeholder="Current goal">
        <input type="text" name="next_milestone" placeholder="Next milestone">
      </div>
      <input type="text" name="blocker" placeholder="Blocker">
      <textarea name="summary" rows="3" placeholder="Project update summary"></textarea>
      <textarea name="completion_summary" rows="2" placeholder="Required only when closing a project"></textarea>
      <button type="submit">Save project update</button>
    </form>
    """


def _render_gmail_account_options(accounts: list[dict[str, Any]]) -> str:
    if not accounts:
        return '<option value="primary">Primary Gmail</option>'
    options: list[str] = []
    for account in accounts:
        label = str(account.get("label") or "primary")
        email_value = str(account.get("email") or "").strip()
        display_name = str(account.get("display_name") or label)
        selected = " selected" if bool(account.get("is_default")) else ""
        descriptor = f"{display_name} ({email_value})" if email_value else display_name
        options.append(
            f'<option value="{html.escape(label)}"{selected}>{html.escape(descriptor)}</option>'
        )
    return "".join(options)


def _render_outbox_compose_form(
    project_options: str,
    gmail_accounts: list[dict[str, Any]],
    current_path: str,
) -> str:
    account_options = _render_gmail_account_options(gmail_accounts)
    return f"""
    <form class="capture-form" method="post" action="/outbox/new">
      {_hidden_redirect_input(current_path)}
      <div class="form-grid">
        <select name="project_id">{project_options}</select>
        <input type="text" name="task_id" placeholder="Optional task id">
      </div>
      <div class="form-grid">
        <select name="account_label">{account_options}</select>
        <input type="text" name="to_recipients" placeholder="To: comma-separated emails" required>
      </div>
      <div class="form-grid">
        <input type="text" name="cc_recipients" placeholder="CC: optional">
        <input type="text" name="bcc_recipients" placeholder="BCC: optional">
      </div>
      <input type="text" name="subject" placeholder="Email subject" required>
      <textarea name="body_text" rows="5" placeholder="Draft the email Athena should queue for approval..." required></textarea>
      <button type="submit">Create Gmail draft</button>
    </form>
    """


def _render_outbox_card(item: dict[str, Any]) -> str:
    badges = _render_trello_badges(
        ("capture", str(item.get("status") or "")),
        ("project", str(item.get("project_name") or item.get("task_title") or "")),
        ("owner", str(item.get("account_label") or "")),
    )
    body_preview = str(item.get("body_text") or "").strip()
    if len(body_preview) > 220:
        body_preview = f"{body_preview[:217]}..."
    details: list[str] = []
    if item.get("to_recipients"):
        details.append(f"To: {item['to_recipients']}")
    if item.get("cc_recipients"):
        details.append(f"Cc: {item['cc_recipients']}")
    if item.get("bcc_recipients"):
        details.append(f"Bcc: {item['bcc_recipients']}")
    if item.get("draft_id"):
        details.append(f"Draft: {item['draft_id']}")
    if item.get("error_message"):
        details.append(f"Error: {item['error_message']}")
    if item.get("approval_note"):
        details.append(f"Note: {item['approval_note']}")
    detail_html = "".join(f'<p class="kanban-copy muted">{html.escape(line)}</p>' for line in details)
    external_link = ""
    if item.get("external_url"):
        external_link = f'<p class="kanban-copy muted"><a href="{html.escape(str(item["external_url"]))}" target="_blank" rel="noreferrer">Open in Gmail</a></p>'
    return f"""
    <article class="kanban-card trello-card">
      <label class="outbox-select">
        <input type="checkbox" name="outbox_id" value="{html.escape(str(item['id']))}">
        Select
      </label>
      <div class="trello-badges">{badges}</div>
      <div class="kanban-title trello-title">{html.escape(str(item.get('subject') or 'Email draft'))}</div>
      <p class="kanban-copy trello-copy">{html.escape(body_preview or 'No body yet.')}</p>
      {detail_html}
      {external_link}
    </article>
    """


def _render_outbox_panel(items: list[dict[str, Any]], current_path: str) -> str:
    if not items:
        return '<p class="muted">No email approvals queued.</p>'
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        groups.setdefault(str(item.get("status") or "unknown"), []).append(item)
    columns: list[str] = []
    for status in ("needs_approval", "approved", "sending", "error", "sent", "rejected"):
        rows = groups.get(status, [])
        if not rows:
            continue
        cards = "".join(_render_outbox_card(item) for item in rows)
        columns.append(
            f"""
            <section class="kanban-column trello-list">
              <div class="trello-list-header">
                <h3>{html.escape(status.replace('_', ' ').title())}</h3>
                <span class="trello-list-count">{len(rows)}</span>
              </div>
              <div class="trello-list-body">{cards}</div>
            </section>
            """
        )
    controls = """
    <div class="panel-actions">
      <button type="submit" name="action" value="approve">Approve selected</button>
      <button type="submit" name="action" value="reject">Reject selected</button>
      <button type="submit" name="action" value="send">Send approved / selected</button>
      <input type="text" name="note" placeholder="Optional approval note">
    </div>
    """
    return f"""
    <form class="outbox-batch-form" method="post" action="/outbox/batch">
      {_hidden_redirect_input(current_path)}
      <div class="trello-board outbox-board">
        {"".join(columns)}
      </div>
      {controls}
    </form>
    """


def _truncate(text: str, limit: int = 220) -> str:
    clean = text.strip()
    if len(clean) <= limit:
        return clean
    return f"{clean[:limit - 3].rstrip()}..."


def _render_preview_list(
    items: list[dict[str, Any]],
    *,
    title_key: str,
    meta_keys: list[str],
    body_key: str | None = None,
    empty_message: str = "No records yet.",
    limit: int = 6,
) -> str:
    if not items:
        return f'<p class="muted">{html.escape(empty_message)}</p>'
    cards: list[str] = []
    for item in items[:limit]:
        title = str(item.get(title_key) or item.get("title") or item.get("id") or "Untitled")
        meta = " · ".join(str(item.get(key)) for key in meta_keys if item.get(key))
        body = str(item.get(body_key) or "").strip() if body_key else ""
        cards.append(
            f"""
            <article class="preview-item">
              <div class="preview-title-row">
                <strong>{html.escape(title)}</strong>
                {f'<span class="preview-meta">{html.escape(meta)}</span>' if meta else ''}
              </div>
              {f'<p>{html.escape(_truncate(body, 180))}</p>' if body else ''}
            </article>
            """
        )
    return "".join(cards)


def _render_section_card(
    title: str,
    description: str,
    body: str,
    *,
    actions: str = "",
    extra_class: str = "",
) -> str:
    card_class = "section-card"
    if extra_class:
        card_class = f"{card_class} {extra_class}"
    return f"""
    <section class="{html.escape(card_class)}">
      <div class="section-heading">
        <div>
          <h2>{html.escape(title)}</h2>
          <p>{html.escape(description)}</p>
        </div>
        {f'<div class="section-actions">{actions}</div>' if actions else ''}
      </div>
      <div class="section-body">
        {body}
      </div>
    </section>
    """


def _render_board_page(
    data: dict[str, Any],
    *,
    current_path: str,
    project_options: str,
) -> str:
    board_html = _render_board_lists(
        data.get("kanban", {}),
        data.get("inbox", []),
        data.get("closed_tasks", []),
        project_options,
        current_path,
    )
    capture_html = _render_quick_capture_form(current_path)
    stats_html = _build_stat_grid(
        {
            "open_tasks": data["dashboard"].get("counts", {}).get("open_tasks", 0),
            "in_progress_tasks": data["dashboard"].get("counts", {}).get("in_progress_tasks", 0),
            "queued_tasks": data["dashboard"].get("counts", {}).get("queued_tasks", 0),
            "blocked_tasks": data["dashboard"].get("counts", {}).get("blocked_tasks", 0),
            "closed_tasks": data["dashboard"].get("counts", {}).get("closed_tasks", len(data.get("closed_tasks", []))),
            "new_items": data["dashboard"].get("inbox", {}).get("new_items", 0),
            "triaged_items": data["dashboard"].get("inbox", {}).get("triaged_items", 0),
            "outbox_needs_approval": data["dashboard"].get("outbox", {}).get("outbox_needs_approval", 0),
            "outbox_approved": data["dashboard"].get("outbox", {}).get("outbox_approved", 0),
        }
    )
    sidebar_body = f"""
    <div class="stack-list">
      <div class="mini-stat-block">
        {stats_html}
      </div>
      <div class="control-stack">
        <h3>Quick capture</h3>
        {capture_html}
      </div>
      <div class="control-stack">
        <h3>Sync and reviews</h3>
        <div class="sync-controls">
          {_render_sync_controls(current_path)}
        </div>
        <div class="sync-controls">
          {_render_review_controls(current_path)}
        </div>
      </div>
    </div>
    """
    return f"""
    <div class="page-grid board-layout">
      <div class="primary-column">
        {_render_section_card("Active board", "Kanban lists for capture, work in progress, blockers, and done.", f'<div class="trello-board">{board_html}</div>', actions='<a class="section-link" href="/inbox">Open inbox</a>')}
      </div>
      <div class="secondary-column">
        {_render_section_card("Control rail", "Use this side to add new work and keep the board fresh.", sidebar_body, extra_class="compact-card")}
      </div>
    </div>
    """


def _render_inbox_page(
    data: dict[str, Any],
    *,
    current_path: str,
    project_options: str,
) -> str:
    inbox_items = data.get("inbox", [])
    inbox_cards = "".join(
        _render_trello_capture_card(item, project_options, current_path) for item in inbox_items
    ) or '<p class="muted">Inbox is clear.</p>'
    queue_preview = _render_preview_list(
        data.get("tasks", []),
        title_key="title",
        meta_keys=["bucket", "status", "project_name"],
        body_key="next_action",
        empty_message="No active tasks yet.",
        limit=6,
    )
    summary_body = f"""
    <div class="info-grid">
      <div class="info-pill">
        <span>New captures</span>
        <strong>{html.escape(str(data['dashboard'].get('inbox', {}).get('new_items', 0)))}</strong>
      </div>
      <div class="info-pill">
        <span>Triaged</span>
        <strong>{html.escape(str(data['dashboard'].get('inbox', {}).get('triaged_items', 0)))}</strong>
      </div>
    </div>
    <div class="control-stack">
      <h3>Capture something new</h3>
      {_render_quick_capture_form(current_path)}
    </div>
    """
    return f"""
    <div class="page-grid">
      <div class="primary-column">
        {_render_section_card("Inbox triage", "Raw captures stay here until Athena or Fleire turns them into work.", f'<div class="card-grid">{inbox_cards}</div>')}
      </div>
      <div class="secondary-column">
        {_render_section_card("Intake health", "Keep inbox light and convert anything real into a task.", summary_body, actions='<a class="section-link" href="/board">Go to board</a>', extra_class="compact-card")}
        {_render_section_card("Open tasks", "A quick check of what already exists before you create more work.", queue_preview, extra_class="compact-card")}
      </div>
    </div>
    """


def _render_outbox_page(
    data: dict[str, Any],
    *,
    current_path: str,
    project_options: str,
) -> str:
    compose_html = _render_outbox_compose_form(project_options, data.get("gmail_accounts", []), current_path)
    outbox_html = _render_outbox_panel(data.get("outbox", []), current_path)
    preview_html = _render_preview_list(
        data.get("outbox", []),
        title_key="subject",
        meta_keys=["status", "project_name", "to_recipients"],
        body_key="body_text",
        empty_message="No drafts waiting for approval.",
        limit=5,
    )
    return f"""
    <div class="page-grid">
      <div class="secondary-column">
        {_render_section_card("Compose", "Draft the email here. Athena will queue it for explicit approval.", compose_html, extra_class="compact-card")}
        {_render_section_card("Approval snapshot", "What is currently waiting on you, already approved, or sent.", preview_html, extra_class="compact-card")}
      </div>
      <div class="primary-column">
        {_render_section_card("Outbox workflow", "Batch approve, reject, and send from one place.", outbox_html, actions='<a class="section-link" href="/board">Back to board</a>')}
      </div>
    </div>
    """


def _render_projects_page(data: dict[str, Any], *, current_path: str) -> str:
    projects_html = _render_items(
        data["projects"],
        [
            "name",
            "portfolio_name",
            "status",
            "health",
            "derived_status",
            "derived_health",
            "current_goal",
            "next_milestone",
            "blocker",
            "rollup_summary",
            "completion_summary",
        ],
    )
    repo_html = _render_items(
        data["repos"],
        ["repo_name", "project_name", "last_seen_branch", "last_seen_dirty", "last_scanned_at"],
    )
    project_preview = _render_preview_list(
        data.get("projects", []),
        title_key="name",
        meta_keys=["portfolio_name", "health", "status"],
        body_key="current_goal",
        empty_message="No projects yet.",
        limit=6,
    )
    project_control_html = _render_project_control(data.get("projects", []), current_path)
    return f"""
    <div class="page-grid">
      <div class="secondary-column">
        {_render_section_card("Update a project", "Record status, health, blockers, or milestones without leaving the board.", project_control_html, extra_class="compact-card")}
        {_render_section_card("Portfolio snapshot", "High-level project health across the current portfolio.", project_preview, extra_class="compact-card")}
      </div>
      <div class="primary-column">
        {_render_section_card("Projects", "The authoritative operating view for the projects Athena is tracking.", projects_html)}
        {_render_section_card("Repos", "Codebase signals support project status. They do not replace it.", repo_html)}
      </div>
    </div>
    """


def _render_briefs_page(data: dict[str, Any], *, current_path: str) -> str:
    weekly = data.get("weekly_briefs") or {}
    latest = weekly.get("latest") or {}
    latest_content = str(latest.get("content") or "").strip()
    latest_body = (
        f"""
        <div class="focus-block">
          <div class="focus-row">
            <span>Generated</span>
            <strong>{html.escape(str(latest.get('generated_at_formatted') or 'Unknown'))}</strong>
          </div>
          <div class="focus-row">
            <span>Summary</span>
            <strong>{html.escape(str(latest.get('summary') or 'No summary recorded.'))}</strong>
          </div>
          <div class="focus-row">
            <span>Path</span>
            <strong>{html.escape(str(latest.get('path') or 'No file path'))}</strong>
          </div>
        </div>
        <pre class="document-reader">{html.escape(latest_content)}</pre>
        """
        if latest
        else '<p class="muted">No weekly CEO brief has been generated yet.</p>'
    )
    history = _render_preview_list(
        weekly.get("items", []),
        title_key="title",
        meta_keys=["generated_at_formatted"],
        body_key="summary",
        empty_message="No synthesis history yet.",
        limit=10,
    )
    actions = f"""
    <div class="sync-controls">
      {_render_sync_controls(current_path)}
    </div>
    <div class="sync-controls">
      {_render_review_controls(current_path)}
    </div>
    """
    return f"""
    <div class="page-grid">
      <div class="primary-column">
        {_render_section_card("Latest CEO brief", "A weekly founder packet grounded in Athena's local life, portfolio, task, approval, and calendar state.", latest_body)}
      </div>
      <div class="secondary-column">
        {_render_section_card("Generate and refresh", "Run the brief directly or refresh the weekly review that also regenerates it.", actions, extra_class="compact-card")}
        {_render_section_card("Recent synthesis history", "Past weekly packets stay visible instead of vanishing into chat.", history, extra_class="compact-card")}
      </div>
    </div>
    """


def _render_context_page(data: dict[str, Any], *, current_path: str) -> str:
    life_parts = _render_items(data["life"]["areas"], ["name", "status", "priority", "notes"])
    life_goals = _render_items(data["life"]["goals"], ["title", "status", "horizon", "current_focus"])
    life_people = _render_items(data["life"]["people"], ["name", "relationship_type", "importance_score", "contact_rule"])
    source_rows = _render_items(data["sources"], ["title", "kind", "is_authoritative", "source_system"])
    brief_rows = _render_items(data["briefs"], ["scope_kind", "scope_id", "brief_type", "content"])
    chat = data.get("chat") or {}
    chat_html = (
        "".join(
            f'<div class="item-field"><strong>{html.escape(key)}:</strong> {html.escape(str(value))}</div>'
            for key, value in chat.items()
            if value
        )
        or '<p class="muted">No active chat state.</p>'
    )
    sync_html = f"""
    <div class="sync-controls">
      {_render_sync_controls(current_path)}
    </div>
    <div class="sync-controls">
      {_render_review_controls(current_path)}
    </div>
    """
    life_body = f"""
    <div class="life-columns">
      <article>
        <h3>Areas</h3>
        {life_parts}
      </article>
      <article>
        <h3>Goals</h3>
        {life_goals}
      </article>
      <article>
        <h3>People</h3>
        {life_people}
      </article>
    </div>
    """
    return f"""
    <div class="page-grid">
      <div class="primary-column">
        {_render_section_card("Life system", "The rules, goals, and people Athena uses to hold the larger picture.", life_body)}
        {_render_section_card("Source documents", "Authoritative local context plus mirrored sources from the outside world.", source_rows)}
        {_render_section_card("Awareness briefs", "Small, cheap summaries Athena can load fast in chat.", brief_rows)}
      </div>
      <div class="secondary-column">
        {_render_section_card("Current chat state", "What the Telegram-facing agent currently believes it is doing.", chat_html, extra_class="compact-card")}
        {_render_section_card("Refresh context", "Pull in new mirrored context and regenerate summaries.", sync_html, extra_class="compact-card")}
      </div>
    </div>
    """


def _render_overview_page(data: dict[str, Any], *, current_path: str) -> str:
    raw_counts = data["dashboard"].get("counts", {})
    inbox_counts = data["dashboard"].get("inbox", {})
    outbox_counts = data["dashboard"].get("outbox", {})
    stats_html = _build_stat_grid(
        {
            "open_tasks": raw_counts.get("open_tasks", 0),
            "in_progress_tasks": raw_counts.get("in_progress_tasks", 0),
            "queued_tasks": raw_counts.get("queued_tasks", 0),
            "blocked_tasks": raw_counts.get("blocked_tasks", 0),
            "closed_tasks": raw_counts.get("closed_tasks", len(data.get("closed_tasks", []))),
            "new_items": inbox_counts.get("new_items", 0),
            "triaged_items": inbox_counts.get("triaged_items", 0),
            "outbox_needs_approval": outbox_counts.get("outbox_needs_approval", 0),
            "outbox_approved": outbox_counts.get("outbox_approved", 0),
        }
    )
    chat = data.get("chat") or {}
    focus_body = f"""
    <div class="focus-block">
      <div class="focus-row">
        <span>Current task</span>
        <strong>{html.escape(str(chat.get('current_task_id') or 'No active task'))}</strong>
      </div>
      <div class="focus-row">
        <span>Project</span>
        <strong>{html.escape(str(chat.get('current_project_name') or 'No active project'))}</strong>
      </div>
      <div class="focus-row">
        <span>Intent</span>
        <strong>{html.escape(str(chat.get('last_user_intent') or 'No recent intent'))}</strong>
      </div>
      <div class="focus-row">
        <span>Progress</span>
        <strong>{html.escape(str(chat.get('last_progress') or 'No recent progress note'))}</strong>
      </div>
    </div>
    <div class="inline-links">
      <a class="section-link" href="/board">Open board</a>
      <a class="section-link" href="/context">Open context</a>
    </div>
    """
    ops_body = f"""
    <div class="control-stack">
      <h3>Sync</h3>
      <div class="sync-controls">
        {_render_sync_controls(current_path)}
      </div>
    </div>
    <div class="control-stack">
      <h3>Reviews</h3>
      <div class="sync-controls">
        {_render_review_controls(current_path)}
      </div>
    </div>
    """
    tasks_preview = _render_preview_list(
        data.get("tasks", []),
        title_key="title",
        meta_keys=["bucket", "status", "project_name"],
        body_key="next_action",
        empty_message="No open tasks.",
    )
    outbox_preview = _render_preview_list(
        data.get("outbox", []),
        title_key="subject",
        meta_keys=["status", "to_recipients"],
        body_key="body_text",
        empty_message="No email approvals queued.",
    )
    project_preview = _render_preview_list(
        data.get("projects", []),
        title_key="name",
        meta_keys=["portfolio_name", "health", "status"],
        body_key="next_milestone",
        empty_message="No projects yet.",
    )
    weekly_brief_preview = _render_preview_list(
        (data.get("weekly_briefs") or {}).get("items", []),
        title_key="title",
        meta_keys=["generated_at_formatted"],
        body_key="summary",
        empty_message="No weekly CEO brief yet.",
        limit=3,
    )
    context_preview = _render_preview_list(
        data.get("sources", []),
        title_key="title",
        meta_keys=["kind", "source_system"],
        empty_message="No source documents yet.",
    )
    return f"""
    <div class="overview-grid">
      {_render_section_card("Current state", "Counts across the life, project, and execution layer.", stats_html, extra_class="compact-card")}
      {_render_section_card("Today", "What Athena thinks matters right now in the active chat.", focus_body, extra_class="compact-card")}
      {_render_section_card("Operations", "Sync fresh context and run review cadences without leaving the app.", ops_body, extra_class="compact-card")}
      {_render_section_card("Immediate work", "Top open tasks across the system.", tasks_preview, actions='<a class="section-link" href="/board">See full board</a>')}
      {_render_section_card("Email approvals", "What is waiting for review or ready to send.", outbox_preview, actions='<a class="section-link" href="/outbox">Open outbox</a>')}
      {_render_section_card("Portfolio health", "Where projects stand across the current portfolio.", project_preview, actions='<a class="section-link" href="/projects">Open projects</a>')}
      {_render_section_card("CEO weekly brief", "The latest founder-facing synthesis Athena generated from the local operating system.", weekly_brief_preview, actions='<a class="section-link" href="/briefs">Open briefs</a>')}
      {_render_section_card("Context sources", "Local truth and mirrored documents Athena can actually rely on.", context_preview, actions='<a class="section-link" href="/context">Open context</a>')}
    </div>
    """


def _render_page_body(data: dict[str, Any], *, current_path: str) -> str:
    project_options = _project_options(data.get("projects", []))
    if current_path == "/board":
        return _render_board_page(data, current_path=current_path, project_options=project_options)
    if current_path == "/inbox":
        return _render_inbox_page(data, current_path=current_path, project_options=project_options)
    if current_path == "/outbox":
        return _render_outbox_page(data, current_path=current_path, project_options=project_options)
    if current_path == "/projects":
        return _render_projects_page(data, current_path=current_path)
    if current_path == "/briefs":
        return _render_briefs_page(data, current_path=current_path)
    if current_path == "/context":
        return _render_context_page(data, current_path=current_path)
    return _render_overview_page(data, current_path=current_path)


def _render_html(
    data: dict[str, Any],
    *,
    current_path: str = "/",
    banner_message: str | None = None,
    banner_kind: str = "ok",
) -> str:
    safe_path = _safe_page_route(current_path)
    page = APP_PAGE_LOOKUP[safe_path]
    raw_counts = data["dashboard"].get("counts", {})
    inbox_counts = data["dashboard"].get("inbox", {})
    outbox_counts = data["dashboard"].get("outbox", {})
    banner = f'<div class="banner {html.escape(banner_kind)}">{html.escape(banner_message)}</div>' if banner_message else ""
    topline = " · ".join(
        [
            f"{raw_counts.get('open_tasks', 0)} open tasks",
            f"{outbox_counts.get('outbox_needs_approval', 0)} approvals waiting",
            f"{inbox_counts.get('new_items', 0)} inbox captures",
        ]
    )
    page_body = _render_page_body(data, current_path=safe_path)

    return f"""<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{html.escape(str(page["label"]))} · Athena OS</title>
    <link rel="stylesheet" href="/static/style.css">
  </head>
  <body>
    <div class="app-shell">
      {_render_sidebar_nav(safe_path, data)}
      <main class="app-main">
        <div class="topline">{html.escape(topline)}</div>
        {_render_page_header(safe_path)}
        {banner}
        <div class="page-content">
          {page_body}
        </div>
      </main>
    </div>
  </body>
</html>"""


class AthenaHandler(BaseHTTPRequestHandler):
    server_version = "AthenaBoard/0.1"

    def _send_html(self, content: str) -> None:
        encoded = content.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _handle_static(self, paths: AthenaPaths, rel_path: str) -> None:
        asset = paths.repo_root / "athena/static" / rel_path.strip("/")
        if not asset.exists() or not asset.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content = asset.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/css")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _read_form(self) -> dict[str, str]:
        raw_length = self.headers.get("Content-Length", "0")
        length = int(raw_length or "0")
        body = self.rfile.read(length).decode("utf-8") if length > 0 else ""
        parsed = parse_qs(body, keep_blank_values=True)
        return {key: values[-1] for key, values in parsed.items()}

    def _read_form_multimap(self) -> dict[str, list[str]]:
        raw_length = self.headers.get("Content-Length", "0")
        length = int(raw_length or "0")
        body = self.rfile.read(length).decode("utf-8") if length > 0 else ""
        return parse_qs(body, keep_blank_values=True)

    def _redirect_with_notice(self, redirect_to: str | None, notice: str, kind: str = "ok") -> None:
        route = _safe_page_route(redirect_to)
        query = urlencode({"notice": notice, "kind": kind})
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", f"{route}?{query}")
        self.end_headers()

    def _form_value(self, params: dict[str, Any], key: str) -> str | None:
        value = params.get(key)
        if isinstance(value, list):
            return value[-1] if value else None
        if isinstance(value, str):
            return value
        return None

    def _redirect_from_params(self, params: dict[str, Any], notice: str, kind: str = "ok") -> None:
        self._redirect_with_notice(self._form_value(params, "redirect_to"), notice, kind)

    def _optional(self, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    def _evidence_items(self, value: str | None) -> list[str]:
        if value is None:
            return []
        return [line.strip() for line in value.splitlines() if line.strip()]

    def _run_sync_command(self, paths: AthenaPaths, command: str, *, as_json: bool) -> None:
        if command not in {"all", "google", "life", "repos", "briefs", "weekly-brief"}:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        summary = run_sync(command, paths=paths)
        if as_json:
            _json_response(self, summary)
            return
        self._redirect_with_notice("/", f"Sync complete: {command}")

    def _handle_task_action(
        self,
        paths: AthenaPaths,
        task_id: str,
        action: str,
        params: dict[str, str],
    ) -> dict[str, Any]:
        actor = "board"
        if action == "start":
            return state_module.start_task(
                task_id=task_id,
                db_path=paths.db_path,
                actor=actor,
                next_action=self._optional(params.get("next_action")),
                note=self._optional(params.get("note")),
            )
        if action == "block":
            blocker = self._optional(params.get("blocker"))
            if blocker is None:
                raise ValueError("blocker is required")
            return state_module.block_task(
                task_id=task_id,
                db_path=paths.db_path,
                actor=actor,
                blocker=blocker,
                next_action=self._optional(params.get("next_action")),
                note=self._optional(params.get("note")),
            )
        if action == "complete":
            summary = self._optional(params.get("summary"))
            if summary is None:
                raise ValueError("summary is required")
            evidence = self._evidence_items(params.get("evidence"))
            return state_module.complete_task(
                task_id=task_id,
                db_path=paths.db_path,
                actor=actor,
                summary=summary,
                evidence=evidence,
                verified_by=self._optional(params.get("verified_by")),
                note=self._optional(params.get("note")),
            )
        if action == "reopen":
            reason = self._optional(params.get("reason"))
            if reason is None:
                raise ValueError("reason is required")
            return state_module.reopen_task(
                task_id=task_id,
                db_path=paths.db_path,
                actor=actor,
                reason=reason,
                next_action=self._optional(params.get("next_action")),
            )
        raise ValueError(f"unknown action {action}")

    def _handle_new_capture(self, paths: AthenaPaths, params: dict[str, str]) -> dict[str, Any]:
        raw_text = self._optional(params.get("raw_text"))
        if raw_text is None:
            raise ValueError("raw_text is required")
        return state_module.capture_item(
            db_path=paths.db_path,
            raw_text=raw_text,
            source_channel="board",
            source_chat_id="local-board",
            classification=self._optional(params.get("classification")),
            note=self._optional(params.get("note")),
        )

    def _handle_capture_to_task(
        self,
        paths: AthenaPaths,
        capture_id: str,
        params: dict[str, str],
    ) -> dict[str, Any]:
        title = self._optional(params.get("title"))
        owner = self._optional(params.get("owner"))
        if title is None or owner is None:
            raise ValueError("title and owner are required")
        with connect_db(paths.db_path) as conn:
            capture = query_one(conn, "SELECT * FROM captured_items WHERE id = ?", (capture_id,))
            if capture is None:
                raise ValueError(f"capture not found: {capture_id}")
            project_id = self._optional(params.get("project_id"))
            portfolio_id = None
            if project_id is not None:
                project = query_one(conn, "SELECT portfolio_id FROM projects WHERE id = ?", (project_id,))
                portfolio_id = project["portfolio_id"] if project else None
        return state_module.create_task(
            db_path=paths.db_path,
            title=title,
            owner=owner,
            capture_id=capture_id,
            project_id=project_id,
            portfolio_id=portfolio_id,
            source_text=str(capture["raw_text"]),
            source_channel=str(capture["source_channel"] or "board"),
            source_chat_id=str(capture["source_chat_id"] or "local-board"),
            source_message_ref=str(capture["source_message_ref"] or capture_id),
            dedupe_key=f"capture-task:{capture_id}",
            actor="board",
        )

    def _handle_project_update(self, paths: AthenaPaths, params: dict[str, str]) -> dict[str, Any]:
        project_id = self._optional(params.get("project_id"))
        if project_id is None:
            raise ValueError("project_id is required")
        return state_module.update_project_status(
            db_path=paths.db_path,
            project_id=project_id,
            actor="board",
            status=self._optional(params.get("status")),
            health=self._optional(params.get("health")),
            blocker=self._optional(params.get("blocker")),
            current_goal=self._optional(params.get("current_goal")),
            next_milestone=self._optional(params.get("next_milestone")),
            summary=self._optional(params.get("summary")),
            completion_summary=self._optional(params.get("completion_summary")),
        )

    def _handle_new_outbox(self, paths: AthenaPaths, params: dict[str, str]) -> dict[str, Any]:
        to_recipients = self._optional(params.get("to_recipients"))
        subject = self._optional(params.get("subject"))
        body_text = self._optional(params.get("body_text"))
        if to_recipients is None or subject is None or body_text is None:
            raise ValueError("to_recipients, subject, and body_text are required")
        return outbox_module.create_email_outbox(
            db_path=paths.db_path,
            paths=paths,
            to_recipients=to_recipients,
            cc_recipients=self._optional(params.get("cc_recipients")),
            bcc_recipients=self._optional(params.get("bcc_recipients")),
            task_id=self._optional(params.get("task_id")),
            project_id=self._optional(params.get("project_id")),
            account_label=self._optional(params.get("account_label")),
            subject=subject,
            body_text=body_text,
            actor="board",
        )

    def _handle_outbox_batch(self, paths: AthenaPaths, params: dict[str, list[str]]) -> dict[str, Any]:
        action = (params.get("action") or [""])[-1].strip()
        note = self._optional((params.get("note") or [""])[-1])
        outbox_ids = [item.strip() for item in params.get("outbox_id", []) if item.strip()]
        if action == "approve":
            return outbox_module.approve_outbox_items(
                db_path=paths.db_path,
                outbox_ids=outbox_ids,
                actor="board",
                note=note,
            )
        if action == "reject":
            return outbox_module.reject_outbox_items(
                db_path=paths.db_path,
                outbox_ids=outbox_ids,
                actor="board",
                note=note,
            )
        if action == "send":
            return outbox_module.send_outbox_items(
                db_path=paths.db_path,
                paths=paths,
                outbox_ids=outbox_ids or None,
                actor="board",
            )
        raise ValueError("action must be one of approve, reject, or send")

    def do_GET(self) -> None:
        paths = self.server.athena_state.paths  # type: ignore[attr-defined]
        parsed = urlsplit(self.path)
        route = parsed.path
        query = parse_qs(parsed.query)
        if route in APP_PAGE_LOOKUP:
            data = _gather_data(paths)
            banner_message = query.get("notice", [query.get("synced", [None])[0]])[0]
            banner_kind = query.get("kind", ["ok"])[0]
            self._send_html(
                _render_html(
                    data,
                    current_path=route,
                    banner_message=banner_message,
                    banner_kind=banner_kind,
                )
            )
            return
        if route.startswith("/api/"):
            data = _gather_data(paths)
            endpoint = route.removeprefix("/api/")
            if endpoint == "dashboard":
                _json_response(self, data["dashboard"])
            elif endpoint == "tasks":
                _json_response(self, data["tasks"])
            elif endpoint == "projects":
                _json_response(self, data["projects"])
            elif endpoint == "repos":
                _json_response(self, data["repos"])
            elif endpoint == "life":
                _json_response(self, data["life"])
            elif endpoint == "sources":
                _json_response(self, data["sources"])
            elif endpoint == "briefs":
                _json_response(self, data["briefs"])
            elif endpoint == "weekly-briefs":
                _json_response(self, data["weekly_briefs"])
            elif endpoint == "inbox":
                _json_response(self, data["inbox"])
            elif endpoint == "kanban":
                _json_response(self, data["kanban"])
            elif endpoint == "closed-tasks":
                _json_response(self, data["closed_tasks"])
            elif endpoint == "outbox":
                _json_response(self, data["outbox"])
            elif endpoint == "chat":
                _json_response(self, data["chat"])
            elif endpoint == "health":
                _json_response(self, {"ok": True, "db": str(paths.db_path)})
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
            return
        if route.startswith("/static/"):
            self._handle_static(paths, route[len("/static/"):])
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        paths = self.server.athena_state.paths  # type: ignore[attr-defined]
        parsed = urlsplit(self.path)
        route = parsed.path
        if route.startswith("/api/sync/"):
            self._run_sync_command(paths, route.removeprefix("/api/sync/"), as_json=True)
            return
        if route.startswith("/sync/"):
            params = self._read_form()
            command = route.removeprefix("/sync/")
            try:
                run_sync(command, paths=paths)
                self._redirect_from_params(params, f"Sync complete: {command}")
            except Exception as exc:
                self._redirect_from_params(params, f"Sync failed: {exc}", kind="error")
            return
        if route == "/captures/new":
            params = self._read_form()
            try:
                self._handle_new_capture(paths, params)
                self._redirect_from_params(params, "Captured into inbox")
            except Exception as exc:
                self._redirect_from_params(params, f"Capture failed: {exc}", kind="error")
            return
        if route == "/projects/update":
            params = self._read_form()
            try:
                self._handle_project_update(paths, params)
                self._redirect_from_params(params, "Project updated")
            except Exception as exc:
                self._redirect_from_params(params, f"Project update failed: {exc}", kind="error")
            return
        if route == "/outbox/new":
            params = self._read_form()
            try:
                self._handle_new_outbox(paths, params)
                self._redirect_from_params(params, "Draft queued for approval")
            except Exception as exc:
                self._redirect_from_params(params, f"Outbox draft failed: {exc}", kind="error")
            return
        if route.startswith("/api/projects/update"):
            params = self._read_form()
            try:
                _json_response(self, self._handle_project_update(paths, params))
            except Exception as exc:
                self.send_error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        if route == "/outbox/batch":
            params = self._read_form_multimap()
            try:
                result = self._handle_outbox_batch(paths, params)
                notice = "Outbox updated"
                if result.get("sent_count"):
                    notice = f"Sent {result['sent_count']} email(s)"
                self._redirect_from_params(params, notice)
            except Exception as exc:
                self._redirect_from_params(params, f"Outbox action failed: {exc}", kind="error")
            return
        if route == "/api/outbox/new":
            params = self._read_form()
            try:
                _json_response(self, self._handle_new_outbox(paths, params))
            except Exception as exc:
                self.send_error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        if route == "/api/outbox/batch":
            params = self._read_form_multimap()
            try:
                _json_response(self, self._handle_outbox_batch(paths, params))
            except Exception as exc:
                self.send_error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        if route.startswith("/reviews/"):
            params = self._read_form()
            cadence = route.removeprefix("/reviews/")
            try:
                run_review_cycle(cadence, db_path=paths.db_path, actor="board")
                self._redirect_from_params(params, f"Review complete: {cadence}")
            except Exception as exc:
                self._redirect_from_params(params, f"Review failed: {exc}", kind="error")
            return
        if route.startswith("/api/reviews/"):
            cadence = route.removeprefix("/api/reviews/")
            try:
                _json_response(self, run_review_cycle(cadence, db_path=paths.db_path, actor="board"))
            except Exception as exc:
                self.send_error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        if route.startswith("/captures/"):
            parts = route.strip("/").split("/")
            if len(parts) == 3 and parts[2] == "task":
                params = self._read_form()
                try:
                    result = self._handle_capture_to_task(paths, parts[1], params)
                    self._redirect_from_params(params, f"Created task {result['id']}")
                except Exception as exc:
                    self._redirect_from_params(params, f"Capture conversion failed: {exc}", kind="error")
                return
        if route.startswith("/api/captures/"):
            parts = route.removeprefix("/api/captures/").split("/")
            if len(parts) == 2 and parts[1] == "task":
                params = self._read_form()
                try:
                    _json_response(self, self._handle_capture_to_task(paths, parts[0], params))
                except Exception as exc:
                    self.send_error(HTTPStatus.BAD_REQUEST, str(exc))
                return
        if route.startswith("/tasks/"):
            parts = route.strip("/").split("/")
            if len(parts) == 3:
                params = self._read_form()
                try:
                    self._handle_task_action(paths, parts[1], parts[2], params)
                    self._redirect_from_params(params, f"Task updated: {parts[2]}")
                except Exception as exc:
                    self._redirect_from_params(params, f"Task action failed: {exc}", kind="error")
                return
        if route.startswith("/api/tasks/"):
            parts = route.removeprefix("/api/tasks/").split("/")
            if len(parts) == 2:
                params = self._read_form()
                try:
                    _json_response(self, self._handle_task_action(paths, parts[0], parts[1], params))
                except Exception as exc:
                    self.send_error(HTTPStatus.BAD_REQUEST, str(exc))
                return
        self.send_error(HTTPStatus.NOT_FOUND)


def create_server(host: str, port: int, paths: AthenaPaths | None = None) -> ThreadingHTTPServer:
    resolved_paths = paths or default_paths()
    ensure_db(paths=resolved_paths)
    server_state = ServerState(paths=resolved_paths)
    httpd = ThreadingHTTPServer((host, port), AthenaHandler)
    httpd.athena_state = server_state  # type: ignore[attr-defined]
    return httpd


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Athena local board server.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    httpd = create_server(args.host, args.port)
    print(f"Serving Athena board on http://{args.host}:{args.port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("Shutting down Athena board")
    finally:
        httpd.shutdown()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
