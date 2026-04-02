from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import secrets
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Iterable

from .config import AthenaPaths, default_paths
from .source_docs import (
    choose_document_id,
    dedupe_source_documents,
    text_summary,
    upsert_source_document,
)

GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL_MODIFY_SCOPE = "https://www.googleapis.com/auth/gmail.modify"
GMAIL_COMPOSE_SCOPE = "https://www.googleapis.com/auth/gmail.compose"
GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
DRIVE_READONLY_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
DRIVE_FILE_SCOPE = "https://www.googleapis.com/auth/drive.file"
DRIVE_FULL_SCOPE = "https://www.googleapis.com/auth/drive"
DOCS_SCOPE = "https://www.googleapis.com/auth/documents"
SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar"
CALENDAR_READONLY_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
PEOPLE_READONLY_SCOPE = "https://www.googleapis.com/auth/contacts.readonly"
PROFILE_OPENID_SCOPE = "openid"
PROFILE_EMAIL_SCOPE = "email"
PROFILE_BASIC_SCOPE = "profile"
SCOPE_ALIASES = {
    "gmail": GMAIL_READONLY_SCOPE,
    "gmail-read": GMAIL_READONLY_SCOPE,
    "gmail-readonly": GMAIL_READONLY_SCOPE,
    "gmail-manage": GMAIL_MODIFY_SCOPE,
    "gmail-modify": GMAIL_MODIFY_SCOPE,
    "gmail-compose": GMAIL_COMPOSE_SCOPE,
    "gmail-send": GMAIL_SEND_SCOPE,
    "drive": DRIVE_READONLY_SCOPE,
    "drive-read": DRIVE_READONLY_SCOPE,
    "drive-readonly": DRIVE_READONLY_SCOPE,
    "drive-file": DRIVE_FILE_SCOPE,
    "drive-write": DRIVE_FILE_SCOPE,
    "drive-full": DRIVE_FULL_SCOPE,
    "docs": DOCS_SCOPE,
    "sheets": SHEETS_SCOPE,
    "calendar": CALENDAR_SCOPE,
    "calendar-read": CALENDAR_READONLY_SCOPE,
    "calendar-readonly": CALENDAR_READONLY_SCOPE,
    "contacts-read": PEOPLE_READONLY_SCOPE,
    "contacts-readonly": PEOPLE_READONLY_SCOPE,
    "openid": PROFILE_OPENID_SCOPE,
    "email": PROFILE_EMAIL_SCOPE,
    "profile": PROFILE_BASIC_SCOPE,
}
SCOPE_GROUPS = {
    "athena-google-readonly": [
        PROFILE_OPENID_SCOPE,
        PROFILE_EMAIL_SCOPE,
        PROFILE_BASIC_SCOPE,
        GMAIL_READONLY_SCOPE,
        DRIVE_READONLY_SCOPE,
    ],
    "athena-google-full": [
        PROFILE_OPENID_SCOPE,
        PROFILE_EMAIL_SCOPE,
        PROFILE_BASIC_SCOPE,
        GMAIL_MODIFY_SCOPE,
        GMAIL_COMPOSE_SCOPE,
        GMAIL_SEND_SCOPE,
        DRIVE_FULL_SCOPE,
        DOCS_SCOPE,
        SHEETS_SCOPE,
        CALENDAR_SCOPE,
        PEOPLE_READONLY_SCOPE,
    ],
}
DEFAULT_REDIRECT_URI = "http://127.0.0.1:8911/oauth2callback"


class GoogleAuthError(RuntimeError):
    pass


@dataclass(frozen=True)
class GmailMirrorSettings:
    enabled: bool = False
    query: str = "in:inbox newer_than:30d"
    max_results: int = 15
    default_account_label: str = "primary"
    accounts: tuple["GmailAccountSettings", ...] = ()


@dataclass(frozen=True)
class GmailAccountSettings:
    label: str = "primary"
    email: str = ""
    display_name: str = ""
    is_default: bool = False
    token_path: Path | None = None
    client_secrets_path: Path | None = None


@dataclass(frozen=True)
class CalendarSyncSettings:
    enabled: bool = False
    calendar_ids: tuple[str, ...] = ("primary",)
    days_back: int = 0
    days_ahead: int = 14
    max_results: int = 25


@dataclass(frozen=True)
class ContactsSyncSettings:
    enabled: bool = True
    page_size: int = 200


@dataclass(frozen=True)
class DriveFolderSpec:
    id: str
    name: str


@dataclass(frozen=True)
class GoogleSyncSettings:
    oauth_profile: str
    oauth_scopes: tuple[str, ...]
    include_granted_scopes: bool
    gmail: GmailMirrorSettings
    calendar: CalendarSyncSettings
    contacts: ContactsSyncSettings
    drive_folders: tuple[DriveFolderSpec, ...]
    notebooklm_folder: DriveFolderSpec | None


class UrlLibTransport:
    def request_json(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        data: bytes | None = None,
    ) -> Any:
        body = self.request_bytes(method, url, headers=headers, data=data)
        if not body:
            return {}
        return json.loads(body.decode("utf-8"))

    def request_bytes(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        data: bytes | None = None,
    ) -> bytes:
        request = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read()


def _ensure_dir(path: Path) -> Path:
    path = path.expanduser().resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _oauth_session_path(paths: AthenaPaths) -> Path:
    return paths.google_dir / "oauth-session.json"


def _oauth_session_path_for_account(paths: AthenaPaths, account_label: str | None = None) -> Path:
    clean = re.sub(r"[^A-Za-z0-9._-]+", "-", str(account_label or "").strip().lower()).strip("-")
    if not clean or clean == "primary":
        return _oauth_session_path(paths)
    return paths.google_dir / f"oauth-session.{clean}.json"


def _resolve_config_path(root: Path, raw_value: Any) -> Path | None:
    clean = str(raw_value or "").strip()
    if not clean:
        return None
    candidate = Path(clean).expanduser()
    if not candidate.is_absolute():
        candidate = (root / candidate).expanduser()
    return candidate.resolve()


def _load_client_config(paths: AthenaPaths) -> dict[str, Any]:
    config_path = paths.google_client_secrets_path.expanduser().resolve()
    if not config_path.exists():
        raise GoogleAuthError(f"Missing Google client secrets: {config_path}")
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    config = raw.get("installed") or raw.get("web")
    if not config:
        raise GoogleAuthError("Google client secrets JSON must contain an 'installed' or 'web' object.")
    return config


def _load_token_data(paths: AthenaPaths) -> dict[str, Any]:
    token_path = paths.google_token_path.expanduser().resolve()
    if not token_path.exists():
        raise GoogleAuthError(f"Missing Google token file: {token_path}")
    return json.loads(token_path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def _normalize_scopes(scopes: Iterable[str]) -> list[str]:
    resolved: list[str] = []
    for scope in scopes:
        clean = scope.strip()
        if not clean:
            continue
        alias = clean.lower()
        if alias in SCOPE_GROUPS:
            for member in _normalize_scopes(SCOPE_GROUPS[alias]):
                if member not in resolved:
                    resolved.append(member)
            continue
        normalized = SCOPE_ALIASES.get(alias, clean)
        if normalized and normalized not in resolved:
            resolved.append(normalized)
    if not resolved:
        resolved.extend(SCOPE_GROUPS["athena-google-readonly"])
    return resolved


def _code_verifier() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(48)).decode("ascii").rstrip("=")


