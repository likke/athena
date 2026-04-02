from __future__ import annotations

import base64
import json
import tempfile
import unittest
import urllib.parse
from dataclasses import replace
from pathlib import Path

from athena.config import default_paths
from athena.db import connect_db, ensure_db
from athena.google import (
    GMAIL_READONLY_SCOPE,
    GMAIL_MODIFY_SCOPE,
    DRIVE_FULL_SCOPE,
    DOCS_SCOPE,
    build_auth_url,
    exchange_code,
    list_drive_folders,
    mirror_google_sources,
    oauth_status,
    requested_scopes,
)


def _test_paths(tmp_dir: Path):
    paths = default_paths()
    overrides = {
        "db_path": tmp_dir / "tasks.sqlite",
        "workspace_root": tmp_dir / "workspace",
        "workspace_telegram_root": tmp_dir / "workspace-telegram",
        "life_dir": tmp_dir / "life",
        "notebooklm_export_dir": tmp_dir / "life" / "notebooklm-exports",
        "google_dir": tmp_dir / "workspace" / "system" / "google",
        "google_settings_path": tmp_dir / "workspace" / "system" / "google" / "settings.json",
        "google_client_secrets_path": tmp_dir / "workspace" / "system" / "google" / "client_secret.json",
        "google_token_path": tmp_dir / "workspace" / "system" / "google" / "token.json",
        "google_mirror_dir": tmp_dir / "workspace" / "system" / "google-mirror",
        "task_view_dir": tmp_dir / "workspace-telegram" / "task-system",
        "ledger_path": tmp_dir / "workspace" / "system" / "task-ledger" / "telegram-1937792843.md",
        "local_ledger_path": tmp_dir / "workspace-telegram" / "task-system" / "TELEGRAM_LEDGER.md",
    }
    return replace(paths, **overrides)


class FakeTransport:
    def __init__(self):
        self.json_routes: list[tuple[str, object]] = []
        self.byte_routes: list[tuple[str, bytes]] = []

    def add_json(self, contains: str, payload: object) -> None:
        self.json_routes.append((contains, payload))

    def add_bytes(self, contains: str, payload: bytes) -> None:
        self.byte_routes.append((contains, payload))

    def request_json(self, method: str, url: str, *, headers=None, data=None):
        for contains, payload in self.json_routes:
            if contains in url:
                return payload
        raise AssertionError(f"Unexpected JSON request: {method} {url}")

    def request_bytes(self, method: str, url: str, *, headers=None, data=None) -> bytes:
        for contains, payload in self.byte_routes:
            if contains in url:
                return payload
        raise AssertionError(f"Unexpected bytes request: {method} {url}")


