"""Забор решённого листа из авто-бэкапа GoodNotes в Google Drive.

GoodNotes умеет складывать бэкап только в папку в корне Drive и делает это
с задержкой в минуты — поэтому здесь ретрай, а сигналом служит сообщение
ученика («готово»), а не поллинг: опрашивать Drive круглосуточно ради двух
файлов в день незачем.

Скоуп: `drive.file` видит только то, что создало наше приложение, а бэкап
создаёт GoodNotes — нужен `drive.readonly` (переавторизация токена
scripts/google_auth.py).
"""

import time
from datetime import datetime, timezone
from pathlib import Path

from googleapiclient.http import MediaIoBaseDownload

from tutor.tools.google_auth import DRIVE_READONLY, build_service, gexec, has_scope

FOLDER_MIME = "application/vnd.google-apps.folder"
# бэкап отстаёт от «готово» на минуты: ждём суммарно ~3 минуты, потом сдаёмся
RETRY_DELAY = 20
RETRIES = 9


class BackupNotFound(RuntimeError):
    """Понятная человеку причина, а не трейс: сообщение уходит прямо в чат."""


def require_scope() -> None:
    """Токен без drive.readonly не видит чужие файлы — скажем об этом заранее
    и по-человечески, а не 403-й из недр googleapiclient."""
    if not has_scope(DRIVE_READONLY):
        raise BackupNotFound(
            "the token has no drive.readonly access. The GoodNotes backup was not "
            "created by this app, so without that scope it is invisible. Re-authorise "
            "with: uv run python scripts/google_auth.py (ssh tunnel 8765). "
            "Meanwhile you can just send the file here.")


def root_folders() -> list[str]:
    """Папки в корне Drive — чтобы в ошибке подсказать, где искать бэкап."""
    drive = build_service("drive", "v3")
    q = f"mimeType = '{FOLDER_MIME}' and 'root' in parents and trashed = false"
    found = gexec(drive.files().list(q=q, fields="files(name)", pageSize=50))
    return sorted(f["name"] for f in found.get("files", []))


def _folder_id(name: str) -> str:
    require_scope()
    drive = build_service("drive", "v3")
    q = (f"name = '{name}' and mimeType = '{FOLDER_MIME}' "
         "and 'root' in parents and trashed = false")
    found = gexec(drive.files().list(q=q, fields="files(id)")).get("files", [])
    if not found:
        raise BackupNotFound(
            f"there is no folder \u00ab{name}\u00bb in the root of Drive. What is there: "
            f"{', '.join(root_folders()) or 'nothing'}. Check the name of the GoodNotes "
            "auto-backup folder (config.py → TutorConfig.backup_folder)")
    return found[0]["id"]


def latest_pdf(folder: str, since: datetime | None = None) -> dict | None:
    """Самый свежий PDF в папке бэкапа; None, если свежее `since` ничего нет."""
    drive = build_service("drive", "v3")
    q = (f"'{_folder_id(folder)}' in parents and trashed = false "
         "and mimeType = 'application/pdf'")
    files = gexec(drive.files().list(
        q=q, orderBy="modifiedTime desc", pageSize=5,
        fields="files(id, name, modifiedTime, size)")).get("files", [])
    if not files:
        return None
    newest = files[0]
    if since is not None:
        modified = datetime.fromisoformat(newest["modifiedTime"].replace("Z", "+00:00"))
        if modified <= since:
            return None
    return newest


def fetch_latest_backup(folder: str, dest: Path, since: datetime | None = None,
                        retries: int = RETRIES, delay: int = RETRY_DELAY) -> Path:
    """Дождаться свежего бэкапа и скачать его в dest. Кидает BackupNotFound.

    Ретрай — не про сеть, а про человека: ученик пишет «готово» раньше, чем
    GoodNotes донесёт файл до Drive.
    """
    file = None
    for attempt in range(retries + 1):
        file = latest_pdf(folder, since)
        if file is not None:
            break
        if attempt < retries:
            time.sleep(delay)
    if file is None:
        last = latest_pdf(folder)  # что там вообще лежит — полезно сказать
        when = last["modifiedTime"][:16].replace("T", " ") if last else "—"
        raise BackupNotFound(
            f"no new PDF in \u00ab{folder}\u00bb (latest: {last['name'] if last else 'none'}, "
            f"{when}). The GoodNotes backup may not have synced yet: wait a minute and "
            "write again, or send the file straight into the chat.")

    drive = build_service("drive", "v3")
    dest.parent.mkdir(parents=True, exist_ok=True)
    request = drive.files().get_media(fileId=file["id"])
    with dest.open("wb") as f:
        downloader = MediaIoBaseDownload(f, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
    return dest


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
