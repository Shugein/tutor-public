"""Google Sheets: журнал оценок — одна строка на проверенную работу."""

from datetime import datetime
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from tutor.config import Settings
from tutor.mcp_tools import tool
from tutor.tools.google_auth import build_service, call_tool, ensure_folder, gexec

GRADEBOOK_NAME = "Gradebook"
HEADER = ["date", "topic", "score", "note"]


def _ensure_gradebook(folder: str) -> str:
    drive = build_service("drive", "v3")
    folder_id = ensure_folder(folder)
    q = (f"name = '{GRADEBOOK_NAME}' and '{folder_id}' in parents "
         "and mimeType = 'application/vnd.google-apps.spreadsheet' and trashed = false")
    found = gexec(drive.files().list(q=q, fields="files(id)")).get("files", [])
    if found:
        return found[0]["id"]

    body = {"name": GRADEBOOK_NAME, "parents": [folder_id],
            "mimeType": "application/vnd.google-apps.spreadsheet"}
    ss_id = gexec(drive.files().create(body=body, fields="id"))["id"]
    sheets = build_service("sheets", "v4")
    gexec(sheets.spreadsheets().values().append(
        spreadsheetId=ss_id, range="A1", valueInputOption="RAW",
        body={"values": [HEADER]}))
    return ss_id


def update_gradebook(topic: str, score: str, note: str, folder: str, tz: str) -> str:
    ss_id = _ensure_gradebook(folder)
    sheets = build_service("sheets", "v4")
    row = [datetime.now(ZoneInfo(tz)).strftime("%Y-%m-%d %H:%M"), topic, score, note]
    gexec(sheets.spreadsheets().values().append(
        spreadsheetId=ss_id, range="A:D", valueInputOption="USER_ENTERED",
        body={"values": [row]}))
    return "ok"


def make_tools(settings: Settings) -> list:
    @tool("update_gradebook",
          "Добавить строку в Sheets-журнал оценок (дата подставится сама)",
          {"topic": Annotated[str, "тема/работа"],
           "score": Annotated[str, "оценка, например '8/10'"],
           "note": Annotated[str, "короткий комментарий: что западает"]})
    async def _update(args: dict[str, Any]) -> dict[str, Any]:
        return await call_tool(update_gradebook, topic=args["topic"],
                               score=args["score"], note=args["note"],
                               folder=settings.drive_folder, tz=settings.timezone)

    return [_update]
