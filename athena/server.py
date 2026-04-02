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
from .reviews import run_review_cycle
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


def _gather_data(paths: AthenaPaths) -> dict[str, Any]:
    ensure_db(paths=paths)
    with connect_db(paths.db_path) as conn:
        return {
            "dashboard": dashboard_snapshot(paths.db_path),
            "life": _life_context(conn),
            "inbox": _capture_data(conn),
            "kanban": _kanban_data(conn),
            "closed_tasks": _closed_task_data(conn),
            "projects": _project_data(conn),
            "repos": _repo_data(conn),
            "tasks": _task_data(conn),
            "sources": _source_documents(conn),
            "briefs": _awareness_briefs(conn),
            "chat": _chat_context(conn),
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


def _render_sync_controls() -> str:
    actions = [
        ("all", "Sync all context"),
        ("life", "Sync life"),
        ("repos", "Scan repos"),
        ("briefs", "Refresh briefs"),
    ]
    forms = []
    for command, label in actions:
        forms.append(
            f"""
            <form class="sync-control" method="post" action="/sync/{command}">
              <button type="submit">{html.escape(label)}</button>
            </form>
            """
        )
    return "".join(forms)


def _render_review_controls() -> str:
    buttons = []
    for cadence, label in (
        ("daily", "Run daily review"),
        ("weekly", "Run weekly review"),
        ("monthly", "Run monthly review"),
    ):
        buttons.append(
            f"""
            <form class="sync-control" method="post" action="/reviews/{cadence}">
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


def _build_task_action_form(task_id: str, action: str, label: str, body: str) -> str:
    return f"""
    <form class="control-form" method="post" action="/tasks/{task_id}/{action}">
      {body}
      <button type="submit">{html.escape(label)}</button>
    </form>
    """


def _build_start_form(task_id: str, next_action: str) -> str:
    return _build_task_action_form(
        task_id,
        "start",
        "Start",
        f"""
        <label class="control-label">Next action</label>
        <input type="text" name="next_action" value="{html.escape(next_action)}" placeholder="What happens next?">
        <p class="control-desc muted">Move task into active execution.</p>
        """,
    )


def _build_complete_form(task_id: str, summary: str) -> str:
    return _build_task_action_form(
        task_id,
        "complete",
        "Complete",
        f"""
        <label class="control-label">Completion note</label>
        <input type="text" name="summary" value="{html.escape(summary)}" placeholder="What shipped or was decided?" required>
        <p class="control-desc muted">Completion always records evidence.</p>
        """,
    )


def _build_block_form(task_id: str, blocker: str, next_action: str) -> str:
    return f"""
    <form class="control-form" method="post" action="/tasks/{task_id}/block">
      <label class="control-label">Blocker</label>
      <input type="text" name="blocker" value="{html.escape(blocker)}" placeholder="Describe the blocker" required>
      <label class="control-label">Next action</label>
      <input type="text" name="next_action" value="{html.escape(next_action)}" placeholder="How do we unblock it?">
      <button type="submit">Block</button>
      <p class="control-desc muted">Capture what is preventing progress.</p>
    </form>
    """


def _build_reopen_form(task_id: str, reason: str) -> str:
    return _build_task_action_form(
        task_id,
        "reopen",
        "Reopen",
        f"""
        <label class="control-label">Reason</label>
        <input type="text" name="reason" value="{html.escape(reason)}" placeholder="Why is this back open?" required>
        <p class="control-desc muted">Clear the old completion and bring the task back.</p>
        """,
    )


def _build_task_controls(task: dict[str, Any]) -> str:
    task_id = str(task["task_id"])
    title = str(task.get("title") or task_id)
    next_action = str(task.get("next_action") or "")
    blocker = str(task.get("blocker") or "")
    actions = "".join(
        [
            _build_start_form(task_id, next_action),
            _build_complete_form(task_id, f"Board completion: {title}"),
            _build_block_form(task_id, blocker, next_action),
        ]
    )
    return f"""
    <details class="card-actions">
      <summary>Actions</summary>
      <div class="kanban-controls">{actions}</div>
    </details>
    """


def _render_quick_capture_form() -> str:
    return """
    <form class="capture-form" method="post" action="/captures/new">
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


def _render_trello_task_card(task: dict[str, Any]) -> str:
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
      {_build_task_controls(task)}
    </article>
    """


def _render_trello_capture_card(item: dict[str, Any], project_options: str) -> str:
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


def _render_trello_closed_card(task: dict[str, Any]) -> str:
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
        {_build_reopen_form(str(task['id']), f"Follow-up needed for {task.get('title') or task['id']}")}
      </details>
    </article>
    """


def _render_board_lists(
    kanban: dict[str, list[dict[str, Any]]],
    inbox: list[dict[str, Any]],
    closed_tasks: list[dict[str, Any]],
    project_options: str,
) -> str:
    columns: list[tuple[str, str]] = []
    if inbox:
        inbox_cards = "".join(_render_trello_capture_card(item, project_options) for item in inbox)
        columns.append(("Inbox", inbox_cards))

    preferred = ["ATHENA", "FLEIRE", "BLOCKED", "SOMEDAY"]
    seen: set[str] = set()
    for bucket in preferred + sorted(kanban):
        if bucket in seen or bucket not in kanban:
            continue
        seen.add(bucket)
        cards = "".join(_render_trello_task_card(task) for task in kanban[bucket])
        columns.append((bucket.title(), cards))

    if closed_tasks:
        done_cards = "".join(_render_trello_closed_card(task) for task in closed_tasks)
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


def _render_project_control(projects: list[dict[str, Any]]) -> str:
    if not projects:
        return '<p class="muted">No projects yet.</p>'
    options = _project_options(projects, include_blank=False)
    return f"""
    <form class="capture-form" method="post" action="/projects/update">
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


def _render_html(data: dict[str, Any], banner_message: str | None = None, banner_kind: str = "ok") -> str:
    raw_counts = data["dashboard"].get("counts", {})
    inbox_counts = data["dashboard"].get("inbox", {})
    stats_html = _build_stat_grid(
        {
            "open_tasks": raw_counts.get("open_tasks", 0),
            "in_progress_tasks": raw_counts.get("in_progress_tasks", 0),
            "queued_tasks": raw_counts.get("queued_tasks", 0),
            "blocked_tasks": raw_counts.get("blocked_tasks", 0),
            "closed_tasks": raw_counts.get("closed_tasks", len(data.get("closed_tasks", []))),
            "new_items": inbox_counts.get("new_items", 0),
            "triaged_items": inbox_counts.get("triaged_items", 0),
        }
    )
    sync_controls = _render_sync_controls()
    review_controls = _render_review_controls()
    project_options = _project_options(data.get("projects", []))
    board_html = _render_board_lists(
        data.get("kanban", {}),
        data.get("inbox", []),
        data.get("closed_tasks", []),
        project_options,
    )
    quick_capture_html = _render_quick_capture_form()
    project_control_html = _render_project_control(data.get("projects", []))
    life_parts = _render_items(data["life"]["areas"], ["name", "status", "priority", "notes"])
    life_goals = _render_items(data["life"]["goals"], ["title", "status", "horizon", "current_focus"])
    life_people = _render_items(data["life"]["people"], ["name", "relationship_type", "importance_score"])
    project_rows = _render_items(
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
    repo_rows = _render_items(data["repos"], ["repo_name", "project_name", "last_seen_branch", "last_seen_dirty", "last_scanned_at"])
    task_rows = _render_items(data["tasks"], ["title", "bucket", "status", "priority", "project_name", "next_action", "blocker"])
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
    banner = f'<div class="banner {html.escape(banner_kind)}">{html.escape(banner_message)}</div>' if banner_message else ""
    buckets = ", ".join(sorted(data.get("kanban", {}))) or "None"

    return f"""<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Athena Board</title>
    <link rel="stylesheet" href="/static/style.css">
  </head>
  <body>
    <header class="hero">
      <div>
        <p class="eyebrow">Athena · Fleire Castro</p>
        <h1>Control surface</h1>
        <p>Live awareness for life, projects, and execution. Every action is wired to the state layer.</p>
      </div>
      <div class="hero-aside">
        <div class="stat-grid">
          {stats_html}
        </div>
        <div class="sync-controls">
          {sync_controls}
        </div>
      </div>
    </header>
    {banner}
    <main>
      <section class="panel board-panel">
        <div class="panel-heading">
          <div>
            <h2>Board</h2>
            <p>Trello-style view of inbox, active work, blocked work, and done work.</p>
          </div>
          <div class="panel-actions">
            <span class="muted">Lists: {html.escape(buckets)}</span>
          </div>
        </div>
        <div class="trello-board">
          {board_html}
        </div>
      </section>
      <section class="panel">
        <h2>Quick Capture</h2>
        {quick_capture_html}
      </section>
      <section class="panel">
        <h2>Portfolios & Projects</h2>
        {project_control_html}
        {project_rows}
      </section>
      <section class="panel">
        <h2>Reviews</h2>
        <div class="sync-controls">
          {review_controls}
        </div>
      </section>
      <section class="panel life-panel">
        <h2>Life context</h2>
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
      </section>
      <section class="panel">
        <h2>Repos</h2>
        {repo_rows}
      </section>
      <section class="panel">
        <h2>Tasks</h2>
        {task_rows}
      </section>
      <section class="panel">
        <h2>Sources</h2>
        {source_rows}
      </section>
      <section class="panel">
        <h2>Awareness Briefs</h2>
        {brief_rows}
      </section>
      <section class="panel">
        <h2>Telegram Chat State</h2>
        {chat_html}
      </section>
    </main>
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

    def _redirect_home(self, notice: str, kind: str = "ok") -> None:
        query = urlencode({"notice": notice, "kind": kind})
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", f"/?{query}")
        self.end_headers()

    def _optional(self, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    def _run_sync_command(self, paths: AthenaPaths, command: str, *, as_json: bool) -> None:
        if command not in {"all", "life", "repos", "briefs"}:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        summary = run_sync(command, paths=paths)
        if as_json:
            _json_response(self, summary)
            return
        self._redirect_home(f"Sync complete: {command}")

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
            return state_module.complete_task(
                task_id=task_id,
                db_path=paths.db_path,
                actor=actor,
                summary=summary,
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

    def do_GET(self) -> None:
        paths = self.server.athena_state.paths  # type: ignore[attr-defined]
        parsed = urlsplit(self.path)
        route = parsed.path
        query = parse_qs(parsed.query)
        if route == "/":
            data = _gather_data(paths)
            banner_message = query.get("notice", [query.get("synced", [None])[0]])[0]
            banner_kind = query.get("kind", ["ok"])[0]
            self._send_html(_render_html(data, banner_message=banner_message, banner_kind=banner_kind))
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
            elif endpoint == "inbox":
                _json_response(self, data["inbox"])
            elif endpoint == "kanban":
                _json_response(self, data["kanban"])
            elif endpoint == "closed-tasks":
                _json_response(self, data["closed_tasks"])
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
            self._run_sync_command(paths, route.removeprefix("/sync/"), as_json=False)
            return
        if route == "/captures/new":
            params = self._read_form()
            try:
                self._handle_new_capture(paths, params)
                self._redirect_home("Captured into inbox")
            except Exception as exc:
                self._redirect_home(f"Capture failed: {exc}", kind="error")
            return
        if route == "/projects/update":
            params = self._read_form()
            try:
                self._handle_project_update(paths, params)
                self._redirect_home("Project updated")
            except Exception as exc:
                self._redirect_home(f"Project update failed: {exc}", kind="error")
            return
        if route.startswith("/api/projects/update"):
            params = self._read_form()
            try:
                _json_response(self, self._handle_project_update(paths, params))
            except Exception as exc:
                self.send_error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        if route.startswith("/reviews/"):
            cadence = route.removeprefix("/reviews/")
            try:
                run_review_cycle(cadence, db_path=paths.db_path, actor="board")
                self._redirect_home(f"Review complete: {cadence}")
            except Exception as exc:
                self._redirect_home(f"Review failed: {exc}", kind="error")
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
                    self._redirect_home(f"Created task {result['id']}")
                except Exception as exc:
                    self._redirect_home(f"Capture conversion failed: {exc}", kind="error")
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
                    self._redirect_home(f"Task updated: {parts[2]}")
                except Exception as exc:
                    self._redirect_home(f"Task action failed: {exc}", kind="error")
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
