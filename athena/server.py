from __future__ import annotations

import argparse
import html
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from http import HTTPStatus
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .config import AthenaPaths, default_paths
from .db import connect_db, dashboard_snapshot, ensure_db, query_all, query_one
from .sync import run_sync


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
          p.current_goal,
          p.next_milestone,
          p.blocker
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


def _gather_data(paths: AthenaPaths) -> dict[str, Any]:
    ensure_db(paths=paths)
    with connect_db(paths.db_path) as conn:
        return {
            "dashboard": dashboard_snapshot(paths.db_path),
            "life": _life_context(conn),
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
            f"<div class=\"item-field\"><strong>{html.escape(field)}:</strong> {html.escape(str(item.get(field, '')))}</div>"
            if item.get(field) is not None else ""
            for field in fields
            if field in item
        )
        rows.append(f"<div class=\"item-card\">{row}</div>")
    return "".join(rows) if rows else "<p class=\"muted\">No records yet.</p>"


def _render_html(data: dict[str, Any], sync_status: str | None = None) -> str:
    counts = data["dashboard"].get("counts", {})
    counts_html = "".join(
        f"<div class=\"stat\"><span>{label}</span><strong>{counts.get(label.lower() + '_tasks', '0')}</strong></div>"
        for label in ("open", "blocked", "in_progress", "queued")
    )
    sync_html = "".join(
        f"""
        <form method=\"post\" action=\"/sync/{command}\">
          <button type=\"submit\">{label}</button>
        </form>
        """
        for command, label in (
            ("all", "Sync All"),
            ("life", "Sync Life"),
            ("repos", "Scan Repos"),
            ("briefs", "Refresh Briefs"),
        )
    )
    life_parts = _render_items(data["life"]["areas"], ["name", "status", "priority", "notes"])
    life_goals = _render_items(data["life"]["goals"], ["title", "status", "horizon", "current_focus"])
    life_people = _render_items(data["life"]["people"], ["name", "relationship_type", "importance_score"])
    project_rows = _render_items(data["projects"], ["name", "portfolio_name", "status", "health", "current_goal"])
    repo_rows = _render_items(data["repos"], ["repo_name", "project_name", "last_seen_branch", "last_seen_dirty", "last_scanned_at"])
    task_rows = _render_items(data["tasks"], ["title", "bucket", "status", "priority", "project_name", "next_action", "blocker"])
    source_rows = _render_items(data["sources"], ["title", "kind", "is_authoritative", "source_system"])
    brief_rows = _render_items(data["briefs"], ["scope_kind", "scope_id", "brief_type", "content"])
    chat = data.get("chat") or {}
    chat_html = (
        "".join(
            f"<div class=\"item-field\"><strong>{html.escape(key)}:</strong> {html.escape(str(value))}</div>"
            for key, value in chat.items()
            if value
        )
        or "<p class=\"muted\">No active chat state.</p>"
    )

    return """<!DOCTYPE html>
<html lang=\"en\">
  <head>
    <meta charset=\"utf-8\">
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
    <title>Athena Board</title>
    <link rel=\"stylesheet\" href=\"/static/style.css\">
  </head>
  <body>
    <header class=\"hero\">
      <div>
        <p class=\"eyebrow\">Athena / Fleire Castro</p>
        <h1>Local command center</h1>
        <p>Life, portfolio, and execution context refreshed directly from the SQLite truth.</p>
      </div>
      <div class=\"hero-aside\">
        <div class=\"hero-stats\">{counts}</div>
        <div class=\"sync-actions\">{sync_html}</div>
      </div>
    </header>
    {sync_banner}
    <main>
      <section class=\"panel\">
        <h2>Life Context</h2>
        <div class=\"panels\">
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
      <section class=\"panel\">
        <h2>Portfolios & Projects</h2>
        {project_rows}
      </section>
      <section class=\"panel\">
        <h2>Repos</h2>
        {repo_rows}
      </section>
      <section class=\"panel\">
        <h2>Tasks</h2>
        {task_rows}
      </section>
      <section class=\"panel\">
        <h2>Sources</h2>
        {source_rows}
      </section>
      <section class=\"panel\">
        <h2>Awareness Briefs</h2>
        {brief_rows}
      </section>
      <section class=\"panel\">
        <h2>Telegram Chat State</h2>
        {chat_html}
      </section>
    </main>
  </body>
</html>""".format(
        counts=counts_html,
        sync_html=sync_html,
        sync_banner=(
            f"<div class=\"banner\">Last sync: {html.escape(sync_status)}</div>" if sync_status else ""
        ),
        life_parts=life_parts,
        life_goals=life_goals,
        life_people=life_people,
        project_rows=project_rows,
        repo_rows=repo_rows,
        task_rows=task_rows,
        source_rows=source_rows,
        brief_rows=brief_rows,
        chat_html=chat_html,
    )


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
        asset = (paths.repo_root / "athena/static" / rel_path.strip("/"))
        if not asset.exists() or not asset.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content = asset.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/css")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _run_sync_command(self, paths: AthenaPaths, command: str, *, as_json: bool) -> None:
        if command not in {"all", "life", "repos", "briefs"}:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        summary = run_sync(command, paths=paths)
        if as_json:
            _json_response(self, summary)
            return
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", f"/?synced={command}")
        self.end_headers()

    def do_GET(self) -> None:
        paths = self.server.athena_state.paths  # type: ignore[attr-defined]
        parsed = urlsplit(self.path)
        route = parsed.path
        query = parse_qs(parsed.query)
        if route == "/":
            data = _gather_data(paths)
            sync_status = query.get("synced", [None])[0]
            self._send_html(_render_html(data, sync_status=sync_status))
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
        self.send_error(HTTPStatus.NOT_FOUND)


def create_server(host: str, port: int, paths: AthenaPaths | None = None) -> ThreadingHTTPServer:
    resolved_paths = paths or default_paths()
    ensure_db(paths=resolved_paths)
    server_state = ServerState(paths=resolved_paths)
    handler = AthenaHandler
    httpd = ThreadingHTTPServer((host, port), handler)
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