def _code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _expiry_from_token_response(payload: dict[str, Any]) -> int | None:
    expires_in = payload.get("expires_in")
    if expires_in in (None, ""):
        return None
    return int(datetime.now(tz=timezone.utc).timestamp()) + int(expires_in) - 60


def init_settings_template(paths: AthenaPaths | None = None, *, force: bool = False) -> Path:
    resolved_paths = paths or default_paths()
    template = {
        "oauth": {
            "profile": "athena-google-full",
            "scopes": [],
            "include_granted_scopes": False,
        },
        "gmail": {
            "enabled": True,
            "query": "in:inbox category:primary newer_than:30d",
            "max_results": 15,
            "default_account": "primary",
            "accounts": [
                {
                    "label": "primary",
                    "email": "fleire@thirdteam.org",
                    "display_name": "Fleire",
                    "default": True,
                },
                {
                    "label": "athena",
                    "email": "athena@thirdteam.org",
                    "display_name": "Athena (send-as)",
                    "default": False,
                }
            ],
        },
        "calendar": {
            "enabled": True,
            "calendar_ids": ["primary"],
            "days_back": 0,
            "days_ahead": 14,
            "max_results": 25,
        },
        "contacts": {
            "enabled": True,
            "page_size": 200,
        },
        "drive": {
            "folders": [
                {
                    "id": "REPLACE_WITH_DRIVE_FOLDER_ID",
                    "name": "Athena Drive Mirror",
                }
            ]
        },
        "notebooklm": {
            "folder_id": "REPLACE_WITH_NOTEBOOKLM_EXPORT_FOLDER_ID",
            "name": "NotebookLM Exports",
        },
    }
    settings_path = resolved_paths.google_settings_path.expanduser().resolve()
    if settings_path.exists() and not force:
        return settings_path
    _ensure_dir(resolved_paths.google_dir)
    return _write_json(settings_path, template)


def load_sync_settings(paths: AthenaPaths | None = None) -> GoogleSyncSettings:
    resolved_paths = paths or default_paths()
    settings_path = resolved_paths.google_settings_path.expanduser().resolve()
    if not settings_path.exists():
        return GoogleSyncSettings(
            oauth_profile="athena-google-full",
            oauth_scopes=(),
            include_granted_scopes=False,
            gmail=GmailMirrorSettings(),
            calendar=CalendarSyncSettings(),
            contacts=ContactsSyncSettings(enabled=False),
            drive_folders=(),
            notebooklm_folder=None,
        )

    raw = json.loads(settings_path.read_text(encoding="utf-8"))
    oauth = raw.get("oauth") or {}
    oauth_profile = str(oauth.get("profile") or "athena-google-full")
    oauth_scopes = tuple(str(item).strip() for item in (oauth.get("scopes") or []) if str(item).strip())
    include_granted_scopes = bool(oauth.get("include_granted_scopes", False))
    gmail = raw.get("gmail") or {}
    gmail_accounts: list[GmailAccountSettings] = []
    for account in gmail.get("accounts") or []:
        label = str(account.get("label") or "").strip()
        if not label:
            continue
        gmail_accounts.append(
            GmailAccountSettings(
                label=label,
                email=str(account.get("email") or "").strip(),
                display_name=str(account.get("display_name") or account.get("name") or "").strip(),
                is_default=bool(account.get("default", False)),
                token_path=_resolve_config_path(resolved_paths.google_dir, account.get("token_path")),
                client_secrets_path=_resolve_config_path(resolved_paths.google_dir, account.get("client_secret_path")),
            )
        )
    default_account_label = str(
        gmail.get("default_account")
        or gmail.get("default_account_label")
        or next((account.label for account in gmail_accounts if account.is_default), "")
        or (gmail_accounts[0].label if gmail_accounts else GmailMirrorSettings.default_account_label)
    ).strip()
    gmail_settings = GmailMirrorSettings(
        enabled=bool(gmail.get("enabled", False)),
        query=str(gmail.get("query") or GmailMirrorSettings.query),
        max_results=int(gmail.get("max_results") or GmailMirrorSettings.max_results),
        default_account_label=default_account_label or GmailMirrorSettings.default_account_label,
        accounts=tuple(gmail_accounts),
    )
    calendar = raw.get("calendar") or {}
    calendar_ids = tuple(str(item).strip() for item in (calendar.get("calendar_ids") or ["primary"]) if str(item).strip())
    calendar_settings = CalendarSyncSettings(
        enabled=bool(calendar.get("enabled", False)),
        calendar_ids=calendar_ids or CalendarSyncSettings.calendar_ids,
        days_back=max(0, int(calendar.get("days_back") or CalendarSyncSettings.days_back)),
        days_ahead=max(0, int(calendar.get("days_ahead") or CalendarSyncSettings.days_ahead)),
        max_results=max(1, int(calendar.get("max_results") or CalendarSyncSettings.max_results)),
    )
    contacts = raw.get("contacts") or {}
    contacts_settings = ContactsSyncSettings(
        enabled=bool(contacts.get("enabled", ContactsSyncSettings.enabled)),
        page_size=max(1, int(contacts.get("page_size") or ContactsSyncSettings.page_size)),
    )

    drive_folders: list[DriveFolderSpec] = []
    for folder in (raw.get("drive") or {}).get("folders") or []:
        folder_id = str(folder.get("id") or "").strip()
        if not folder_id or folder_id.startswith("REPLACE_WITH_"):
            continue
        drive_folders.append(
            DriveFolderSpec(
                id=folder_id,
                name=str(folder.get("name") or folder_id),
            )
        )

    notebooklm_raw = raw.get("notebooklm") or {}
    notebook_folder_id = str(notebooklm_raw.get("folder_id") or "").strip()
    notebook_folder = None
    if notebook_folder_id and not notebook_folder_id.startswith("REPLACE_WITH_"):
        notebook_folder = DriveFolderSpec(
            id=notebook_folder_id,
            name=str(notebooklm_raw.get("name") or "NotebookLM Exports"),
        )

    return GoogleSyncSettings(
        oauth_profile=oauth_profile,
        oauth_scopes=oauth_scopes,
        include_granted_scopes=include_granted_scopes,
        gmail=gmail_settings,
        calendar=calendar_settings,
        contacts=contacts_settings,
        drive_folders=tuple(drive_folders),
        notebooklm_folder=notebook_folder,
    )


def _fallback_gmail_account() -> GmailAccountSettings:
    return GmailAccountSettings(
        label="primary",
        email="",
        display_name="Primary Gmail",
        is_default=True,
    )