class GoogleTestCase(unittest.TestCase):
    def test_build_auth_url_and_exchange_code(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            paths = _test_paths(tmp)
            paths.google_dir.mkdir(parents=True, exist_ok=True)
            paths.google_client_secrets_path.write_text(
                json.dumps(
                    {
                        "installed": {
                            "client_id": "client-id",
                            "client_secret": "client-secret",
                            "auth_uri": "https://accounts.example.com/o/oauth2/v2/auth",
                            "token_uri": "https://oauth2.example.com/token",
                        }
                    }
                ),
                encoding="utf-8",
            )

            auth = build_auth_url(paths, scopes=("gmail",))
            self.assertIn("accounts.example.com", auth["auth_url"])
            self.assertEqual(auth["scopes"], [GMAIL_READONLY_SCOPE])
            self.assertTrue((paths.google_dir / "oauth-session.json").exists())
            params = urllib.parse.parse_qs(urllib.parse.urlparse(auth["auth_url"]).query)
            self.assertEqual(params["include_granted_scopes"], ["false"])

            transport = FakeTransport()
            transport.add_json(
                "https://oauth2.example.com/token",
                {
                    "access_token": "access-token",
                    "refresh_token": "refresh-token",
                    "expires_in": 3600,
                    "scope": GMAIL_READONLY_SCOPE,
                    "token_type": "Bearer",
                },
            )
            result = exchange_code("auth-code", paths, transport=transport)
            self.assertTrue(result["has_refresh_token"])
            token = json.loads(paths.google_token_path.read_text(encoding="utf-8"))
            self.assertEqual(token["access_token"], "access-token")
            self.assertEqual(token["refresh_token"], "refresh-token")
            self.assertGreater(token["expiry"], 0)

    def test_requested_scopes_and_status_use_full_profile(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            paths = _test_paths(tmp)
            paths.google_dir.mkdir(parents=True, exist_ok=True)
            paths.google_settings_path.write_text(
                json.dumps(
                    {
                        "oauth": {
                            "profile": "athena-google-full",
                            "scopes": [],
                            "include_granted_scopes": False,
                        }
                    }
                ),
                encoding="utf-8",
            )
            paths.google_token_path.write_text(
                json.dumps(
                    {
                        "access_token": "cached-access-token",
                        "refresh_token": "refresh-token",
                        "expiry": 4102444800,
                        "scope": " ".join([GMAIL_MODIFY_SCOPE, DRIVE_FULL_SCOPE, DOCS_SCOPE]),
                    }
                ),
                encoding="utf-8",
            )

            scopes = requested_scopes(paths)
            self.assertIn(GMAIL_MODIFY_SCOPE, scopes)
            self.assertIn(DRIVE_FULL_SCOPE, scopes)
            self.assertIn(DOCS_SCOPE, scopes)

            status = oauth_status(paths)
            self.assertEqual(status["oauth_profile"], "athena-google-full")
            self.assertFalse(status["include_granted_scopes"])
            self.assertTrue(status["token_present"])
            self.assertIn(GMAIL_MODIFY_SCOPE, status["granted_scopes"])

    def test_build_auth_url_can_opt_in_to_include_granted_scopes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            paths = _test_paths(tmp)
            paths.google_dir.mkdir(parents=True, exist_ok=True)
            paths.google_client_secrets_path.write_text(
                json.dumps(
                    {
                        "installed": {
                            "client_id": "client-id",
                            "client_secret": "client-secret",
                            "auth_uri": "https://accounts.example.com/o/oauth2/v2/auth",
                            "token_uri": "https://oauth2.example.com/token",
                        }
                    }
                ),
                encoding="utf-8",
            )
            paths.google_settings_path.write_text(
                json.dumps(
                    {
                        "oauth": {
                            "profile": "athena-google-full",
                            "scopes": [],
                            "include_granted_scopes": True,
                        }
                    }
                ),
                encoding="utf-8",
            )

            auth = build_auth_url(paths, scopes=("gmail",))
            params = urllib.parse.parse_qs(urllib.parse.urlparse(auth["auth_url"]).query)
            self.assertEqual(params["include_granted_scopes"], ["true"])

    def test_mirror_google_sources_syncs_gmail_drive_and_notebooklm(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            paths = _test_paths(tmp)
            ensure_db(paths=paths)
            paths.google_dir.mkdir(parents=True, exist_ok=True)
            paths.google_settings_path.write_text(
                json.dumps(
                    {
                        "gmail": {
                            "enabled": True,
                            "query": "in:inbox newer_than:30d",
                            "max_results": 1,
                        },
                        "drive": {
                            "folders": [
                                {
                                    "id": "drive-folder",
                                    "name": "Drive Mirror",
                                }
                            ]
                        },
                        "notebooklm": {
                            "folder_id": "notebook-folder",
                            "name": "NotebookLM Exports",
                        },
                    }
                ),
                encoding="utf-8",
            )
            paths.google_token_path.write_text(
                json.dumps(
                    {
                        "access_token": "cached-access-token",
                        "refresh_token": "refresh-token",
                        "expiry": 4102444800,
                    }
                ),
                encoding="utf-8",
            )

            body = base64.urlsafe_b64encode(b"Email body text").decode("ascii").rstrip("=")
            transport = FakeTransport()
            transport.add_json(
                "gmail.googleapis.com/gmail/v1/users/me/messages?",
                {"messages": [{"id": "msg-1"}]},
            )
            transport.add_json(
                "gmail.googleapis.com/gmail/v1/users/me/messages/msg-1?format=full",
                {
                    "id": "msg-1",
                    "internalDate": "1712088000000",
                    "snippet": "Snippet text",
                    "labelIds": ["INBOX", "UNREAD"],
                    "payload": {
                        "headers": [
                            {"name": "Subject", "value": "Inbox task"},
                            {"name": "From", "value": "sender@example.com"},
                            {"name": "Date", "value": "Tue, 2 Apr 2026 10:00:00 +0000"},
                        ],
                        "mimeType": "multipart/alternative",
                        "parts": [
                            {
                                "mimeType": "text/plain",
                                "body": {"data": body},
                            }
                        ],
                    },
                },
            )
            transport.add_json(
                "drive/v3/files?q=%27drive-folder%27",
                {
                    "files": [
                        {
                            "id": "doc-1",
                            "name": "Project Notes",
                            "mimeType": "application/vnd.google-apps.document",
                            "webViewLink": "https://drive.google.com/file/d/doc-1/view",
                        }
                    ]
                },
            )
            transport.add_json(
                "drive/v3/files?q=%27notebook-folder%27",
                {
                    "files": [
                        {
                            "id": "note-1",
                            "name": "Notebook Goals",
                            "mimeType": "text/plain",
                            "webViewLink": "https://drive.google.com/file/d/note-1/view",
                        }
                    ]
                },
            )
            transport.add_bytes("/files/doc-1/export?mimeType=text/plain", b"Drive project note text")
            transport.add_bytes("/files/note-1?alt=media", b"Notebook life goal text")

            with connect_db(paths.db_path) as conn:
                result = mirror_google_sources(conn, paths=paths, transport=transport)
                conn.commit()

            self.assertEqual(result["gmail_messages"], 1)
            self.assertEqual(result["drive_files"], 1)
            self.assertEqual(result["notebooklm_files"], 1)
            self.assertTrue((paths.google_mirror_dir / "gmail" / "msg-1.md").exists())
            self.assertTrue((paths.notebooklm_export_dir / "Notebook-Goals-note-1.txt").exists())
            with connect_db(paths.db_path) as conn:
                rows = list(
                    conn.execute(
                        "SELECT source_system, kind, title FROM source_documents ORDER BY source_system, kind, title"
                    )
                )
                source_kinds = {(row["source_system"], row["kind"]) for row in rows}
                self.assertIn(("gmail", "gmail_message"), source_kinds)
                self.assertIn(("gmail", "gmail_mailbox"), source_kinds)
                self.assertIn(("gdrive", "drive_file"), source_kinds)
                self.assertIn(("gdrive", "drive_file_summary"), source_kinds)
                self.assertIn(("NotebookLM", "notebooklm_export_summary"), source_kinds)

    def test_list_drive_folders_uses_access_token_and_query(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            paths = _test_paths(tmp)
            paths.google_dir.mkdir(parents=True, exist_ok=True)
            paths.google_token_path.write_text(
                json.dumps(
                    {
                        "access_token": "cached-access-token",
                        "refresh_token": "refresh-token",
                        "expiry": 4102444800,
                    }
                ),
                encoding="utf-8",
            )

            transport = FakeTransport()
            transport.add_json(
                "drive/v3/files?",
                {
                    "files": [
                        {
                            "id": "folder-1",
                            "name": "NotebookLM Exports",
                            "webViewLink": "https://drive.google.com/drive/folders/folder-1",
                        }
                    ]
                },
            )
            rows = list_drive_folders(paths=paths, transport=transport, query="NotebookLM", limit=10)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["id"], "folder-1")


if __name__ == "__main__":
    unittest.main()
