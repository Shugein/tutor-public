"""Google Docs через Drive-конвертацию markdown ↔ Docs.

Вместо ручной сборки batchUpdate-реквестов используем нативную конвертацию
Drive API (text/markdown → Docs и обратно): меньше кода, агент и так думает
в markdown. Цена: append пересобирает документ целиком (комментарии и ручное
форматирование в Docs не переживут) — для конспектов тьютора это приемлемо.
"""

from typing import Annotated, Any

from googleapiclient.http import MediaInMemoryUpload

from tutor.config import Settings
from tutor.mcp_tools import tool
from tutor.tools.google_auth import build_service, call_tool, ensure_folder, gexec

GDOC_MIME = "application/vnd.google-apps.document"


def create_gdoc(title: str, markdown: str, folder: str) -> dict[str, str]:
    drive = build_service("drive", "v3")
    media = MediaInMemoryUpload(markdown.encode("utf-8"), mimetype="text/markdown")
    body = {"name": title, "mimeType": GDOC_MIME, "parents": [ensure_folder(folder)]}
    f = gexec(drive.files().create(body=body, media_body=media,
                                   fields="id, webViewLink"))
    return {"doc_id": f["id"], "url": f["webViewLink"]}


def read_gdoc(doc_id: str) -> str:
    drive = build_service("drive", "v3")
    return gexec(drive.files().export(fileId=doc_id, mimeType="text/markdown")).decode("utf-8")


def append_gdoc(doc_id: str, markdown: str) -> str:
    current = read_gdoc(doc_id)
    return update_gdoc(doc_id, current.rstrip() + "\n\n" + markdown)


def update_gdoc(doc_id: str, markdown: str) -> str:
    """Перезаписать документ целиком — для живых доков вроде плана подготовки,
    чтобы пересборка не плодила новые документы."""
    drive = build_service("drive", "v3")
    media = MediaInMemoryUpload(markdown.encode("utf-8"), mimetype="text/markdown")
    gexec(drive.files().update(fileId=doc_id, media_body=media))
    return "ok"


def make_tools(settings: Settings) -> list:
    @tool("create_gdoc",
          "Создать Google Doc из markdown в папке тьютора; вернёт ссылку",
          {"title": str, "markdown": Annotated[str, "содержимое в markdown"]})
    async def _create(args: dict[str, Any]) -> dict[str, Any]:
        return await call_tool(create_gdoc, title=args["title"],
                               markdown=args["markdown"], folder=settings.drive_folder)

    @tool("read_gdoc", "Прочитать Google Doc как markdown",
          {"doc_id": Annotated[str, "id документа"]})
    async def _read(args: dict[str, Any]) -> dict[str, Any]:
        return await call_tool(read_gdoc, doc_id=args["doc_id"])

    @tool("append_gdoc", "Дописать markdown в конец Google Doc",
          {"doc_id": str, "markdown": str})
    async def _append(args: dict[str, Any]) -> dict[str, Any]:
        return await call_tool(append_gdoc, doc_id=args["doc_id"],
                               markdown=args["markdown"])

    @tool("update_gdoc",
          "Перезаписать Google Doc новым markdown (для живых документов вроде плана)",
          {"doc_id": str, "markdown": str})
    async def _update(args: dict[str, Any]) -> dict[str, Any]:
        return await call_tool(update_gdoc, doc_id=args["doc_id"],
                               markdown=args["markdown"])

    return [_create, _read, _append, _update]
