"""Свой SRS: карточки в SQLite, расписание — FSRS-6 (пакет `fsrs`).

Почему свой, а не Anki: ревью должно работать с телефона, ноута и iPad без
покупок и синхронизации, а карточки — создаваться агентом в момент ошибки.
Владеет базой код, а не агент: десятки тапов в день не могут быть SDK-сессиями.

База одна на проект (`logs/srs.sqlite`), карточки помечены тьютором — ревью
идёт общим потоком в отдельном боте, но статистика считается по каждому.

Знакомство отделено от повторения (модель WaniKani, решение 2026-08-14). Новая
карточка не спрашивается: сперва она показывается уроком (лицо и оборот сразу),
и только `introduce` пускает её в расписание. Первый прогон сломался ровно на
этом — пятнадцать незнакомых фразовых глаголов пришли вечером как cloze с тремя
пропусками, то есть первой встречей со словом оказался экзамен по нему.
"""

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fsrs import Card, Rating, Scheduler, State

from tutor.config import PROJECT_ROOT

DB_PATH = PROJECT_ROOT / "logs" / "srs.sqlite"

# Типы карточек. Разные вопросы к слову дают разную нагрузку, и порядок между
# ними не произволен: сначала узнавание, продукция — потом.
KINDS = (
    "recognition",  # лицо — слово или фраза; «что это значит?». Дефолт для новых слов
    "cloze",        # лицо — предложение с пропуском; форма, предлог, коллокация, ошибка
    "production",   # лицо — значение и ситуация; «как это сказать по-английски?»
)

# Через сколько после урока карточка впервые спрашивается. Не сразу: иначе она
# всплывёт следующей же в той же сессии и проверит память о предыдущем экране.
FIRST_REVIEW_AFTER = timedelta(minutes=10)

SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
    id         INTEGER PRIMARY KEY,
    tutor      TEXT NOT NULL,
    kind       TEXT NOT NULL DEFAULT 'cloze',
    front      TEXT NOT NULL,
    back       TEXT NOT NULL,
    example    TEXT NOT NULL DEFAULT '',
    hint       TEXT NOT NULL DEFAULT '',
    context    TEXT NOT NULL DEFAULT '',
    tags       TEXT NOT NULL DEFAULT '',
    priority   INTEGER NOT NULL DEFAULT 0,
    created    TEXT NOT NULL,
    introduced TEXT,
    state      INTEGER NOT NULL,
    step       INTEGER,
    stability  REAL,
    difficulty REAL,
    due        TEXT NOT NULL,
    last_review TEXT,
    suspended  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS cards_due ON cards (suspended, due);
CREATE UNIQUE INDEX IF NOT EXISTS cards_front ON cards (tutor, front);

