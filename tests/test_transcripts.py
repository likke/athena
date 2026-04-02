from __future__ import annotations

import base64
import contextlib
import io
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from athena.config import default_paths
from athena.google import GmailAccountSettings
from athena.taskctl import main
from athena.transcripts import build_targets, collect_call_transcripts


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
        self.routes: list[tuple[str, object]] = []

    def add_json(self, contains: str, payload: object) -> None:
        self.routes.append((contains, payload))

    def request_json(self, method: str, url: str, *, headers=None, data=None):
        for contains, payload in self.routes:
            if contains in url:
                if isinstance(payload, Exception):
                    raise payload
                return payload
        if "/gmail/v1/users/me/messages?" in url:
            return {"messages": []}
        raise AssertionError(f"Unexpected JSON request: {method} {url}")


def _b64url(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


class TranscriptTests(unittest.TestCase):
    def test_build_targets_uses_presets_and_custom_terms(self) -> None:
        targets = build_targets(["Lorna", "Custom Client"])
        self.assertEqual(targets[0].slug, "lorna")
        self.assertIn('"lorna bondoc"', targets[0].aliases)
        self.assertEqual(targets[1].slug, "custom-client")
        self.assertEqual(targets[1].aliases, ('"Custom Client"',))

    def test_collect_call_transcripts_writes_bundle_and_readable_copy(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            paths = _test_paths(tmp)
            destination = tmp / "drive"
            destination.mkdir(parents=True, exist_ok=True)

            transport = FakeTransport()
            transport.add_json(
                "messages?q=from%3Ano-reply%40fathom.video+%28%22lorna+bondoc%22",
                {
                    "messages": [{"id": "msg-1", "threadId": "thread-1"}],
                },
            )
            transport.add_json(
                "messages?q=%28subject%3A%28recap%29",
                {
                    "messages": [{"id": "msg-1", "threadId": "thread-1"}],
                },
            )
            transport.add_json(
                "messages/msg-1?format=full",
                {
                    "id": "msg-1",
                    "threadId": "thread-1",
                    "snippet": "Transcript summary",
                    "payload": {
                        "headers": [
                            {"name": "Subject", "value": 'Recap for "Check-in: Yoveo x DashoContent"'},
                            {"name": "From", "value": "Fathom <no-reply@fathom.video>"},
                            {"name": "To", "value": "fleire@thirdteam.org"},
                            {"name": "Date", "value": "Tue, 31 Mar 2026 15:27:31 +0000"},
                        ],
                        "mimeType": "multipart/alternative",
                        "parts": [
                            {
                                "mimeType": "text/plain",
                                "body": {"data": _b64url("Meeting recap\nhttps://fathom.video/share/demo")},
                            }
                        ],
                    },
                },
            )

            with patch(
                "athena.transcripts.resolve_gmail_account",
                return_value=GmailAccountSettings(label="primary", email="fleire@thirdteam.org"),
            ), patch(
                "athena.transcripts.ensure_access_token",
                return_value="access-token",
            ), patch(
                "athena.transcripts.shutil.which",
                return_value=None,
            ):
                result = collect_call_transcripts(
                    targets=["lorna"],
                    destination=destination,
                    paths=paths,
                    transport=transport,
                )

            self.assertTrue(result["ok"])
            self.assertEqual(result["targets"][0]["matched_messages"], 1)
            bundle_path = Path(result["destination_path"]) / "lorna.md"
            self.assertTrue(bundle_path.exists())
            self.assertTrue(bundle_path.with_suffix(".txt").exists())
            self.assertTrue((Path(result["destination_path"]) / "README.md").exists())
            self.assertEqual(result["targets"][0]["docx_file"], None)

    def test_taskctl_collect_call_transcripts_command_returns_json(self) -> None:
        stdout = io.StringIO()
        with patch(
            "athena.taskctl.collect_call_transcripts",
            return_value={"ok": True, "destination_path": "/tmp/demo", "targets": []},
        ), patch(
            "sys.argv",
            ["taskctl", "collect-call-transcripts", "--target", "lorna"],
        ), contextlib.redirect_stdout(stdout):
            exit_code = main()

        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["destination_path"], "/tmp/demo")
