"""Единственный конфиг проекта: секреты из .env, параметры — дефолтами в коде.

Никаких os.environ по коду — все ключи и параметры живут здесь.

Тьюторов несколько (interview, english). Различия между ними — в TutorConfig;
`load_settings("english")` возвращает Settings, у которого плоские поля уже
разрешены под этого тьютора. Поэтому всё остальное (харнесс, тулы, бот) про
мультитенантность не знает и работает с одним объектом настроек, как раньше.
"""

from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Имена MCP-серверов харнесса. Здесь, а не в harness.py: TutorConfig решает,
# какие из них тьютору вообще доступны, а config импортировать harness не может.
# Набор по умолчанию; `transcript` в него не входит — он нужен одной петле
# одного тьютора, и лишние схемы в префиксе каждого запроса не бесплатны.
MCP_SERVERS = ["telegram", "google", "srs"]


class LoopSlot(BaseModel):
    """Слот петли — зеркало deploy/crontab: по нему healthcheck понимает,
    что петля должна была отработать, и замечает пропущенный крон."""

    loop: str
    hour: int
    minute: int = 0
    weekday: int | None = None  # None — ежедневно, 6 — воскресенье


class TutorConfig(BaseModel):
    """Чем один тьютор отличается от другого. Всё прочее — общее."""

    tutor_dir: Path  # память тьютора (markdown), она же cwd его сессий
    drive_folder: str  # папка артефактов в Google Drive
    token_field: str  # поле Settings с токеном TG-бота: у каждого тьютора свой бот
    mcp_servers: list[str] = MCP_SERVERS  # какие серверы этому тьютору вообще доступны
    # Папка авто-бэкапа GoodNotes в корне Drive: по слову «готово» бот берёт
    # оттуда свежий PDF. None — тьютор так не работает, слово идёт в диалог.
    backup_folder: str | None = None
    slots: list[LoopSlot] = []


TUTORS: dict[str, TutorConfig] = {
    "interview": TutorConfig(
        tutor_dir=Path("tutors/interview"),
        drive_folder="Tutor/Interview",
        token_field="tg_token",  # историческое имя: первый бот проекта
        slots=[
            LoopSlot(loop="morning", hour=8),
            LoopSlot(loop="evening", hour=20),
            # deadlines ходит дважды в день (12:00 и 18:00); в слотах — только
            # поздний: ключ слота это тьютор/петля, две записи схлопнулись бы в
            # одну, а проверка «есть лог за сегодня» у них общая
            LoopSlot(loop="deadlines", hour=18),
            LoopSlot(loop="weekly", hour=18, weekday=6),
            LoopSlot(loop="review", hour=19, weekday=6),
        ],
    ),
    "english": TutorConfig(
        tutor_dir=Path("tutors/english"),
        drive_folder="Tutor/English",
        token_field="tg_token_english",
        # Без google: Forms и Calendar тьютору английского запрещены промптом,
        # а 11 лишних схем ехали в префиксе каждого запроса. Тул, которого нет,
        # надёжнее тула, которым велено не пользоваться.
        # transcript — источник текста для петли episode (fetch_page, fetch_subtitles)
        mcp_servers=["telegram", "srs", "transcript"],
        backup_folder="GoodNotes",  # имя папки в корне Drive; поправить, если другое
        # Времена разнесены с interview (08:00/20:00) намеренно: две тяжёлые
        # сессии в одну минуту — лишний риск упереться в окно подписки.
        slots=[
            LoopSlot(loop="morning", hour=7, minute=30),  # заниматься с 08:00
            LoopSlot(loop="evening", hour=20, minute=30),
            LoopSlot(loop="weekly", hour=18, minute=30, weekday=6),
        ],
    ),
}

DEFAULT_TUTOR = "interview"