CREATE TABLE IF NOT EXISTS review_log (
    id       INTEGER PRIMARY KEY,
    card_id  INTEGER NOT NULL REFERENCES cards (id),
    rating   INTEGER NOT NULL,
    reviewed TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS review_log_card ON review_log (card_id);
"""

# Колонки, добавленные к уже живущей базе. `CREATE TABLE IF NOT EXISTS` их не
# доедет — таблица уже есть, — а alembic ради одной таблицы это оверкилл.
MIGRATIONS = {
    "kind": "ALTER TABLE cards ADD COLUMN kind TEXT NOT NULL DEFAULT 'cloze'",
    "example": "ALTER TABLE cards ADD COLUMN example TEXT NOT NULL DEFAULT ''",
    "hint": "ALTER TABLE cards ADD COLUMN hint TEXT NOT NULL DEFAULT ''",
    "introduced": "ALTER TABLE cards ADD COLUMN introduced TEXT",
    "priority": "ALTER TABLE cards ADD COLUMN priority INTEGER NOT NULL DEFAULT 0",
}

RATINGS = {"again": Rating.Again, "hard": Rating.Hard,
           "good": Rating.Good, "easy": Rating.Easy}


@dataclass
class Row:
    """Карточка как её видит бот: содержимое + когда следующий показ."""

    id: int
    tutor: str
    kind: str
    front: str
    back: str
    example: str
    hint: str
    context: str
    tags: str
    priority: int
    due: datetime
    introduced: datetime | None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)  # autocommit
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Догнать схему до текущей. Дешевле проверки версии и идемпотентно."""
    have = {r["name"] for r in conn.execute("PRAGMA table_info(cards)")}
    missing = [name for name in MIGRATIONS if name not in have]
    for name in missing:
        conn.execute(MIGRATIONS[name])
    if "introduced" in missing:
        # карточки, которые он уже видел в боте, знакомить заново незачем:
        # состоявшееся повторение и есть предъявление
        conn.execute("UPDATE cards SET introduced = last_review "
                     "WHERE last_review IS NOT NULL")


@contextmanager
def session(path: Path | None = None):
    """Соединение с гарантированным закрытием.

    Именно так, а не `with connect() as conn`: контекст sqlite3.Connection
    управляет транзакцией и НЕ закрывает соединение — вызовы из бота копили бы
    открытые дескрипторы.
    """
    conn = connect(path)
    try:
        yield conn
    finally:
        conn.close()


def _to_card(row: sqlite3.Row) -> Card:
    return Card(
        card_id=row["id"],
        state=State(row["state"]),
        step=row["step"],
        stability=row["stability"],
        difficulty=row["difficulty"],
        due=datetime.fromisoformat(row["due"]),
        last_review=datetime.fromisoformat(row["last_review"]) if row["last_review"] else None,
    )


def _row(row: sqlite3.Row) -> Row:
    return Row(id=row["id"], tutor=row["tutor"], kind=row["kind"],
               front=row["front"], back=row["back"], example=row["example"],
               hint=row["hint"], context=row["context"], tags=row["tags"],
               priority=row["priority"], due=datetime.fromisoformat(row["due"]),
               introduced=datetime.fromisoformat(row["introduced"])
               if row["introduced"] else None)


def get_card(conn: sqlite3.Connection, card_id: int) -> Row | None:
    row = conn.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
    return _row(row) if row else None


def add_card(conn: sqlite3.Connection, tutor: str, front: str, back: str,
             kind: str = "recognition", example: str = "", hint: str = "",
             context: str = "", tags: str = "", priority: int = 0,
             introduced: bool = False) -> tuple[int, bool]:
    """Завести карточку. Возвращает (id, создана ли новая).

    Карточка рождается непредъявленной: в очередь повторения её пускает
    `introduce`, а до того она ждёт урока. Исключение — `introduced=True`: так
    заводятся карточки на собственные ошибки, где предъявление уже состоялось
    (он сам это написал, и вечерний разбор объяснил, что было не так).

    Повтор одного и того же front не плодит дубли: агент видит слово в ошибках
    несколько дней подряд, и каждая такая встреча не должна ломать расписание.
    """
    front, back = front.strip(), back.strip()
    if not front or not back:
        raise ValueError("у карточки должны быть обе стороны")
    if kind not in KINDS:
        raise ValueError(f"тип карточки должен быть одним из {', '.join(KINDS)}")
    existing = conn.execute(
        "SELECT id FROM cards WHERE tutor = ? AND front = ?", (tutor, front)).fetchone()
    if existing:
        return existing["id"], False

    card = Card()  # новая карточка FSRS: due = сейчас, состояние Learning
    now = _now()
    cur = conn.execute(
        "INSERT INTO cards (tutor, kind, front, back, example, hint, context, tags, "
        "priority, created, introduced, state, step, stability, difficulty, due, "
        "last_review) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
        (tutor, kind, front, back, example.strip(), hint.strip(), context.strip(),
         tags.strip(), priority, now.isoformat(),
         now.isoformat() if introduced else None,
         card.state.value, card.step, card.stability, card.difficulty,
         card.due.isoformat()),
    )
    return cur.lastrowid, True


def due_cards(conn: sqlite3.Connection, tutor: str | None = None,
              limit: int = 50, now: datetime | None = None) -> list[Row]:
    """Карточки, которым пора — по возрастанию срока (самые просроченные первыми).

    Только предъявленные: непредъявленную спрашивать не о чем, она ещё не была
    уроком (`new_cards`).
    """
    sql = ("SELECT * FROM cards WHERE suspended = 0 AND introduced IS NOT NULL "
           "AND due <= ?"
           + (" AND tutor = ?" if tutor else "")
           + " ORDER BY due LIMIT ?")
    params = [(now or _now()).isoformat()] + ([tutor] if tutor else []) + [limit]
    return [_row(r) for r in conn.execute(sql, params)]


def new_cards(conn: sqlite3.Connection, tutor: str | None = None,
              per_day: int = 5, now: datetime | None = None) -> list[Row]:
    """Карточки на знакомство сегодня — не больше дневной нормы.

    Норма здесь, а не в промпте: агент уже заводил пятнадцать новых слов за
    вечер, и промпт, который он может нарушить, — не ограничение. Сколько бы он
    ни завёл, в день предъявляется `per_day`, остальное ждёт своей очереди.

    Приоритет вперёд возраста: слово из целевого словаря — того, что нужно на
    собеседовании и в статьях, — не должно неделю стоять в хвосте за лексикой
    первого юнита.
    """
    left = max(0, per_day - introduced_today(conn, tutor, now=now))
    if not left:
        return []
    sql = ("SELECT * FROM cards WHERE suspended = 0 AND introduced IS NULL"
           + (" AND tutor = ?" if tutor else "")
           + " ORDER BY priority DESC, created, id LIMIT ?")
    params = ([tutor] if tutor else []) + [left]
    return [_row(r) for r in conn.execute(sql, params)]


def introduced_today(conn: sqlite3.Connection, tutor: str | None = None,
                     now: datetime | None = None) -> int:
    since = (now or _now()).date().isoformat()
    sql = ("SELECT count(*) FROM cards WHERE introduced >= ?"
           + (" AND tutor = ?" if tutor else ""))
    params = [since] + ([tutor] if tutor else [])
    return conn.execute(sql, params).fetchone()[0]


def introduce(conn: sqlite3.Connection, card_id: int,
              now: datetime | None = None) -> None:
    """Отметить карточку показанной уроком и пустить в расписание."""
    when = now or _now()
    conn.execute("UPDATE cards SET introduced = ?, due = ? WHERE id = ?",
                 (when.isoformat(), (when + FIRST_REVIEW_AFTER).isoformat(), card_id))


def review(conn: sqlite3.Connection, card_id: int, rating: str,
           now: datetime | None = None, scheduler: Scheduler | None = None) -> datetime:
    """Записать оценку и пересчитать срок по FSRS. Возвращает новый срок."""
    if rating not in RATINGS:
        raise ValueError(f"оценка должна быть одной из {', '.join(RATINGS)}")
    row = conn.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
    if row is None:
        raise ValueError(f"карточки {card_id} нет")

    when = now or _now()
    updated, _ = (scheduler or Scheduler()).review_card(_to_card(row), RATINGS[rating], when)
    conn.execute(
        "UPDATE cards SET state = ?, step = ?, stability = ?, difficulty = ?, "
        "due = ?, last_review = ? WHERE id = ?",
        (updated.state.value, updated.step, updated.stability, updated.difficulty,
         updated.due.isoformat(), when.isoformat(), card_id),
    )
    conn.execute("INSERT INTO review_log (card_id, rating, reviewed) VALUES (?, ?, ?)",
                 (card_id, RATINGS[rating].value, when.isoformat()))
    return updated.due


def suspend(conn: sqlite3.Connection, card_id: int, value: bool = True) -> None:
    """Убрать карточку из ротации, не удаляя историю («слово выучено», «мусорная»)."""
    conn.execute("UPDATE cards SET suspended = ? WHERE id = ?", (int(value), card_id))


def stats(conn: sqlite3.Connection, tutor: str | None = None,
          now: datetime | None = None) -> dict[str, int]:
    """Сводка для вечерней петли и недельного отчёта.

    `waiting` — очередь на знакомство: слова, которые агент уже завёл, но
    которых ученик ещё не видел. Если она растёт, тьютор сыплет словами быстрее,
    чем их можно выучить, и вечерняя петля обязана это заметить.
    """
    where = " WHERE tutor = ?" if tutor else ""
    params: tuple = (tutor,) if tutor else ()
    total = conn.execute(f"SELECT count(*) FROM cards{where}", params).fetchone()[0]
    waiting = conn.execute(
        "SELECT count(*) FROM cards WHERE introduced IS NULL AND suspended = 0"
        f"{' AND tutor = ?' if tutor else ''}", params).fetchone()[0]
    due = len(due_cards(conn, tutor, limit=10**6, now=now))
    since = (now or _now()).date().isoformat()
    reviewed = conn.execute(
        "SELECT count(*) FROM review_log r JOIN cards c ON c.id = r.card_id "
        f"WHERE r.reviewed >= ?{' AND c.tutor = ?' if tutor else ''}",
        (since, *params)).fetchone()[0]
    return {"total": total, "due": due, "waiting": waiting,
            "learned_today": introduced_today(conn, tutor, now=now),
            "reviewed_today": reviewed}
