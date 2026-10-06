"""Жив ли тьютор? Проверяет две вещи и говорит в TG сырым HTTP (без зависимостей от бота):

1. Поллят ли боты Telegram (heartbeat-файл на бота, ботов столько же, сколько тьюторов).
2. Отработали ли петли свои слоты из deploy/crontab (лог сессии за сегодня) — по всем тьюторам.

Вторая проверка появилась после 2026-07-26: контейнер scheduler был остановлен
в 18:59 и поднят в 19:25 — ровно поверх слота review. Supercronic пропущенные
джобы не догоняет, и пропажа недельного review осталась незамеченной две недели.

Не рестартит ничего — рестарты делает compose (restart: always). Запускается
supercronic'ом в scheduler-контейнере каждые 10 минут; если умер и scheduler —
сказать некому, это осознанный предел (совсем мёртвую систему заметит человек
по тишине утром).
"""

import json
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from tutor.bot import heartbeat_file
from tutor.config import PROJECT_ROOT, Settings, apply_timezone_override, load_settings

STATE_FILE = PROJECT_ROOT / "logs" / "healthcheck.state"  # json: ключ алерта → когда
LOGS_DIR = PROJECT_ROOT / "logs"
STALE_AFTER = 15 * 60  # heartbeat старше 15 минут — поллинг мёртв (обновляется каждые ~10 с)
ALERT_COOLDOWN = 2 * 3600  # не спамить: алерт про бота не чаще раза в 2 часа

LOOP_GRACE = 90 * 60  # петля успевает отработать за полтора часа с начала слота


@dataclass(frozen=True)
class Slot:
    """Слот петли конкретного тьютора: когда должна была запуститься (время
    крона) и в чьей таймзоне назван её лог (таймзона тьютора)."""

    tutor: str
    loop: str
    hour: int
    minute: int
    weekday: int | None  # None = ежедневно, 6 = воскресенье
    tz: str

    @property
    def key(self) -> str:
        return f"{self.tutor}/{self.loop}"

    def occurrence(self, now: datetime) -> datetime:
        """Момент сегодняшнего слота в таймзоне крона."""
        return now.replace(hour=self.hour, minute=self.minute, second=0, microsecond=0)


def slots_of(settings: Settings) -> list[Slot]:
    """Слоты всех тьюторов из конфига — зеркало deploy/crontab.
    deadlines с 2026-08-15 ходит дважды в день, и в слотах стоит поздний из
    двух: пропуск одного прогона по-прежнему ничего не решает, а мёртвый крон —
    решает."""
    slots = []
    for name, cfg in settings.tutors.items():
        # таймзона тьютора живёт файлом в его памяти, у каждого своя
        tz = apply_timezone_override(
            settings.model_copy(update={"tutor_dir": cfg.tutor_dir})).timezone
        slots += [Slot(name, s.loop, s.hour, s.minute, s.weekday, tz) for s in cfg.slots]
    return slots


def send_tg(token: str, chat_id: int, text: str) -> None:
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=json.dumps({"chat_id": chat_id, "text": text}).encode(),
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(req, timeout=30)


def _load_state() -> dict:
    """Старый формат — голый ts алерта про бота; читаем его как {"bot": ts}."""
    if not STATE_FILE.exists():
        return {}
    raw = STATE_FILE.read_text().strip()
    if not raw:
        return {}
    try:
        state = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if isinstance(state, dict):
        return state
    # старый формат — голое число: json.loads разбирает его как float, не как ошибку
    return {"bot": float(state)} if isinstance(state, (int, float)) else {}


def _save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state))


def bot_alert(state: dict, bots: list[str]) -> str | None:
    """Текст алерта про мёртвый поллинг — или None, если боты живы/недавно алертили.

    Ботов столько же, сколько тьюторов, плюс карточный: у каждого свой
    heartbeat-файл. Молча умерший карточный бот — это молча прекратившееся
    повторение, его пропажу тоже надо замечать.
    """
    dead = []
    for tutor in bots:
        beat = heartbeat_file(tutor)
        if beat.exists():
            age = time.time() - beat.stat().st_mtime
            if age < STALE_AFTER:
                continue
            dead.append(f"{tutor}: heartbeat не обновлялся {int(age / 60)} мин")
        else:
            dead.append(f"{tutor}: heartbeat-файла нет — бот ни разу не запускался?")
    if not dead:
        return None

    if time.time() - float(state.get("bot", 0.0)) < ALERT_COOLDOWN:
        return None
    state["bot"] = time.time()
    return (f"⚠️ Бот не поллит Telegram — {'; '.join(dead)}. Бот сам рестартует при "
            "зависании; раз алерт дошёл — рестарты не помогают (прокси? сеть?), "
            "нужен человек: смотри docker logs deploy-bot-<тьютор>-1.")