def resolve_gmail_account(
    paths: AthenaPaths | None = None,
    *,
    account_label: str | None = None,
) -> GmailAccountSettings:
    resolved_paths = paths or default_paths()
    settings = load_sync_settings(resolved_paths)
    accounts = list(settings.gmail.accounts)
    if not accounts:
        fallback = _fallback_gmail_account()
        requested = str(account_label or "").strip()
        if requested and requested not in {"primary", fallback.label}:
            raise GoogleAuthError(f"Unknown Gmail account label: {requested}")
        return fallback

    requested_label = str(account_label or settings.gmail.default_account_label or "").strip()
    if requested_label:
        for account in accounts:
            if account.label == requested_label:
                return account
        raise GoogleAuthError(f"Unknown Gmail account label: {requested_label}")
    for account in accounts:
        if account.is_default:
            return account
    return accounts[0]


def _paths_for_gmail_account(paths: AthenaPaths, account: GmailAccountSettings) -> AthenaPaths:
    token_path = account.token_path or paths.google_token_path
    client_secrets_path = account.client_secrets_path or paths.google_client_secrets_path
    return replace(
        paths,
        google_token_path=token_path.expanduser().resolve(),
        google_client_secrets_path=client_secrets_path.expanduser().resolve(),
    )


def list_gmail_accounts(paths: AthenaPaths | None = None) -> list[dict[str, Any]]:
    resolved_paths = paths or default_paths()
    settings = load_sync_settings(resolved_paths)
    if not settings.gmail.accounts:
        fallback = _fallback_gmail_account()
        fallback_paths = _paths_for_gmail_account(resolved_paths, fallback)
        return [
            {
                "label": fallback.label,
                "email": fallback.email,
                "display_name": fallback.display_name,
                "is_default": True,
                "token_path": str(fallback_paths.google_token_path),
                "client_secret_path": str(fallback_paths.google_client_secrets_path),
            }
        ]

    default_account = resolve_gmail_account(resolved_paths)
    rows: list[dict[str, Any]] = []
    for account in settings.gmail.accounts:
        account_paths = _paths_for_gmail_account(resolved_paths, account)
        rows.append(
            {
                "label": account.label,
                "email": account.email,
                "display_name": account.display_name or account.label,
                "is_default": account.label == default_account.label,
                "token_path": str(account_paths.google_token_path),
                "client_secret_path": str(account_paths.google_client_secrets_path),
            }
        )
    return rows


def requested_scopes(paths: AthenaPaths | None = None, scopes: Iterable[str] | None = None) -> list[str]:
    resolved_paths = paths or default_paths()
    explicit = list(scopes or [])
    if explicit:
        return _normalize_scopes(explicit)
    settings = load_sync_settings(resolved_paths)
    requested = [settings.oauth_profile, *settings.oauth_scopes]
    return _normalize_scopes(requested)


def build_auth_url(
    paths: AthenaPaths | None = None,
    *,
    scopes: Iterable[str] | None = None,
    redirect_uri: str = DEFAULT_REDIRECT_URI,
    account_label: str | None = None,
) -> dict[str, Any]:
    resolved_paths = paths or default_paths()
    account = resolve_gmail_account(resolved_paths, account_label=account_label)
    account_paths = _paths_for_gmail_account(resolved_paths, account)
    client = _load_client_config(account_paths)
    normalized_scopes = requested_scopes(resolved_paths, scopes)
    settings = load_sync_settings(resolved_paths)
    session = {
        "state": secrets.token_urlsafe(24),
        "code_verifier": _code_verifier(),
        "redirect_uri": redirect_uri,
        "scopes": normalized_scopes,
    }
    params = {
        "client_id": client["client_id"],
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(normalized_scopes),
        "access_type": "offline",
        "include_granted_scopes": "true" if settings.include_granted_scopes else "false",
        "prompt": "consent",
        "state": session["state"],
        "code_challenge": _code_challenge(session["code_verifier"]),
        "code_challenge_method": "S256",
    }
    if account.email:
        params["login_hint"] = account.email
    auth_url = f"{client['auth_uri']}?{urllib.parse.urlencode(params)}"
    session["auth_url"] = auth_url
    session_path = _oauth_session_path_for_account(resolved_paths, account.label)
    _ensure_dir(resolved_paths.google_dir)
    _write_json(session_path, session)
    return {
        "auth_url": auth_url,
        "account_label": account.label,
        "account_email": account.email,
        "session_path": str(session_path),
        "client_secret_path": str(account_paths.google_client_secrets_path),
        "token_path": str(account_paths.google_token_path),
        "redirect_uri": redirect_uri,
        "scopes": normalized_scopes,
    }


def exchange_code(
    code: str,
    paths: AthenaPaths | None = None,
    *,
    transport: UrlLibTransport | None = None,
    account_label: str | None = None,
) -> dict[str, Any]:
    resolved_paths = paths or default_paths()
    account = resolve_gmail_account(resolved_paths, account_label=account_label)
    account_paths = _paths_for_gmail_account(resolved_paths, account)
    client = _load_client_config(account_paths)
    session_path = _oauth_session_path_for_account(resolved_paths, account.label)
    if not session_path.exists():
        raise GoogleAuthError(f"Missing OAuth session file: {session_path}")
    session = json.loads(session_path.read_text(encoding="utf-8"))
    transport = transport or UrlLibTransport()
    payload = urllib.parse.urlencode(
        {
            "client_id": client["client_id"],
            "client_secret": client["client_secret"],
            "code": code.strip(),
            "code_verifier": session["code_verifier"],
            "grant_type": "authorization_code",
            "redirect_uri": session["redirect_uri"],
        }
    ).encode("utf-8")
    token = transport.request_json(
        "POST",
        client["token_uri"],
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data=payload,
    )
    token["expiry"] = _expiry_from_token_response(token)
    token["scope"] = token.get("scope") or " ".join(session.get("scopes") or [])
    _write_json(account_paths.google_token_path, token)
    return {
        "account_label": account.label,
        "account_email": account.email,
        "token_path": str(account_paths.google_token_path),
        "scopes": token["scope"],
        "has_refresh_token": bool(token.get("refresh_token")),
    }


