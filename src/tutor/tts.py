"""Озвучка примеров карточек: Kokoro-82M локально, на CPU, без сети.

Почему свой синтез, а не запись носителя: пример на карточке — предложение,
которое сочинил тьютор под конкретную ошибку, и готовой записи для него не
существует. Слово в изоляции звучит не так, как в речи (`run out of` →
/rʌn‿aʊɾ‿əv/), поэтому озвучивается именно пример, а не лицо карточки.

Почему ONNX, а не пакет `kokoro`: тот тянет torch — плюс два с половиной
гигабайта в образ ради модели на 82M параметров. Здесь onnxruntime и веса
файлом: ~180 МБ зависимостей, 0.4 с на фразу.

**IPA-разметка.** G2P espeak уверенно ошибается на техническом жаргоне:
проверено 2026-08-15 — `Kubernetes` → «кьюбернитс», `CUDA` → «кьюда»,
`PostgreSQL` теряет слог, омограф `lead` (свинец) читается как «лид». Поэтому
в тексте примера разрешена разметка `[Kubernetes](/ˌkuːbərˈnɛtɪs/)`: помеченное
слово уходит в синтез транскрипцией, всё остальное фонемизируется обычным
путём. На экране разметка не видна — `strip_ipa` снимает её при показе.

Модель в git не лежит (311 МБ): `uv run python scripts/setup_tts.py`.
"""

import logging
import re
from pathlib import Path

from tutor.config import PROJECT_ROOT

log = logging.getLogger("tutor.tts")

MODEL_DIR = PROJECT_ROOT / "logs" / "tts"
MODEL_PATH = MODEL_DIR / "kokoro-v1.0.onnx"
VOICES_PATH = MODEL_DIR / "voices-v1.0.bin"
AUDIO_DIR = PROJECT_ROOT / "logs" / "card_audio"

RELEASE = ("https://github.com/thewh1teagle/kokoro-onnx/releases/download/"
           "model-files-v1.0/")
DOWNLOADS = {MODEL_PATH: RELEASE + "kokoro-v1.0.onnx",
             VOICES_PATH: RELEASE + "voices-v1.0.bin"}

# Где espeak-ng лежит в debian. Пути явные, потому что espeakng-loader (внутри
# kokoro-onnx) по умолчанию ищет свою сборку по пути CI-раннера, которого на
# машине нет, и падает с «espeak not installed» при живом системном espeak.
ESPEAK_LIB = "/usr/lib/x86_64-linux-gnu/libespeak-ng.so.1"
ESPEAK_DATA = "/usr/lib/x86_64-linux-gnu/espeak-ng-data"

# [слово](/транскрипция/) — синтаксис misaki, здесь разбирается вручную:
# ONNX-сборка markdown не понимает, а фонемы принимает напрямую.
IPA = re.compile(r"\[([^\[\]]+)\]\(/([^/()]+)/\)")

_engine = None
_tokenizer = None


def strip_ipa(text: str) -> str:
    """Убрать разметку произношения — на экран идёт обычный текст."""
    return IPA.sub(r"\1", text)


def available() -> bool:
    """Скачана ли модель. Нет — карточки просто живут без звука."""
    return MODEL_PATH.exists() and VOICES_PATH.exists()


def _load():
    """Модель и токенизатор одним ленивым синглтоном: 311 МБ не грузятся,
    пока никто не просил озвучку, но между карточками переиспользуются."""
    global _engine, _tokenizer
    if _engine is None:
        from kokoro_onnx import EspeakConfig, Kokoro, Tokenizer
        config = EspeakConfig(lib_path=ESPEAK_LIB, data_path=ESPEAK_DATA)
        _engine = Kokoro(str(MODEL_PATH), str(VOICES_PATH), espeak_config=config)
        _tokenizer = Tokenizer(espeak_config=config)
    return _engine, _tokenizer


def phonemize(text: str) -> str:
    """Текст → фонемы, с уважением к ручной разметке произношения."""
    _, tokenizer = _load()
    parts, pos = [], 0
    for match in IPA.finditer(text):
        head = text[pos:match.start()]
        if head.strip():
            parts.append(tokenizer.phonemize(head))
        parts.append(match.group(2))  # транскрипция едет как есть
        pos = match.end()
    tail = text[pos:]
    if tail.strip():
        parts.append(tokenizer.phonemize(tail))
    return " ".join(p for p in parts if p)


def card_audio(card_id: int) -> Path | None:
    """Готовый файл озвучки карточки, если он есть."""
    path = AUDIO_DIR / f"{card_id}.ogg"
    return path if path.exists() else None


def speak(text: str, dest: Path, voice: str) -> Path | None:
    """Синтезировать текст в файл. Возвращает путь или None.

    Никогда не бросает: озвучка — украшение карточки, и её отсутствие не повод
    ронять заведение карточки или показ в боте.
    """
    if not text.strip() or not available():
        return None
    try:
        import soundfile

        engine, _ = _load()
        audio, rate = engine.create(phonemize(text), voice=voice, is_phonemes=True)
        dest.parent.mkdir(parents=True, exist_ok=True)
        # OGG/OPUS — то, что Telegram принимает как голосовое; libsndfile умеет
        # его сам, так что ffmpeg в образе не нужен
        soundfile.write(dest, audio, rate, format="OGG", subtype="OPUS")
        return dest
    except Exception as e:
        log.warning("не удалось озвучить %r: %s", text[:40], e)
        return None


def speak_card(card_id: int, text: str, voice: str) -> Path | None:
    """Озвучить пример карточки. Повторный вызов ничего не пересчитывает."""
    done = card_audio(card_id)
    return done or speak(text, AUDIO_DIR / f"{card_id}.ogg", voice)