def _log_exists(logs_dir: Path, slot: Slot, log_date: str) -> bool:
    names = [f"{log_date}-{slot.tutor}-{slot.loop}.jsonl"]
    if slot.tutor == "interview":
        # логи до мультитенантности (до 2026-08-09) звались без имени тьютора;
        # строку можно убрать, когда таких дат не останется в проверяемом окне
        names.append(f"{log_date}-{slot.loop}.jsonl")
    return any((logs_dir / name).exists() for name in names)


def missed_loops(now: datetime, slots: list[Slot],
                 logs_dir: Path | None = None) -> dict[str, str]:
    """{тьютор/петля: дата её лога} для слотов, прошедших больше LOOP_GRACE назад
    без лога.

    `now` — время крона (локальное в контейнере), таймзона в слоте — таймзона
    тьютора: ею харнес датирует файл лога, и это может быть другой календарный день.

    Дата считается от МОМЕНТА СЛОТА, а не от «сейчас». Разница не косметическая:
    в окне 23:00–23:59 по Белграду в Москве уже завтра, лога за завтрашнюю дату
    существовать не может — и сторож будил ученика ложным «петля не отработала»
    каждую ночь с 2026-08-03, когда сам и появился.
    """
    logs_dir = logs_dir or LOGS_DIR  # не дефолтом в сигнатуре: он застывает на импорте
    missed = {}
    for slot in slots:
        if slot.weekday is not None and now.weekday() != slot.weekday:
            continue
        when = slot.occurrence(now)
        if (now - when).total_seconds() < LOOP_GRACE:
            continue  # слот ещё не наступил или петля имеет право доработать
        log_date = f"{when.astimezone(ZoneInfo(slot.tz)):%Y-%m-%d}"
        if not _log_exists(logs_dir, slot, log_date):
            missed[slot.key] = log_date
    return missed


def known_since(state: dict, slots: list[Slot], now: datetime) -> dict[str, float]:
    """{слот: когда сторож впервые его увидел}; новые запоминает молча.

    Слот, включённый днём, не «пропустил» своё утро — его в то утро не было.
    2026-08-10: слоты english включили в 14:56, и сторож тут же отчитался
    о пропуске слота 07:30. Отметка живёт в state, а он переживает
    пересоздание контейнера — поэтому случай «scheduler лежал поверх слота»
    (26.07, ради которого сторож и написан) по-прежнему алертит.
    """
    for slot in slots:
        state.setdefault(f"seen:{slot.key}", now.timestamp())
    return {slot.key: state[f"seen:{slot.key}"] for slot in slots}


def loops_alert(state: dict, now: datetime, slots: list[Slot]) -> str | None:
    """Алерт про пропущенные петли; не чаще одного раза в день на петлю."""
    seen = known_since(state, slots, now)
    by_key = {slot.key: slot for slot in slots}
    fresh = {key: log_date for key, log_date in missed_loops(now, slots).items()
             if by_key[key].occurrence(now).timestamp() >= seen[key]
             and state.get(f"loop:{key}") != log_date}
    if not fresh:
        return None
    for key, log_date in fresh.items():
        state[f"loop:{key}"] = log_date
    listed = ", ".join(f"{s.key} ({s.hour}:{s.minute:02d})" for s in slots if s.key in fresh)
    return (f"⚠️ Петля не отработала свой слот сегодня: {listed}. Лога сессии нет — "
            "крон не сработал (контейнер лежал в этот час?) или процесс упал до старта. "
            "Supercronic пропущенное не догоняет: смотри docker logs deploy-scheduler-1, "
            "запусти вручную — docker compose -f deploy/compose.yaml exec scheduler "
            "python -m tutor.loop --tutor <тьютор> --loop <имя>.")


def main() -> None:
    # настройки interview: его ботом и шлём алерты про обоих — сторожу нужен
    # один рабочий канал, а не по каналу на тьютора
    settings: Settings = load_settings()
    state = _load_state()
    before = dict(state)
    now = datetime.now().astimezone()  # время крона: TZ контейнера, как в crontab
    slots = slots_of(settings)

    alerts = [text for text in (bot_alert(state, [*settings.tutors, "cards"]),
                                loops_alert(state, now, slots)) if text]
    for text in alerts:
        send_tg(settings.tg_token.get_secret_value(), settings.tg_chat_id, text)
    # сохраняем и без алерта: отметки «слот впервые увиден» нужны молчаливому прогону,
    # иначе новый слот так и не запомнится и завтра отчитается о сегодняшнем пропуске
    if state != before:
        _save_state(state)


if __name__ == "__main__":
    main()