def authorize_with_local_browser(
    paths: AthenaPaths | None = None,
    *,
    scopes: Iterable[str] | None = None,
    account_label: str | None = None,
    redirect_uri: str = DEFAULT_REDIRECT_URI,
    open_browser: bool = True,
    timeout_seconds: int = 240,
    transport: UrlLibTransport | None = None,
) -> dict[str, Any]:
    resolved_paths = paths or default_paths()
    auth = build_auth_url(
        resolved_paths,
        scopes=scopes,
        redirect_uri=redirect_uri,
        account_label=account_label,
    )
    callback_url = urllib.parse.urlsplit(redirect_uri)
    callback_host = callback_url.hostname or "127.0.0.1"
    callback_port = callback_url.port
    callback_path = callback_url.path or "/"
    if callback_port is None:
        raise GoogleAuthError("Redirect URI must include an explicit localhost port for local browser auth.")
    if callback_host not in {"127.0.0.1", "localhost"}:
        raise GoogleAuthError("Local browser auth only supports localhost redirect URIs.")

    session_path = Path(auth["session_path"]).expanduser().resolve()
    session_payload = json.loads(session_path.read_text(encoding="utf-8"))
    expected_state = str(session_payload.get("state") or "")
    result: dict[str, str] = {}
    event = threading.Event()

    class OAuthCallbackHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # pragma: no cover - keep console clean
            return

        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlsplit(self.path)
            if parsed.path != callback_path:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            result["code"] = (params.get("code") or [""])[-1]
            result["state"] = (params.get("state") or [""])[-1]
            result["error"] = (params.get("error") or [""])[-1]
            result["error_description"] = (params.get("error_description") or [""])[-1]
            if result["error"]:
                body = "<h1>Google auth failed</h1><p>You can return to Athena and try again.</p>"
            else:
                body = "<h1>Athena Google auth complete</h1><p>You can close this tab and return to Athena.</p>"
            encoded = body.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
            event.set()

    try:
        httpd = HTTPServer((callback_host, callback_port), OAuthCallbackHandler)
    except OSError as exc:
        raise GoogleAuthError(f"Could not bind OAuth callback server on {callback_host}:{callback_port}: {exc}") from exc

    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        if open_browser:
            webbrowser.open(auth["auth_url"], new=1, autoraise=True)
        if not event.wait(timeout_seconds):
            raise GoogleAuthError(
                f"Timed out waiting for Google auth callback on {callback_host}:{callback_port}."
            )
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)

    if result.get("error"):
        detail = result.get("error_description") or result["error"]
        raise GoogleAuthError(f"Google auth was not completed: {detail}")
    if result.get("state") != expected_state:
        raise GoogleAuthError("OAuth callback state did not match the stored session.")
    code = str(result.get("code") or "").strip()
    if not code:
        raise GoogleAuthError("OAuth callback did not include an authorization code.")

    exchange = exchange_code(
        code,
        resolved_paths,
        transport=transport,
        account_label=auth["account_label"],
    )
    return {
        **auth,
        **exchange,
        "opened_browser": open_browser,
        "timeout_seconds": timeout_seconds,
    }


def ensure_access_token(
    paths: AthenaPaths | None = None,
    *,
    transport: UrlLibTransport | None = None,
    account_label: str | None = None,
) -> str:
    resolved_paths = paths or default_paths()
    account = resolve_gmail_account(resolved_paths, account_label=account_label)
    account_paths = _paths_for_gmail_account(resolved_paths, account)
    token_data = _load_token_data(account_paths)
    expiry = int(token_data.get("expiry") or 0)
    now_ts = int(datetime.now(tz=timezone.utc).timestamp())
    access_token = str(token_data.get("access_token") or "")
    if access_token and expiry and expiry > now_ts:
        return access_token

    refresh_token = str(token_data.get("refresh_token") or "")
    if not refresh_token:
        raise GoogleAuthError("Google token file has no refresh_token; re-run the OAuth flow.")

    client = _load_client_config(account_paths)
    transport = transport or UrlLibTransport()
    payload = urllib.parse.urlencode(
        {
            "client_id": client["client_id"],
            "client_secret": client["client_secret"],
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }
    ).encode("utf-8")
    refreshed = transport.request_json(
        "POST",
        client["token_uri"],
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data=payload,
    )
    token_data.update(refreshed)
    token_data["refresh_token"] = refresh_token
    token_data["expiry"] = _expiry_from_token_response(refreshed)
    _write_json(account_paths.google_token_path, token_data)
    return str(token_data["access_token"])


def oauth_status(paths: AthenaPaths | None = None, *, account_label: str | None = None) -> dict[str, Any]:
    resolved_paths = paths or default_paths()
    settings = load_sync_settings(resolved_paths)
    account = resolve_gmail_account(resolved_paths, account_label=account_label)
    account_paths = _paths_for_gmail_account(resolved_paths, account)
    status = {
        "google_dir": str(resolved_paths.google_dir),
        "settings_path": str(resolved_paths.google_settings_path),
        "account_label": account.label,
        "account_email": account.email,
        "configured_accounts": list_gmail_accounts(resolved_paths),
        "client_secret_path": str(account_paths.google_client_secrets_path),
        "client_secret_present": account_paths.google_client_secrets_path.exists(),
        "token_path": str(account_paths.google_token_path),
        "token_present": account_paths.google_token_path.exists(),
        "requested_scopes": requested_scopes(resolved_paths),
        "oauth_profile": settings.oauth_profile,
        "include_granted_scopes": settings.include_granted_scopes,
    }
    if account_paths.google_token_path.exists():
        token = _load_token_data(account_paths)
        status["granted_scopes"] = str(token.get("scope") or "").split()
        status["has_refresh_token"] = bool(token.get("refresh_token"))
        status["token_expiry"] = token.get("expiry")
    return status