class Settings(BaseSettings):
    # секреты (.env)
    tg_token: SecretStr  # бот interview; после load_settings — бот текущего тьютора
    tg_token_english: SecretStr | None = None
    tg_token_cards: SecretStr | None = None  # карточный бот SRS (этап 4 ENGLISH.md)
    tg_chat_id: int  # один и тот же человек во всех ботах: chat_id личного чата общий

    # Builtin и MCP-наборы сужаются по петлям: лишняя tool schema увеличивает
    # контекст, а недоступный tool надёжнее инструкции «не использовать».
    loop_tools: dict[str, list[str]] = {
        "deadlines": ["Read", "Edit", "Grep"],  # 61% всех запусков, работа на 4 тула
        "review": ["Read", "Grep", "Glob", "Write"],
        "weekly": ["Read", "Write", "Edit", "Glob", "Grep"],
    }
    loop_mcp: dict[str, list[str]] = {  # петля сужает набор ещё сильнее
        "deadlines": ["telegram"],  # Google не звала ни разу: −8 схем тулов
        "review": ["telegram"],
        # episode карточек не заводит: они появляются только после просмотра,
        # когда ученик разметил список — и это уже диалог, а не эта петля
        "episode": ["telegram", "transcript"],
    }
    mcp_servers: list[str] = MCP_SERVERS  # разрешает for_tutor из TutorConfig
    # Сколько новых слов в день показывает карточный бот. Ограничение живёт в
    # коде, а не в промпте: 2026-08-11 вечерняя петля завела 15 незнакомых
    # фразовых глаголов разом, и повторение на этом встало. Дефолт Anki — 20
    # новых в день на всё; для взрослого со словарём с нуля рабочая доза меньше.
    cards_new_per_day: int = 5
    # Голос озвучки примеров (Kokoro). Британский: учебник британский и IELTS
    # чаще слушают в этом акценте. Полный список — `k.get_voices()`;
    # af_*/am_* американские, bf_*/bm_* британские.
    tts_voice: str = "bf_emma"
    backup_folder: str | None = None
    # Таймаут HTTP-запросов к Telegram. Дефолт python-telegram-bot — 5 с на всё,
    # и этого не хватает: 2026-08-15 ответ тьютора не ушёл вовсе, потому что
    # запрос через прокси идёт ~7 с и плашка «думаю» упала с TimedOut, забрав
    # с собой весь ответ. Таймаут здесь — предохранитель от зависшего сокета,
    # а не бюджет скорости: держать его щедрым дешевле, чем терять сообщения.
    tg_timeout_s: float = 30
    # Сколько сессии позволено молчать. Сторож не против долгой сессии, а против
    # мёртвой: 2026-08-19 запрос к API ушёл в подвисшее соединение прокси, и оба
    # бота замолчали навсегда — вокруг сессии таймаутов не было вовсе, а сокет
    # так и стоял открытым с пустыми очередями. Считаем паузу между сообщениями,
    # а не общую длину: работающая петля не виновата, что она длинная. По логам
    # за июль-август пауза p99 — 48 с, худшая живая — 612 с (deadlines), так что
    # 900 с не заденет никого из ныне живущих.
    session_idle_timeout_s: float = 900
    timezone: str = "Europe/Belgrade"
    otlp_endpoint: str = "http://localhost:4317"  # Phoenix; пустая строка — трейсинг выключен

    # Exact ids were verified through App Server models/list on 2026-08-27.
    codex_model: str = "gpt-5.6-terra"
    codex_loop_models: dict[str, str] = {
        "chat": "gpt-5.6-terra",
        "morning": "gpt-5.6-terra",
        "evening": "gpt-5.6-terra",
        "check": "gpt-5.6-terra",
        "weekly": "gpt-5.6-terra",
        "onboarding": "gpt-5.6-terra",
        "replan": "gpt-5.6-terra",
        "deadlines": "gpt-5.6-luna",
        "review": "gpt-5.6-terra",
        "episode": "gpt-5.6-terra",
    }
    codex_loop_effort: dict[str, str] = {
        "chat": "high",
        "morning": "high",
        "evening": "high",
        "check": "high",
        "weekly": "high",
        "onboarding": "high",
        "replan": "high",
        "deadlines": "low",
        "review": "medium",
        "episode": "medium",
    }
    # Optimization targets are acceptance metrics, not cancellation thresholds.
    # Models and effort stay unchanged while context/turns are optimized.
    codex_loop_optimization_targets: dict[str, int] = {
        "deadlines": 25_000,
        "chat": 100_000,
        "morning": 120_000,
        "evening": 120_000,
        "weekly": 150_000,
        "episode": 120_000,
        "check": 180_000,
        "review": 150_000,
        "onboarding": 180_000,
        "replan": 300_000,
    }
    # Disabled until the fake eval-pack proves that these ceilings do not cut
    # acceptable work. They only guard runaway sessions, never normal targets.
    codex_loop_runaway_ceilings: dict[str, int] = {
        "deadlines": 50_000,
        "chat": 200_000,
        "morning": 240_000,
        "evening": 240_000,
        "weekly": 300_000,
        "episode": 240_000,
        "check": 360_000,
        "review": 300_000,
        "onboarding": 360_000,
        "replan": 600_000,
    }
    codex_loop_step_ceilings: dict[str, int] = {
        "deadlines": 4,
        "chat": 8,
        "morning": 8,
        "evening": 8,
        "weekly": 8,
        "episode": 8,
        "check": 8,
        "review": 8,
        "onboarding": 12,
        "replan": 20,
    }
    codex_enforce_runaway_budget: bool = False
    # A dedicated product CODEX_HOME prevents global developer persona, skills,
    # memories and project instructions from entering tutor sessions.
    codex_home: Path | None = None
    # Single-user isolation is currently the Docker container. Multi-user Codex
    # sandboxing is a separate project after M10.
    codex_sandbox: Literal["read-only", "workspace-write", "full-access"] = "full-access"

    # кого обслуживает процесс; поля ниже разрешает for_tutor()
    tutor: str = DEFAULT_TUTOR
    tutors: dict[str, TutorConfig] = TUTORS
    drive_folder: str = TUTORS[DEFAULT_TUTOR].drive_folder
    tutor_dir: Path = TUTORS[DEFAULT_TUTOR].tutor_dir

    # Load only active dotenv fields so retired provider credentials need not be
    # read or copied during migration. Unknown explicit settings still fail fast.
    model_config = SettingsConfigDict(
        env_file=".env",
        dotenv_filtering="only_existing",
        extra="forbid",
    )

    def codex_model_for(self, loop_name: str) -> str:
        return self.codex_loop_models.get(loop_name, self.codex_model)


