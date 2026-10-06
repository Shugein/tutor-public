"""Буфер недавнего диалога (jsonl): короткая память между stateless-сессиями.

Долгая память — файлы в tutors/<тьютор>/; здесь только последние реплики,
которые в промпт подмешивают bot.py (весь хвост) и harness.py (окно за сутки).

Буфер — на тьютора: у каждого свой бот и свой разговор, и разговор про юниты
Solutions не должен всплывать в утреннем уроке по ML.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[2]
LOGS_DIR = PROJECT_ROOT / "logs"

BUFFER_SIZE = 12
RECENT_CHAR_LIMIT = 12_000
RECENT_MESSAGE_LIMIT = 4_000

WINDOW_HOURS = 48  # петле нужен ответ «со вчера», а не вся переписка
TUTOR_LIMIT = 400  # свои простыни режем: их содержание уже разложено по файлам памяти


def history_file(tutor: str) -> Path:
    return LOGS_DIR / f"chat_history-{tutor}.jsonl"


def polls_file(tutor: str) -> Path:
    return LOGS_DIR / f"polls-{tutor}.jsonl"


def append(tutor: str, role: str, text: str) -> None:
    path = history_file(tutor)
    path.parent.mkdir(exist_ok=True)
    line = {"ts": datetime.now(timezone.utc).isoformat(), "role": role, "text": text}
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")


def register_poll(tutor: str, poll_id: str, question: str,
                  options: list[str], correct_idx: int) -> None:
    """Квиз-опросы TG: ответ приходит отдельным update'ом с одним poll_id —
    без реестра его не связать с вопросом."""
    path = polls_file(tutor)
    path.parent.mkdir(exist_ok=True)
    line = {"poll_id": poll_id, "question": question,
            "options": options, "correct_idx": correct_idx}
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")


def find_poll(tutor: str, poll_id: str) -> dict | None:
    path = polls_file(tutor)
    if not path.exists():
        return None
    for raw in path.read_text(encoding="utf-8").splitlines():
        entry = json.loads(raw)
        if entry["poll_id"] == poll_id:
            return entry
    return None


def window(tutor: str, tz: str, hours: int = WINDOW_HOURS) -> str:
    """Диалог за последние `hours` часов — окно для cron-петли.

    Полный recent() сюда не годится: 50 реплик тьютора — это десятки тысяч
    токенов в каждой петле, а её вопрос узкий: «отвечал ли ученик со вчера
    и что именно». Его реплики даём целиком (в них сигнал), свои — обрезаем.

    Пустое окно называем явно. До 2026-08-08 петля буфера не получала вовсе,
    но base_system.md обещает агенту «буфер диалога» — и петля рапортовала
    «TG-буфер пуст» о проверке, которую не могла сделать: 05–07.08 три петли
    подряд решили молчать, не увидев ответа ученика от 07.08 23:47.
    """
    path = history_file(tutor)
    if not path.exists():
        return "(сообщений нет)"
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    lines = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(raw)
            ts = datetime.fromisoformat(entry["ts"])
            role, text = entry["role"], entry["text"]
        except (json.JSONDecodeError, KeyError, ValueError):
            continue
        if ts.tzinfo is None:  # записи до появления tz-aware меток
            ts = ts.replace(tzinfo=timezone.utc)
        if ts < cutoff:
            continue
        if role != "user" and len(text) > TUTOR_LIMIT:
            text = f"{text[:TUTOR_LIMIT]}… [обрезано, всего {len(text)} символов]"
        lines.append(f"[{ts.astimezone(ZoneInfo(tz)):%m-%d %H:%M}] {role}: {text}")
    return "\n".join(lines) or f"(за последние {hours} ч сообщений не было)"


def _clip_recent_message(text: str, limit: int = RECENT_MESSAGE_LIMIT) -> str:
    if len(text) <= limit:
        return text
    marker = f"\n… [обрезано, всего {len(text)} символов] …\n"
    room = max(0, limit - len(marker))
    head = room * 2 // 3
    tail = room - head
    return text[:head] + marker + (text[-tail:] if tail else "")


def recent(
    tutor: str,
    n: int = BUFFER_SIZE,
    char_limit: int = RECENT_CHAR_LIMIT,
) -> str:
    """Bounded recent dialogue for a stateless chat session.

    The complete JSONL remains canonical. The prompt gets the newest exchanges,
    bounded both per message and in total, so one long tutor answer cannot evict
    the current conversational thread or be replayed on every model step.
    """
    path = history_file(tutor)
    if not path.exists():
        return ""
    lines = path.read_text(encoding="utf-8").splitlines()[-n:]
    newest_first: list[str] = []
    remaining = max(0, char_limit)
    for raw in reversed(lines):
        try:
            entry = json.loads(raw)
            part = f"{entry['role']}: {_clip_recent_message(str(entry['text']))}"
        except (json.JSONDecodeError, KeyError):
            continue
        separator = 1 if newest_first else 0
        if len(part) + separator > remaining:
            if not newest_first and remaining > len(f"{entry['role']}: ") + 100:
                prefix = f"{entry['role']}: "
                part = prefix + _clip_recent_message(
                    str(entry["text"]), remaining - len(prefix)
                )
            else:
                break
        newest_first.append(part)
        remaining -= len(part) + separator
        if remaining <= 0:
            break
    return "\n".join(reversed(newest_first))
