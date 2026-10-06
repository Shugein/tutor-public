"""Общий Google-клиент: credentials из token.json, retry, папка Drive, MCP-обёртка.

token.json создаётся один раз скриптом scripts/google_auth.py.
"""

import asyncio
import time
from typing import Any, Callable

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from tutor.config import PROJECT_ROOT

# Скоупы, которые ЗАПРАШИВАЕТ scripts/google_auth.py при авторизации.
# Внимание: живой token.json может иметь набор УЖЕ, если выдан раньше —
# поэтому get_credentials ниже читает скоупы из самого файла, а не из этого
# списка. Иначе refresh отправляет незнакомый скоуп, Google отвечает
# invalid_scope, и ломаются все Google-тулы разом (поймано 2026-08-10 при
# добавлении drive.readonly: interview остался бы без Forms и Docs).
SCOPES = [
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/drive.readonly",  # авто-бэкап GoodNotes
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/forms.body",
    "https://www.googleapis.com/auth/forms.responses.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/calendar.events",
]
TOKEN_FILE = PROJECT_ROOT / "token.json"
CREDENTIALS_FILE = PROJECT_ROOT / "credentials.json"


DRIVE_READONLY = "https://www.googleapis.com/auth/drive.readonly"


def get_credentials() -> Credentials:
    if not TOKEN_FILE.exists():
        raise RuntimeError("token.json не найден — запусти scripts/google_auth.py")
    # без списка скоупов: берём те, что записаны в токене
    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE))
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN_FILE.write_text(creds.to_json())
    return creds


def has_scope(scope: str) -> bool:
    """Есть ли скоуп у выданного токена — чтобы сказать словами, а не трейсом."""
    return scope in (get_credentials().scopes or [])


def build_service(api: str, version: str):
    return build(api, version, credentials=get_credentials(), cache_discovery=False)


def gexec(request, retries: int = 1) -> Any:
    """request.execute() с одним ретраем на 429/5xx (спека: 1 ретрай, backoff)."""
    try:
        return request.execute()
    except HttpError as e:
        if retries > 0 and (e.resp.status == 429 or e.resp.status >= 500):
            time.sleep(2)
            return gexec(request, retries - 1)
        raise


def ensure_folder(path: str) -> str:
    """'Tutor/Interview' → id листовой папки в Drive; недостающие создаёт.

    Скоуп drive.file видит только созданное этим приложением — папку один раз
    создаём сами, дальше находим.
    """
    drive = build_service("drive", "v3")
    parent = "root"
    for name in path.strip("/").split("/"):
        q = (
            f"name = '{name}' and mimeType = 'application/vnd.google-apps.folder' "
            f"and '{parent}' in parents and trashed = false"
        )
        found = gexec(drive.files().list(q=q, fields="files(id)")).get("files", [])
        if found:
            parent = found[0]["id"]
        else:
            body = {"name": name, "mimeType": "application/vnd.google-apps.folder",
                    "parents": [parent]}
            parent = gexec(drive.files().create(body=body, fields="id"))["id"]
    return parent


async def call_tool(fn: Callable, /, **kwargs) -> dict[str, Any]:
    """Обёртка sync-функции в MCP-ответ: тред-пул + ошибки строкой ERROR."""
    try:
        result = await asyncio.to_thread(fn, **kwargs)
    except Exception as e:
        return {"content": [{"type": "text", "text": f"ERROR: {e}"}], "is_error": True}
    return {"content": [{"type": "text", "text": str(result)}]}