def for_tutor(settings: Settings, name: str) -> Settings:
    """Разрешить настройки под конкретного тьютора: его память, его Drive-папка,
    его бот и MCP. Возвращает копию — Settings остаётся неизменяемым."""
    cfg = settings.tutors.get(name)
    if cfg is None:
        raise SystemExit(
            f"Неизвестный тьютор {name!r}. Известные: {', '.join(settings.tutors)}"
        )
    token = getattr(settings, cfg.token_field, None)
    if token is None:
        raise SystemExit(
            f"В .env нет {cfg.token_field.upper()} — токена бота тьютора {name!r}. "
            "Образец — .env.example"
        )
    return settings.model_copy(update={
        "tutor": name,
        "tutor_dir": cfg.tutor_dir,
        "drive_folder": cfg.drive_folder,
        "tg_token": token,
        "mcp_servers": cfg.mcp_servers,
        "backup_folder": cfg.backup_folder,
    })


def apply_timezone_override(settings: Settings) -> Settings:
    """Таймзону ученик меняет словами в чате: агент пишет IANA-имя в файл
    `timezone` в своей памяти, код его только читает (ADR-2: фичи — не код).

    Нужна и харнесу (датировать логи), и healthcheck'у (искать лог за сегодня).
    """
    tz_file = PROJECT_ROOT / settings.tutor_dir / "timezone"
    if not tz_file.exists():
        return settings
    name = tz_file.read_text(encoding="utf-8").strip()
    try:
        ZoneInfo(name)
    except (KeyError, ValueError):
        return settings  # кривое имя — остаёмся на конфиге, петля не падает
    return settings.model_copy(update={"timezone": name})


def load_settings(tutor: str = DEFAULT_TUTOR) -> Settings:
    """Точка входа для bot/loop: падает с понятной ошибкой, а не тьютор молчит в 8 утра."""
    try:
        settings = Settings()
    except ValidationError as e:
        missing = [
            str(err["loc"][0]).upper() for err in e.errors() if err["type"] == "missing"
        ]
        if missing:
            raise SystemExit(
                f"В .env не хватает переменных: {', '.join(missing)}. Образец — .env.example"
            ) from e
        raise
    return for_tutor(settings, tutor)
