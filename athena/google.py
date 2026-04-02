from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import secrets
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
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


@dataclass(frozen=True)
class DriveFolderSpec:
    id: str
    name: str


@dataclass(frozen=True)
class GoogleSyncSettings:
    oauth_profile: str
    oauth_scopes: tuple[str, ...]
    gmail: GmailMirrorSettings
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
        },
        "gmail": {
            "enabled": True,
            "query": "in:inbox category:primary newer_than:30d",
            "max_results": 15,
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
            gmail=GmailMirrorSettings(),
            drive_folders=(),
            notebooklm_folder=None,
        )

    raw = json.loads(settings_path.read_text(encoding="utf-8"))
    oauth = raw.get("oauth") or {}
    oauth_profile = str(oauth.get("profile") or "athena-google-full")
    oauth_scopes = tuple(str(item).strip() for item in (oauth.get("scopes") or []) if str(item).strip())
    gmail = raw.get("gmail") or {}
    gmail_settings = GmailMirrorSettings(
        enabled=bool(gmail.get("enabled", False)),
        query=str(gmail.get("query") or GmailMirrorSettings.query),
        max_results=int(gmail.get("max_results") or GmailMirrorSettings.max_results),
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
        gmail=gmail_settings,
        drive_folders=tuple(drive_folders),
        notebooklm_folder=notebook_folder,
    )


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
) -> dict[str, Any]:
    resolved_paths = paths or default_paths()
    client = _load_client_config(resolved_paths)
    normalized_scopes = requested_scopes(resolved_paths, scopes)
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
        "include_granted_scopes": "true",
        "prompt": "consent",
        "state": session["state"],
        "code_challenge": _code_challenge(session["code_verifier"]),
        "code_challenge_method": "S256",
    }
    auth_url = f"{client['auth_uri']}?{urllib.parse.urlencode(params)}"
    session["auth_url"] = auth_url
    session_path = _oauth_session_path(resolved_paths)
    _ensure_dir(resolved_paths.google_dir)
    _write_json(session_path, session)
    return {
        "auth_url": auth_url,
        "session_path": str(session_path),
        "redirect_uri": redirect_uri,
        "scopes": normalized_scopes,
    }


def exchange_code(
    code: str,
    paths: AthenaPaths | None = None,
    *,
    transport: UrlLibTransport | None = None,
) -> dict[str, Any]:
    resolved_paths = paths or default_paths()
    client = _load_client_config(resolved_paths)
    session_path = _oauth_session_path(resolved_paths)
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
    _write_json(resolved_paths.google_token_path, token)
    return {
        "token_path": str(resolved_paths.google_token_path),
        "scopes": token["scope"],
        "has_refresh_token": bool(token.get("refresh_token")),
    }


def ensure_access_token(
    paths: AthenaPaths | None = None,
    *,
    transport: UrlLibTransport | None = None,
) -> str:
    resolved_paths = paths or default_paths()
    token_data = _load_token_data(resolved_paths)
    expiry = int(token_data.get("expiry") or 0)
    now_ts = int(datetime.now(tz=timezone.utc).timestamp())
    access_token = str(token_data.get("access_token") or "")
    if access_token and expiry and expiry > now_ts:
        return access_token

    refresh_token = str(token_data.get("refresh_token") or "")
    if not refresh_token:
        raise GoogleAuthError("Google token file has no refresh_token; re-run the OAuth flow.")

    client = _load_client_config(resolved_paths)
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
    _write_json(resolved_paths.google_token_path, token_data)
    return str(token_data["access_token"])


def oauth_status(paths: AthenaPaths | None = None) -> dict[str, Any]:
    resolved_paths = paths or default_paths()
    settings = load_sync_settings(resolved_paths)
    status = {
        "google_dir": str(resolved_paths.google_dir),
        "settings_path": str(resolved_paths.google_settings_path),
        "client_secret_path": str(resolved_paths.google_client_secrets_path),
        "client_secret_present": resolved_paths.google_client_secrets_path.exists(),
        "token_path": str(resolved_paths.google_token_path),
        "token_present": resolved_paths.google_token_path.exists(),
        "requested_scopes": requested_scopes(resolved_paths),
        "oauth_profile": settings.oauth_profile,
    }
    if resolved_paths.google_token_path.exists():
        token = _load_token_data(resolved_paths)
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


def _iso_timestamp(raw_ms: str | int | None) -> str:
    if not raw_ms:
        return ""
    seconds = int(raw_ms) / 1000
    return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()


def _write_text(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


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
    if not settings.gmail.enabled and not settings.drive_folders and settings.notebooklm_folder is None:
        return {
            "google_enabled": False,
            "gmail_messages": 0,
            "drive_files": 0,
            "notebooklm_files": 0,
            "reason": "google settings are not configured",
        }

    transport = transport or UrlLibTransport()
    access_token = ensure_access_token(resolved_paths, transport=transport)
    summary = {
        "google_enabled": True,
        "gmail_messages": 0,
        "drive_files": 0,
        "notebooklm_files": 0,
    }

    if settings.gmail.enabled:
        summary.update(
            _mirror_gmail(
                conn,
                paths=resolved_paths,
                settings=settings.gmail,
                transport=transport,
                access_token=access_token,
            )
        )

    drive_total = 0
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
    summary["drive_files"] = drive_total

    if settings.notebooklm_folder is not None:
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

    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Google auth and mirror helpers for Athena.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init-settings", help="Write a local Google settings template.")
    init_parser.add_argument("--force", action="store_true")

    auth_parser = subparsers.add_parser("auth-url", help="Generate the Google OAuth URL and store a PKCE session.")
    auth_parser.add_argument("--scope", dest="scopes", action="append", default=[])
    auth_parser.add_argument("--profile", default=None)
    auth_parser.add_argument("--redirect-uri", default=DEFAULT_REDIRECT_URI)

    exchange_parser = subparsers.add_parser("exchange-code", help="Exchange an OAuth code for local refresh/access tokens.")
    exchange_parser.add_argument("code")

    subparsers.add_parser("status", help="Show local Google OAuth status for Athena.")
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
        result = build_auth_url(paths, scopes=scopes or None, redirect_uri=args.redirect_uri)
        for key, value in result.items():
            print(f"{key}: {value}")
        return 0
    if args.command == "exchange-code":
        result = exchange_code(args.code, paths)
        for key, value in result.items():
            print(f"{key}: {value}")
        return 0
    if args.command == "status":
        result = oauth_status(paths)
        for key, value in result.items():
            print(f"{key}: {value}")
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