def _auth_headers(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"}


def _decode_b64url(value: str | None) -> str:
    if not value:
        return ""
    padded = value + "=" * ((4 - len(value) % 4) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8", errors="replace")


def _strip_html(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", value)).strip()


def _gmail_header(payload: dict[str, Any], name: str) -> str:
    for header in payload.get("headers") or []:
        if str(header.get("name") or "").lower() == name.lower():
            return str(header.get("value") or "")
    return ""


def _gmail_body(payload: dict[str, Any]) -> str:
    collected: list[str] = []
    html_fallback: list[str] = []

    def walk(part: dict[str, Any]) -> None:
        mime_type = str(part.get("mimeType") or "")
        body = part.get("body") or {}
        data = body.get("data")
        if mime_type.startswith("multipart/"):
            for child in part.get("parts") or []:
                walk(child)
            return
        if mime_type == "text/plain" and data:
            text = _decode_b64url(str(data)).strip()
            if text:
                collected.append(text)
        elif mime_type == "text/html" and data:
            html_text = _strip_html(_decode_b64url(str(data)))
            if html_text:
                html_fallback.append(html_text)
        for child in part.get("parts") or []:
            walk(child)

    walk(payload)
    text = "\n\n".join(item for item in collected if item)
    if text:
        return text
    return "\n\n".join(item for item in html_fallback if item)


def _gmail_raw_message(
    *,
    to_recipients: str,
    subject: str,
    body_text: str,
    cc_recipients: str | None = None,
    bcc_recipients: str | None = None,
    from_address: str | None = None,
    from_name: str | None = None,
) -> str:
    message = EmailMessage()
    if from_address:
        message["From"] = formataddr((from_name or "", from_address))
    message["To"] = to_recipients
    if cc_recipients:
        message["Cc"] = cc_recipients
    if bcc_recipients:
        message["Bcc"] = bcc_recipients
    message["Subject"] = subject
    message.set_content(body_text)
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
    return raw.rstrip("=")


def create_gmail_draft(
    *,
    to_recipients: str,
    subject: str,
    body_text: str,
    paths: AthenaPaths | None = None,
    cc_recipients: str | None = None,
    bcc_recipients: str | None = None,
    transport: UrlLibTransport | None = None,
    account_label: str | None = None,
) -> dict[str, Any]:
    resolved_paths = paths or default_paths()
    account = resolve_gmail_account(resolved_paths, account_label=account_label)
    transport = transport or UrlLibTransport()
    access_token = ensure_access_token(resolved_paths, transport=transport, account_label=account.label)
    payload = json.dumps(
        {
            "message": {
                "raw": _gmail_raw_message(
                    to_recipients=to_recipients,
                    subject=subject,
                    body_text=body_text,
                    cc_recipients=cc_recipients,
                    bcc_recipients=bcc_recipients,
                    from_address=account.email or None,
                    from_name=account.display_name or None,
                )
            }
        }
    ).encode("utf-8")
    response = transport.request_json(
        "POST",
        "https://gmail.googleapis.com/gmail/v1/users/me/drafts",
        headers={**_auth_headers(access_token), "Content-Type": "application/json"},
        data=payload,
    )
    message = response.get("message") or {}
    return {
        "account_label": account.label,
        "draft_id": str(response.get("id") or "").strip(),
        "message_id": str(message.get("id") or "").strip(),
        "thread_id": str(message.get("threadId") or "").strip(),
        "external_url": "https://mail.google.com/mail/u/0/#drafts",
    }


def send_gmail_draft(
    *,
    draft_id: str,
    paths: AthenaPaths | None = None,
    transport: UrlLibTransport | None = None,
    account_label: str | None = None,
) -> dict[str, Any]:
    resolved_paths = paths or default_paths()
    account = resolve_gmail_account(resolved_paths, account_label=account_label)
    transport = transport or UrlLibTransport()
    access_token = ensure_access_token(resolved_paths, transport=transport, account_label=account.label)
    payload = json.dumps({"id": draft_id.strip()}).encode("utf-8")
    response = transport.request_json(
        "POST",
        "https://gmail.googleapis.com/gmail/v1/users/me/drafts/send",
        headers={**_auth_headers(access_token), "Content-Type": "application/json"},
        data=payload,
    )
    return {
        "account_label": account.label,
        "message_id": str(response.get("id") or "").strip(),
        "thread_id": str(response.get("threadId") or "").strip(),
        "label_ids": list(response.get("labelIds") or []),
        "external_url": "https://mail.google.com/mail/u/0/#sent",
    }


def _iso_timestamp(raw_ms: str | int | None) -> str:
    if not raw_ms:
        return ""
    seconds = int(raw_ms) / 1000
    return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()


def _rfc3339_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _calendar_event_start(event: dict[str, Any]) -> tuple[str, bool]:
    start = event.get("start") or {}
    date_time = str(start.get("dateTime") or "").strip()
    if date_time:
        return date_time, False
    return str(start.get("date") or "").strip(), True


def _calendar_event_end(event: dict[str, Any]) -> str:
    end = event.get("end") or {}
    return str(end.get("dateTime") or end.get("date") or "").strip()


def _calendar_event_when(event: dict[str, Any]) -> str:
    start, all_day = _calendar_event_start(event)
    end = _calendar_event_end(event)
    if all_day:
        return f"{start} (all day)" if start else "all day"
    if start and end:
        return f"{start} -> {end}"
    return start or end or "unknown"


def _contact_primary_text(items: list[dict[str, Any]] | None, field_name: str) -> str:
    for item in items or []:
        value = str(item.get(field_name) or "").strip()
        if value:
            return value
    return ""


def _contact_person_id(person: dict[str, Any]) -> str:
    resource_name = str(person.get("resourceName") or "").strip()
    candidate = resource_name or _contact_primary_text(person.get("emailAddresses"), "value") or _contact_primary_text(person.get("names"), "displayName")
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", candidate).strip("-") or "contact"
    return f"person-google-{safe.lower()}"


def _write_text(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _google_error_message(exc: Exception) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")
            payload = json.loads(detail)
            message = str(((payload.get("error") or {}).get("message")) or "").strip()
            if message:
                return message
        except Exception:
            pass
        return detail.strip() or str(exc)
    return str(exc)


def _mirror_gmail(conn, *, paths: AthenaPaths, settings: GmailMirrorSettings, transport: UrlLibTransport, access_token: str) -> dict[str, int]:
    output_dir = _ensure_dir(paths.google_mirror_dir / "gmail")
    params = urllib.parse.urlencode(
        {
            "q": settings.query,
            "maxResults": settings.max_results,
            "labelIds": "INBOX",
        }
    )
    listing = transport.request_json(
        "GET",
        f"https://gmail.googleapis.com/gmail/v1/users/me/messages?{params}",
        headers=_auth_headers(access_token),
    )
    messages = listing.get("messages") or []
    summary_lines = [
        "# Gmail Inbox Mirror",
        "",
        f"- query: {settings.query}",
        f"- mirrored_messages: {len(messages)}",
        "",
    ]

    mirrored = 0
    for item in messages:
        message_id = str(item.get("id") or "").strip()
        if not message_id:
            continue
        detail = transport.request_json(
            "GET",
            f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{urllib.parse.quote(message_id)}?format=full",
            headers=_auth_headers(access_token),
        )
        payload = detail.get("payload") or {}
        subject = _gmail_header(payload, "Subject") or f"Gmail message {message_id}"
        sender = _gmail_header(payload, "From")
        date = _gmail_header(payload, "Date") or _iso_timestamp(detail.get("internalDate"))
        snippet = str(detail.get("snippet") or "").strip()
        labels = ", ".join(detail.get("labelIds") or [])
        body = _gmail_body(payload)
        view_url = f"https://mail.google.com/mail/u/0/#inbox/{message_id}"
        markdown = "\n".join(
            [
                f"# {subject}",
                "",
                f"- from: {sender or 'unknown'}",
                f"- date: {date or 'unknown'}",
                f"- labels: {labels or 'none'}",
                f"- gmail_url: {view_url}",
                "",
                "## Snippet",
                "",
                snippet or "(no snippet)",
                "",
                "## Body",
                "",
                body or "(no plain-text body found)",
                "",
            ]
        )
        mirror_path = _write_text(output_dir / f"{message_id}.md", markdown)
        doc_id = choose_document_id(conn, mirror_path, f"gmail-{message_id}")
        upsert_source_document(
            conn,
            doc_id=doc_id,
            kind="gmail_message",
            title=subject,
            path=mirror_path,
            source_system="gmail",
            is_authoritative=False,
            summary=text_summary(body or snippet or subject),
            external_url=view_url,
        )
        dedupe_source_documents(conn, mirror_path, doc_id)
        summary_lines.append(f"- {subject} — {sender or 'unknown'}")
        mirrored += 1

    summary_path = _write_text(output_dir / "inbox-summary.md", "\n".join(summary_lines))
    doc_id = choose_document_id(conn, summary_path, "gmail-inbox-summary")
    upsert_source_document(
        conn,
        doc_id=doc_id,
        kind="gmail_mailbox",
        title="Gmail Inbox Summary",
        path=summary_path,
        source_system="gmail",
        is_authoritative=False,
        summary=f"{mirrored} mirrored inbox messages",
        external_url="https://mail.google.com/mail/u/0/#inbox",
    )
    dedupe_source_documents(conn, summary_path, doc_id)
    return {"gmail_messages": mirrored}


def _drive_list_folder(
    *,
    folder_id: str,
    access_token: str,
    transport: UrlLibTransport,
) -> list[dict[str, Any]]:
    query = f"'{folder_id}' in parents and trashed = false"
    params = urllib.parse.urlencode(
        {
            "q": query,
            "pageSize": 50,
            "fields": "files(id,name,mimeType,webViewLink,modifiedTime)",
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        }
    )
    response = transport.request_json(
        "GET",
        f"https://www.googleapis.com/drive/v3/files?{params}",
        headers=_auth_headers(access_token),
    )
    return list(response.get("files") or [])


def list_drive_folders(
    *,
    paths: AthenaPaths | None = None,
    transport: UrlLibTransport | None = None,
    parent_id: str = "root",
    query: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    resolved_paths = paths or default_paths()
    transport = transport or UrlLibTransport()
    access_token = ensure_access_token(resolved_paths, transport=transport)
    conditions = [f"'{parent_id}' in parents", "mimeType='application/vnd.google-apps.folder'", "trashed = false"]
    if query:
        safe_query = str(query).replace("'", "\\'")
        conditions.append(f"name contains '{safe_query}'")
    params = urllib.parse.urlencode(
        {
            "q": " and ".join(conditions),
            "pageSize": limit,
            "fields": "files(id,name,webViewLink,modifiedTime,parents)",
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
            "orderBy": "modifiedTime desc,name",
        }
    )
    response = transport.request_json(
        "GET",
        f"https://www.googleapis.com/drive/v3/files?{params}",
        headers=_auth_headers(access_token),
    )
    return list(response.get("files") or [])


def _drive_download_text(
    *,
    file_id: str,
    mime_type: str,
    access_token: str,
    transport: UrlLibTransport,
) -> tuple[str, str] | None:
    headers = _auth_headers(access_token)
    if mime_type == "application/vnd.google-apps.document":
        data = transport.request_bytes(
            "GET",
            f"https://www.googleapis.com/drive/v3/files/{urllib.parse.quote(file_id)}/export?mimeType=text/plain",
            headers=headers,
        )
        return data.decode("utf-8", errors="replace"), ".txt"

    suffix_by_mime = {
        "text/plain": ".txt",
        "text/markdown": ".md",
        "application/json": ".json",
        "text/html": ".html",
    }
    suffix = suffix_by_mime.get(mime_type)
    if not suffix:
        return None
    data = transport.request_bytes(
        "GET",
        f"https://www.googleapis.com/drive/v3/files/{urllib.parse.quote(file_id)}?alt=media",
        headers=headers,
    )
    return data.decode("utf-8", errors="replace"), suffix


def _calendar_list_events(
    *,
    calendar_id: str,
    access_token: str,
    transport: UrlLibTransport,
    days_back: int,
    days_ahead: int,
    max_results: int,
) -> list[dict[str, Any]]:
    now = datetime.now(tz=timezone.utc)
    params = urllib.parse.urlencode(
        {
            "singleEvents": "true",
            "orderBy": "startTime",
            "timeMin": _rfc3339_timestamp(now - timedelta(days=days_back)),
            "timeMax": _rfc3339_timestamp(now + timedelta(days=days_ahead)),
            "maxResults": max_results,
            "fields": "items(id,status,summary,description,location,htmlLink,start,end,organizer(displayName,email),attendees(email,responseStatus))",
        }
    )
    response = transport.request_json(
        "GET",
        f"https://www.googleapis.com/calendar/v3/calendars/{urllib.parse.quote(calendar_id, safe='')}/events?{params}",
        headers=_auth_headers(access_token),
    )
    return list(response.get("items") or [])


def _mirror_calendar(
    conn,
    *,
    paths: AthenaPaths,
    settings: CalendarSyncSettings,
    transport: UrlLibTransport,
    access_token: str,
) -> dict[str, int]:
    base_dir = _ensure_dir(paths.google_mirror_dir / "calendar")
    events_dir = _ensure_dir(base_dir / "events")
    summary_lines = [
        "# Upcoming Calendar Agenda",
        "",
        f"- calendar_ids: {', '.join(settings.calendar_ids)}",
        f"- days_back: {settings.days_back}",
        f"- days_ahead: {settings.days_ahead}",
        "",
    ]

    mirrored = 0
    for calendar_id in settings.calendar_ids:
        events = _calendar_list_events(
            calendar_id=calendar_id,
            access_token=access_token,
            transport=transport,
            days_back=settings.days_back,
            days_ahead=settings.days_ahead,
            max_results=settings.max_results,
        )
        summary_lines.append(f"## {calendar_id}")
        summary_lines.append("")
        if not events:
            summary_lines.append("- No upcoming events")
            summary_lines.append("")
            continue
        for event in events:
            if str(event.get("status") or "").lower() == "cancelled":
                continue
            event_id = str(event.get("id") or "").strip()
            if not event_id:
                continue
            title = str(event.get("summary") or f"Calendar event {event_id}").strip()
            when = _calendar_event_when(event)
            location = str(event.get("location") or "").strip()
            description = str(event.get("description") or "").strip()
            html_link = str(event.get("htmlLink") or "").strip()
            organizer = event.get("organizer") or {}
            organizer_name = str(organizer.get("displayName") or organizer.get("email") or "").strip()
            attendees = event.get("attendees") or []
            attendee_lines = [
                f"- {str(item.get('email') or 'unknown')} ({str(item.get('responseStatus') or 'unknown')})"
                for item in attendees
                if str(item.get("email") or "").strip()
            ]
            markdown_parts = [
                f"# {title}",
                "",
                f"- calendar_id: {calendar_id}",
                f"- when: {when}",
                f"- location: {location or 'n/a'}",
                f"- organizer: {organizer_name or 'unknown'}",
                f"- status: {str(event.get('status') or 'confirmed')}",
                f"- event_url: {html_link or 'n/a'}",
                "",
                "## Description",
                "",
                description or "(no description)",
            ]
            if attendee_lines:
                markdown_parts.extend(["", "## Attendees", "", *attendee_lines])
            markdown_parts.append("")
            safe_event_id = re.sub(r"[^A-Za-z0-9._-]+", "-", event_id).strip("-") or "event"
            mirror_path = _write_text(events_dir / f"{safe_event_id}.md", "\n".join(markdown_parts))
            doc_id = choose_document_id(conn, mirror_path, f"gcal-{safe_event_id}")
            upsert_source_document(
                conn,
                doc_id=doc_id,
                kind="calendar_event",
                title=title,
                path=mirror_path,
                source_system="gcal",
                is_authoritative=False,
                summary=text_summary(description or when or title),
                external_url=html_link,
            )
            dedupe_source_documents(conn, mirror_path, doc_id)
            summary_lines.append(f"- {when} — {title}")
            mirrored += 1
        summary_lines.append("")

    summary_path = _write_text(base_dir / "upcoming-summary.md", "\n".join(summary_lines))
    doc_id = choose_document_id(conn, summary_path, "gcal-upcoming-summary")
    upsert_source_document(
        conn,
        doc_id=doc_id,
        kind="calendar_agenda",
        title="Upcoming Calendar Agenda",
        path=summary_path,
        source_system="gcal",
        is_authoritative=False,
        summary=f"{mirrored} mirrored calendar events",
        external_url="https://calendar.google.com/calendar/u/0/r",
    )
    dedupe_source_documents(conn, summary_path, doc_id)
    return {"calendar_events": mirrored}


def _contacts_list(
    *,
    access_token: str,
    transport: UrlLibTransport,
    page_size: int,
    page_token: str | None = None,
) -> dict[str, Any]:
    params = {
        "pageSize": page_size,
        "personFields": "names,emailAddresses,organizations,biographies",
        "sortOrder": "LAST_MODIFIED_DESCENDING",
    }
    if page_token:
        params["pageToken"] = page_token
    response = transport.request_json(
        "GET",
        f"https://people.googleapis.com/v1/people/me/connections?{urllib.parse.urlencode(params)}",
        headers=_auth_headers(access_token),
    )
    return dict(response or {})


def _mirror_contacts(
    conn,
    *,
    paths: AthenaPaths,
    settings: ContactsSyncSettings,
    transport: UrlLibTransport,
    access_token: str,
) -> dict[str, int]:
    base_dir = _ensure_dir(paths.google_mirror_dir / "contacts")
    summary_lines = [
        "# Google Contacts Mirror",
        "",
        f"- page_size: {settings.page_size}",
        "",
    ]

    mirrored = 0
    page_token: str | None = None
    while True:
        payload = _contacts_list(
            access_token=access_token,
            transport=transport,
            page_size=settings.page_size,
            page_token=page_token,
        )
        connections = payload.get("connections") or []
        for person in connections:
            name = _contact_primary_text(person.get("names"), "displayName") or "Unnamed contact"
            email = _contact_primary_text(person.get("emailAddresses"), "value")
            organization = _contact_primary_text(person.get("organizations"), "name")
            bio = _contact_primary_text(person.get("biographies"), "value")
            person_id = _contact_person_id(person)
            slug = person_id.removeprefix("person-")
            markdown_lines = [
                f"# {name}",
                "",
                f"- email: {email or 'n/a'}",
                f"- organization: {organization or 'n/a'}",
                f"- resource_name: {str(person.get('resourceName') or 'n/a')}",
                "",
            ]
            if bio:
                markdown_lines.extend(["## Notes", "", bio, ""])
            mirror_path = _write_text(base_dir / f"{person_id}.md", "\n".join(markdown_lines))
            doc_id = choose_document_id(conn, mirror_path, person_id)
            upsert_source_document(
                conn,
                doc_id=doc_id,
                kind="contact_profile",
                title=name,
                path=mirror_path,
                source_system="gpeople",
                is_authoritative=False,
                summary=text_summary(" ".join(part for part in [name, email, organization, bio] if part)),
                external_url="https://contacts.google.com/",
            )
            dedupe_source_documents(conn, mirror_path, doc_id)

            existing = conn.execute("SELECT created_at FROM people WHERE id = ?", (person_id,)).fetchone()
            created_at = int(existing["created_at"]) if existing else int(datetime.now(tz=timezone.utc).timestamp())
            conn.execute(
                """
                INSERT INTO people (
                  id, slug, name, relationship_type, importance_score, notes, contact_rule, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  slug = excluded.slug,
                  name = excluded.name,
                  relationship_type = excluded.relationship_type,
                  importance_score = excluded.importance_score,
                  notes = excluded.notes,
                  contact_rule = excluded.contact_rule,
                  updated_at = excluded.updated_at
                """,
                (
                    person_id,
                    slug,
                    name,
                    "google_contact",
                    10 if email else 1,
                    organization or bio or None,
                    email or None,
                    created_at,
                    int(datetime.now(tz=timezone.utc).timestamp()),
                ),
            )
            summary_lines.append(f"- {name} — {email or organization or 'no email'}")
            mirrored += 1
        page_token = str(payload.get("nextPageToken") or "").strip() or None
        if not page_token:
            break

    summary_path = _write_text(base_dir / "contacts-summary.md", "\n".join(summary_lines))
    doc_id = choose_document_id(conn, summary_path, "gpeople-contacts-summary")
    upsert_source_document(
        conn,
        doc_id=doc_id,
        kind="contacts_summary",
        title="Google Contacts Summary",
        path=summary_path,
        source_system="gpeople",
        is_authoritative=False,
        summary=f"{mirrored} mirrored Google contacts",
        external_url="https://contacts.google.com/",
    )
    dedupe_source_documents(conn, summary_path, doc_id)
    return {"contacts_synced": mirrored}


def _mirror_drive_folder(
    conn,
    *,
    folder: DriveFolderSpec,
    output_dir: Path,
    summary_dir: Path,
    access_token: str,
    transport: UrlLibTransport,
    source_system: str,
    kind: str,
    upsert_docs: bool,
) -> int:
    files = _drive_list_folder(folder_id=folder.id, access_token=access_token, transport=transport)
    summary_lines = [
        f"# {folder.name}",
        "",
        f"- folder_id: {folder.id}",
        f"- mirrored_files: {len(files)}",
        "",
    ]

    mirrored = 0
    output_dir = _ensure_dir(output_dir)
    summary_dir = _ensure_dir(summary_dir)
    for file_meta in files:
        file_id = str(file_meta.get("id") or "").strip()
        if not file_id:
            continue
        downloaded = _drive_download_text(
            file_id=file_id,
            mime_type=str(file_meta.get("mimeType") or ""),
            access_token=access_token,
            transport=transport,
        )
        if not downloaded:
            continue
        content, suffix = downloaded
        file_name = str(file_meta.get("name") or file_id)
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", file_name).strip("-") or file_id
        mirror_path = _write_text(output_dir / f"{safe_name}-{file_id}{suffix}", content)
        if upsert_docs:
            doc_id = choose_document_id(conn, mirror_path, f"{source_system}-{file_id}")
            upsert_source_document(
                conn,
                doc_id=doc_id,
                kind=kind,
                title=file_name,
                path=mirror_path,
                source_system=source_system,
                is_authoritative=False,
                summary=text_summary(content or file_name),
                external_url=str(file_meta.get("webViewLink") or ""),
            )
            dedupe_source_documents(conn, mirror_path, doc_id)
        summary_lines.append(f"- {file_name}")
        mirrored += 1

    summary_path = _write_text(summary_dir / f"{folder.name.lower().replace(' ', '-')}-summary.md", "\n".join(summary_lines))
    doc_id = choose_document_id(conn, summary_path, f"{source_system}-{folder.id}-summary")
    upsert_source_document(
        conn,
        doc_id=doc_id,
        kind=f"{kind}_summary",
        title=f"{folder.name} Summary",
        path=summary_path,
        source_system=source_system,
        is_authoritative=False,
        summary=f"{mirrored} mirrored files from {folder.name}",
    )
    dedupe_source_documents(conn, summary_path, doc_id)
    return mirrored


def mirror_google_sources(
    conn,
    *,
    paths: AthenaPaths | None = None,
    transport: UrlLibTransport | None = None,
) -> dict[str, Any]:
    resolved_paths = paths or default_paths()
    settings = load_sync_settings(resolved_paths)
    if (
        not settings.gmail.enabled
        and not settings.calendar.enabled
        and not settings.contacts.enabled
        and not settings.drive_folders
        and settings.notebooklm_folder is None
    ):
        return {
            "google_enabled": False,
            "gmail_messages": 0,
            "calendar_events": 0,
            "contacts_synced": 0,
            "drive_files": 0,
            "notebooklm_files": 0,
            "reason": "google settings are not configured",
        }

    transport = transport or UrlLibTransport()
    access_token = ensure_access_token(resolved_paths, transport=transport)
    summary = {
        "google_enabled": True,
        "gmail_messages": 0,
        "calendar_events": 0,
        "contacts_synced": 0,
        "drive_files": 0,
        "notebooklm_files": 0,
    }

    if settings.gmail.enabled:
        try:
            summary.update(
                _mirror_gmail(
                    conn,
                    paths=resolved_paths,
                    settings=settings.gmail,
                    transport=transport,
                    access_token=access_token,
                )
            )
        except (urllib.error.HTTPError, urllib.error.URLError) as exc:
            summary["gmail_error"] = _google_error_message(exc)

    if settings.calendar.enabled:
        try:
            summary.update(
                _mirror_calendar(
                    conn,
                    paths=resolved_paths,
                    settings=settings.calendar,
                    transport=transport,
                    access_token=access_token,
                )
            )
        except (urllib.error.HTTPError, urllib.error.URLError) as exc:
            summary["calendar_error"] = _google_error_message(exc)

    if settings.contacts.enabled:
        try:
            summary.update(
                _mirror_contacts(
                    conn,
                    paths=resolved_paths,
                    settings=settings.contacts,
                    transport=transport,
                    access_token=access_token,
                )
            )
        except (urllib.error.HTTPError, urllib.error.URLError) as exc:
            summary["contacts_error"] = _google_error_message(exc)

    drive_total = 0
    try:
        for folder in settings.drive_folders:
            drive_total += _mirror_drive_folder(
                conn,
                folder=folder,
                output_dir=resolved_paths.google_mirror_dir / "drive" / folder.name.lower().replace(" ", "-"),
                summary_dir=resolved_paths.google_mirror_dir / "drive",
                access_token=access_token,
                transport=transport,
                source_system="gdrive",
                kind="drive_file",
                upsert_docs=True,
            )
    except (urllib.error.HTTPError, urllib.error.URLError) as exc:
        summary["drive_error"] = _google_error_message(exc)
    summary["drive_files"] = drive_total

    if settings.notebooklm_folder is not None:
        try:
            summary["notebooklm_files"] = _mirror_drive_folder(
                conn,
                folder=settings.notebooklm_folder,
                output_dir=resolved_paths.notebooklm_export_dir,
                summary_dir=resolved_paths.google_mirror_dir / "notebooklm",
                access_token=access_token,
                transport=transport,
                source_system="NotebookLM",
                kind="notebooklm_export",
                upsert_docs=False,
            )
        except (urllib.error.HTTPError, urllib.error.URLError) as exc:
            summary["notebooklm_error"] = _google_error_message(exc)

    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Google auth and mirror helpers for Athena.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init-settings", help="Write a local Google settings template.")
    init_parser.add_argument("--force", action="store_true")

    auth_parser = subparsers.add_parser("auth-url", help="Generate the Google OAuth URL and store a PKCE session.")
    auth_parser.add_argument("--scope", dest="scopes", action="append", default=[])
    auth_parser.add_argument("--profile", default=None)
    auth_parser.add_argument("--account", default=None)
    auth_parser.add_argument("--redirect-uri", default=DEFAULT_REDIRECT_URI)

    login_parser = subparsers.add_parser("login", help="Open Google auth in a browser and exchange the code automatically.")
    login_parser.add_argument("--scope", dest="scopes", action="append", default=[])
    login_parser.add_argument("--profile", default=None)
    login_parser.add_argument("--account", default=None)
    login_parser.add_argument("--redirect-uri", default=DEFAULT_REDIRECT_URI)
    login_parser.add_argument("--timeout", type=int, default=240)
    login_parser.add_argument("--no-open-browser", action="store_true")

    exchange_parser = subparsers.add_parser("exchange-code", help="Exchange an OAuth code for local refresh/access tokens.")
    exchange_parser.add_argument("code")
    exchange_parser.add_argument("--account", default=None)

    folders_parser = subparsers.add_parser("list-folders", help="List Drive folders after OAuth is connected.")
    folders_parser.add_argument("--parent-id", default="root")
    folders_parser.add_argument("--query", default=None)
    folders_parser.add_argument("--limit", type=int, default=50)

    status_parser = subparsers.add_parser("status", help="Show local Google OAuth status for Athena.")
    status_parser.add_argument("--account", default=None)
    subparsers.add_parser("sync", help="Run the Google mirror pipeline once.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    paths = default_paths()
    if args.command == "init-settings":
        result = init_settings_template(paths, force=args.force)
        print(f"settings_path: {result}")
        return 0
    if args.command == "auth-url":
        scopes = list(args.scopes or [])
        if args.profile:
            scopes.insert(0, args.profile)
        result = build_auth_url(
            paths,
            scopes=scopes or None,
            redirect_uri=args.redirect_uri,
            account_label=args.account,
        )
        for key, value in result.items():
            print(f"{key}: {value}")
        return 0
    if args.command == "login":
        scopes = list(args.scopes or [])
        if args.profile:
            scopes.insert(0, args.profile)
        result = authorize_with_local_browser(
            paths,
            scopes=scopes or None,
            account_label=args.account,
            redirect_uri=args.redirect_uri,
            open_browser=not args.no_open_browser,
            timeout_seconds=args.timeout,
        )
        for key, value in result.items():
            print(f"{key}: {value}")
        return 0
    if args.command == "exchange-code":
        result = exchange_code(args.code, paths, account_label=args.account)
        for key, value in result.items():
            print(f"{key}: {value}")
        return 0
    if args.command == "status":
        result = oauth_status(paths, account_label=args.account)
        for key, value in result.items():
            print(f"{key}: {value}")
        return 0
    if args.command == "list-folders":
        rows = list_drive_folders(
            paths=paths,
            parent_id=args.parent_id,
            query=args.query,
            limit=args.limit,
        )
        for row in rows:
            print(json.dumps(row, ensure_ascii=True))
        return 0
    if args.command == "sync":
        from .db import connect_db, ensure_db

        ensure_db(paths=paths)
        with connect_db(paths.db_path) as conn:
            result = mirror_google_sources(conn, paths=paths)
            conn.commit()
        for key, value in result.items():
            print(f"{key}: {value}")
        return 0
    raise SystemExit(2)


if __name__ == "__main__":
    raise SystemExit(main())
