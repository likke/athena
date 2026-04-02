from __future__ import annotations

import base64
import json
import tempfile
import unittest
import urllib.parse
from urllib.error import URLError
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
    create_gmail_draft,
    exchange_code,
    init_settings_template,
    list_drive_folders,
    mirror_google_sources,
    oauth_status,
    requested_scopes,
    search_gmail_messages,
    send_gmail_draft,
)


def _test_paths(tmp_dir: Path):
    paths = default_paths()
    overrides = {
        "db_path": tmp_dir / "tasks.sqlite",
        "workspace_root": tmp_dir / "workspace",
        "workspace_telegram_root": tmp_dir / "workspace-telegram",
        "briefs_dir": tmp_dir / "workspace" / "system" / "briefs",
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
        self.requests: list[dict[str, object]] = []

    def add_json(self, contains: str, payload: object) -> None:
        self.json_routes.append((contains, payload))

    def add_bytes(self, contains: str, payload: bytes) -> None:
        self.byte_routes.append((contains, payload))

    def request_json(self, method: str, url: str, *, headers=None, data=None):
        self.requests.append({"kind": "json", "method": method, "url": url, "headers": headers or {}, "data": data})
        for contains, payload in self.json_routes:
            if contains in url:
                if isinstance(payload, Exception):
                    raise payload
                return payload
        raise AssertionError(f"Unexpected JSON request: {method} {url}")

    def request_bytes(self, method: str, url: str, *, headers=None, data=None) -> bytes:
        self.requests.append({"kind": "bytes", "method": method, "url": url, "headers": headers or {}, "data": data})
        for contains, payload in self.byte_routes:
            if contains in url:
                if isinstance(payload, Exception):
                    raise payload
                return payload
        raise AssertionError(f"Unexpected bytes request: {method} {url}")


class GoogleTestCase(unittest.TestCase):
    def test_init_settings_template_defaults_to_primary_mailbox(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            paths = _test_paths(tmp)

            settings_path = init_settings_template(paths, force=True)
            payload = json.loads(settings_path.read_text(encoding="utf-8"))

            self.assertEqual(payload["gmail"]["default_account"], "primary")
            accounts = {row["label"]: row for row in payload["gmail"]["accounts"]}
            self.assertTrue(accounts["primary"]["default"])
            self.assertEqual(accounts["primary"]["email"], "fleire@thirdteam.org")
            self.assertFalse(accounts["athena"]["default"])
            self.assertEqual(accounts["athena"]["display_name"], "Athena (send-as)")

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

    def test_account_specific_auth_and_draft_use_account_paths_and_sender(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            paths = _test_paths(tmp)
            paths.google_dir.mkdir(parents=True, exist_ok=True)
            athena_client_secret = paths.google_dir / "accounts" / "athena" / "client_secret.json"
            athena_token_path = paths.google_dir / "accounts" / "athena" / "token.json"
            athena_client_secret.parent.mkdir(parents=True, exist_ok=True)
            athena_client_secret.write_text(
                json.dumps(
                    {
                        "installed": {
                            "client_id": "athena-client-id",
                            "client_secret": "athena-client-secret",
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
                            "include_granted_scopes": False,
                        },
                        "gmail": {
                            "enabled": True,
                            "default_account": "athena",
                            "accounts": [
                                {
                                    "label": "athena",
                                    "email": "athena@thirdteam.org",
                                    "display_name": "Athena",
                                    "default": True,
                                    "token_path": "accounts/athena/token.json",
                                    "client_secret_path": "accounts/athena/client_secret.json",
                                }
                            ],
                        },
                    }
                ),
                encoding="utf-8",
            )

            auth = build_auth_url(paths, scopes=("gmail-compose",), account_label="athena")
            self.assertEqual(auth["account_label"], "athena")
            self.assertEqual(auth["account_email"], "athena@thirdteam.org")
            self.assertEqual(Path(auth["token_path"]).resolve(), athena_token_path.resolve())
            self.assertTrue((paths.google_dir / "oauth-session.athena.json").exists())
            params = urllib.parse.parse_qs(urllib.parse.urlparse(auth["auth_url"]).query)
            self.assertEqual(params["login_hint"], ["athena@thirdteam.org"])

            transport = FakeTransport()
            transport.add_json(
                "https://oauth2.example.com/token",
                {
                    "access_token": "athena-access-token",
                    "refresh_token": "athena-refresh-token",
                    "expires_in": 3600,
                    "scope": " ".join([GMAIL_MODIFY_SCOPE]),
                    "token_type": "Bearer",
                },
            )
            exchange = exchange_code("athena-auth-code", paths, transport=transport, account_label="athena")
            self.assertEqual(exchange["account_label"], "athena")
            self.assertTrue(athena_token_path.exists())

            transport.add_json(
                "gmail.googleapis.com/gmail/v1/users/me/drafts",
                {
                    "id": "athena-draft-1",
                    "message": {"id": "athena-msg-1", "threadId": "athena-thread-1"},
                },
            )
            draft = create_gmail_draft(
                paths=paths,
                to_recipients="client@example.com",
                subject="Athena note",
                body_text="Draft body",
                account_label="athena",
                transport=transport,
            )
            self.assertEqual(draft["account_label"], "athena")
            draft_requests = [request for request in transport.requests if "gmail.googleapis.com/gmail/v1/users/me/drafts" in str(request["url"])]
            self.assertTrue(draft_requests)
            payload = json.loads(bytes(draft_requests[-1]["data"]).decode("utf-8"))
            raw_message = payload["message"]["raw"]
            padded = raw_message + "=" * ((4 - len(raw_message) % 4) % 4)
            decoded = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
            self.assertIn("From: Athena <athena@thirdteam.org>", decoded)

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
                        "calendar": {
                            "enabled": True,
                            "calendar_ids": ["primary"],
                            "days_back": 0,
                            "days_ahead": 14,
                            "max_results": 5,
                        },
                        "contacts": {
                            "enabled": True,
                            "page_size": 50,
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
                "calendar/v3/calendars/primary/events?",
                {
                    "items": [
                        {
                            "id": "evt-1",
                            "status": "confirmed",
                            "summary": "Founder sync",
                            "description": "Review priorities and deadlines.",
                            "location": "Zoom",
                            "htmlLink": "https://calendar.google.com/calendar/event?eid=evt-1",
                            "start": {"dateTime": "2026-04-03T01:00:00Z"},
                            "end": {"dateTime": "2026-04-03T02:00:00Z"},
                            "organizer": {"email": "fleire@thirdteam.org", "displayName": "Fleire Castro"},
                            "attendees": [
                                {"email": "kent@example.com", "responseStatus": "accepted"},
                            ],
                        }
                    ]
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
            transport.add_json(
                "people.googleapis.com/v1/people/me/connections?",
                {
                    "connections": [
                        {
                            "resourceName": "people/c123",
                            "names": [{"displayName": "Kent Dev"}],
                            "emailAddresses": [{"value": "kent@example.com"}],
                            "organizations": [{"name": "Third Team Ventures"}],
                            "biographies": [{"value": "Junior dev on DashoContent."}],
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
            self.assertEqual(result["calendar_events"], 1)
            self.assertEqual(result["contacts_synced"], 1)
            self.assertEqual(result["drive_files"], 1)
            self.assertEqual(result["notebooklm_files"], 1)
            self.assertTrue((paths.google_mirror_dir / "gmail" / "msg-1.md").exists())
            self.assertTrue((paths.google_mirror_dir / "calendar" / "events" / "evt-1.md").exists())
            self.assertTrue((paths.google_mirror_dir / "contacts" / "person-google-people-c123.md").exists())
            self.assertTrue((paths.notebooklm_export_dir / "Notebook-Goals-note-1.txt").exists())
            with connect_db(paths.db_path) as conn:
                rows = list(
                    conn.execute(
                        "SELECT source_system, kind, title FROM source_documents ORDER BY source_system, kind, title"
                    )
                )
                source_kinds = {(row["source_system"], row["kind"]) for row in rows}
                self.assertIn(("gcal", "calendar_event"), source_kinds)
                self.assertIn(("gcal", "calendar_agenda"), source_kinds)
                self.assertIn(("gmail", "gmail_message"), source_kinds)
                self.assertIn(("gmail", "gmail_mailbox"), source_kinds)
                self.assertIn(("gpeople", "contact_profile"), source_kinds)
                self.assertIn(("gpeople", "contacts_summary"), source_kinds)
                self.assertIn(("gdrive", "drive_file"), source_kinds)
                self.assertIn(("gdrive", "drive_file_summary"), source_kinds)
                self.assertIn(("NotebookLM", "notebooklm_export_summary"), source_kinds)
                person = conn.execute("SELECT * FROM people WHERE id = ?", ("person-google-people-c123",)).fetchone()
                self.assertIsNotNone(person)
                assert person is not None
                self.assertEqual(person["name"], "Kent Dev")
                self.assertEqual(person["contact_rule"], "kent@example.com")

    def test_mirror_google_sources_reports_calendar_service_errors_without_crashing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            paths = _test_paths(tmp)
            ensure_db(paths=paths)
            paths.google_dir.mkdir(parents=True, exist_ok=True)
            paths.google_settings_path.write_text(
                json.dumps(
                    {
                        "calendar": {
                            "enabled": True,
                            "calendar_ids": ["primary"],
                            "days_back": 0,
                            "days_ahead": 14,
                            "max_results": 5,
                        },
                        "contacts": {
                            "enabled": False,
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

            transport = FakeTransport()
            calendar_error = URLError("Calendar API disabled")
            transport.add_json("calendar/v3/calendars/primary/events?", calendar_error)

            with connect_db(paths.db_path) as conn:
                result = mirror_google_sources(conn, paths=paths, transport=transport)
                conn.commit()

            self.assertTrue(result["google_enabled"])
            self.assertEqual(result["calendar_events"], 0)
            self.assertIn("Calendar API disabled", result["calendar_error"])

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

    def test_create_and_send_gmail_draft(self):
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
                "gmail.googleapis.com/gmail/v1/users/me/drafts/send",
                {
                    "id": "sent-1",
                    "threadId": "thread-1",
                    "labelIds": ["SENT"],
                },
            )
            transport.add_json(
                "gmail.googleapis.com/gmail/v1/users/me/drafts",
                {
                    "id": "draft-1",
                    "message": {
                        "id": "msg-1",
                        "threadId": "thread-1",
                    },
                },
            )

            draft = create_gmail_draft(
                paths=paths,
                to_recipients="person@example.com",
                cc_recipients="cc@example.com",
                subject="Follow-up",
                body_text="Thanks for the call.",
                transport=transport,
            )
            self.assertEqual(draft["draft_id"], "draft-1")
            self.assertEqual(draft["message_id"], "msg-1")

            sent = send_gmail_draft(
                paths=paths,
                draft_id="draft-1",
                transport=transport,
            )
            self.assertEqual(sent["message_id"], "sent-1")
            self.assertIn("SENT", sent["label_ids"])

    def test_search_gmail_messages_uses_api_instead_of_browser(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            paths = _test_paths(tmp)
            paths.google_dir.mkdir(parents=True, exist_ok=True)
            paths.google_settings_path.write_text(
                json.dumps(
                    {
                        "gmail": {
                            "enabled": True,
                            "default_account": "primary",
                            "accounts": [
                                {
                                    "label": "primary",
                                    "email": "fleire@thirdteam.org",
                                    "display_name": "Fleire",
                                    "default": True,
                                }
                            ],
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
                    }
                ),
                encoding="utf-8",
            )

            body_text = "Lorna call transcript is attached for review."
            encoded_body = base64.urlsafe_b64encode(body_text.encode("utf-8")).decode("ascii").rstrip("=")

            transport = FakeTransport()
            transport.add_json(
                "gmail.googleapis.com/gmail/v1/users/me/messages?",
                {
                    "messages": [
                        {
                            "id": "msg-1",
                            "threadId": "thread-1",
                        }
                    ],
                    "resultSizeEstimate": 1,
                },
            )
            transport.add_json(
                "gmail.googleapis.com/gmail/v1/users/me/messages/msg-1?format=full",
                {
                    "id": "msg-1",
                    "threadId": "thread-1",
                    "internalDate": "1712100000000",
                    "labelIds": ["INBOX", "UNREAD"],
                    "snippet": "Transcript attached",
                    "payload": {
                        "mimeType": "multipart/mixed",
                        "headers": [
                            {"name": "Subject", "value": "Lorna transcript"},
                            {"name": "From", "value": "sender@example.com"},
                            {"name": "To", "value": "fleire@thirdteam.org"},
                        ],
                        "parts": [
                            {
                                "mimeType": "text/plain",
                                "body": {"data": encoded_body},
                            },
                            {
                                "mimeType": "application/pdf",
                                "filename": "lorna-transcript.pdf",
                                "body": {"attachmentId": "att-1"},
                            },
                        ],
                    },
                },
            )

            result = search_gmail_messages(
                paths=paths,
                query="lorna transcript",
                account_label="primary",
                max_results=5,
                transport=transport,
            )

            self.assertTrue(result["ok"])
            self.assertEqual(result["mode"], "gmail_api")
            self.assertEqual(result["account_label"], "primary")
            self.assertEqual(result["account_email"], "fleire@thirdteam.org")
            self.assertEqual(result["matched_count"], 1)
            self.assertEqual(result["messages"][0]["subject"], "Lorna transcript")
            self.assertIn("lorna-transcript.pdf", result["messages"][0]["attachment_names"])

            listing_request = next(
                request for request in transport.requests if "gmail.googleapis.com/gmail/v1/users/me/messages?" in str(request["url"])
            )
            params = urllib.parse.parse_qs(urllib.parse.urlparse(str(listing_request["url"])).query)
            self.assertEqual(params["q"], ["lorna transcript"])
            self.assertEqual(params["maxResults"], ["5"])


if __name__ == "__main__":
    unittest.main()
