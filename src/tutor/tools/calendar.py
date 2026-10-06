"""Google Calendar: события-напоминания (дедлайны, сессии занятий)."""

from datetime import datetime, timedelta
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from tutor.config import Settings
from tutor.mcp_tools import tool
from tutor.tools.google_auth import build_service, call_tool, gexec


def _span(start_iso: str, tz: str) -> tuple[dict[str, str], dict[str, str]]:
    """(start, end) в формате Calendar API: дата → весь день, дата-время → час."""
    if len(start_iso) == 10:  # только дата → событие на весь день
        return {"date": start_iso}, {"date": start_iso}
    dt = datetime.fromisoformat(start_iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(tz))
    return {"dateTime": dt.isoformat()}, {"dateTime": (dt + timedelta(hours=1)).isoformat()}


def create_calendar_event(title: str, start_iso: str, description: str, tz: str) -> dict[str, str]:
    start, end = _span(start_iso, tz)
    cal = build_service("calendar", "v3")
    event = gexec(cal.events().insert(calendarId="primary", body={
        "summary": title, "description": description, "start": start, "end": end,
    }))
    return {"event_id": event["id"], "url": event.get("htmlLink", "")}


def list_calendar_events(from_iso: str, to_iso: str) -> list[dict[str, str]]:
    """События в окне дат — чтобы узнать event_id уже созданного.

    Без этого update/delete бесполезны: тьютор пишет id в свою память не всегда,
    а событий за месяцы набралось больше, чем он помнит (перекладка плана 08-08).
    """
    cal = build_service("calendar", "v3")
    page, events = None, []
    while True:
        resp = gexec(cal.events().list(
            calendarId="primary", singleEvents=True, orderBy="startTime",
            timeMin=f"{from_iso}T00:00:00Z", timeMax=f"{to_iso}T23:59:59Z",
            maxResults=250, pageToken=page))
        for e in resp.get("items", []):
            start = e.get("start", {})
            events.append({"event_id": e["id"], "title": e.get("summary", ""),
                           "start": start.get("date") or start.get("dateTime", "")})
        page = resp.get("nextPageToken")
        if not page:
            break
    return events


def update_calendar_event(event_id: str, tz: str, title: str = "",
                          start_iso: str = "", description: str = "") -> dict[str, str]:
    """Патч события: пустое поле — не трогать (сдвиг даты не должен стирать текст)."""
    body: dict[str, Any] = {}
    if title:
        body["summary"] = title
    if description:
        body["description"] = description
    if start_iso:
        body["start"], body["end"] = _span(start_iso, tz)
    if not body:
        return {"event_id": event_id, "changed": "ничего не передано — событие не тронуто"}
    cal = build_service("calendar", "v3")
    event = gexec(cal.events().patch(calendarId="primary", eventId=event_id, body=body))
    return {"event_id": event["id"], "changed": ", ".join(body), "url": event.get("htmlLink", "")}


def delete_calendar_event(event_id: str) -> str:
    cal = build_service("calendar", "v3")
    gexec(cal.events().delete(calendarId="primary", eventId=event_id))
    return f"событие {event_id} удалено"


def make_tools(settings: Settings) -> list:
    @tool("create_calendar_event",
          "Создать событие в Google Calendar (напоминание о дедлайне/занятии)",
          {"title": str,
           "start_iso": Annotated[str, "ISO: '2026-07-10' (весь день) или '2026-07-10T18:00' (час)"],
           "description": str})
    async def _create(args: dict[str, Any]) -> dict[str, Any]:
        return await call_tool(create_calendar_event, title=args["title"],
                               start_iso=args["start_iso"],
                               description=args["description"], tz=settings.timezone)

    @tool("list_calendar_events",
          "Список событий календаря за период — сюда за event_id перед update/delete",
          {"from_iso": Annotated[str, "дата начала окна, '2026-07-01'"],
           "to_iso": Annotated[str, "дата конца окна включительно, '2026-08-31'"]})
    async def _list(args: dict[str, Any]) -> dict[str, Any]:
        return await call_tool(list_calendar_events, from_iso=args["from_iso"],
                               to_iso=args["to_iso"])

    @tool("update_calendar_event",
          "Изменить событие: дату, название, описание. Пустое поле — оставить как есть",
          {"event_id": str,
           "start_iso": Annotated[str, "новая дата/время ISO, пусто — не двигать"],
           "title": Annotated[str, "новое название, пусто — не менять"],
           "description": Annotated[str, "новое описание, пусто — не менять"]})
    async def _update(args: dict[str, Any]) -> dict[str, Any]:
        return await call_tool(update_calendar_event, event_id=args["event_id"],
                               start_iso=args.get("start_iso", ""),
                               title=args.get("title", ""),
                               description=args.get("description", ""), tz=settings.timezone)

    @tool("delete_calendar_event",
          "Удалить событие календаря (протухший слот плана). Отменить нельзя",
          {"event_id": str})
    async def _delete(args: dict[str, Any]) -> dict[str, Any]:
        return await call_tool(delete_calendar_event, event_id=args["event_id"])

    return [_create, _list, _update, _delete]
