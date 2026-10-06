"""Установка озвучки: скачать веса Kokoro и озвучить карточки без звука.

Веса (311 МБ + 27 МБ) в git не лежат и в образ не пекутся — они падают в
`logs/tts/`, который смонтирован волюмом и переживает пересборку.

    uv run python scripts/setup_tts.py             # скачать и озвучить всё, что молчит
    uv run python scripts/setup_tts.py --backfill  # только озвучить
"""

import argparse
import urllib.request

from tutor import srs, tts
from tutor.config import load_settings


def download() -> None:
    for path, url in tts.DOWNLOADS.items():
        if path.exists():
            print(f"  уже есть: {path.name} ({path.stat().st_size / 2**20:.0f} МБ)")
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        print(f"  качаю {path.name}…")
        urllib.request.urlretrieve(url, path)
        print(f"  готово: {path.stat().st_size / 2**20:.0f} МБ")


def backfill(voice: str) -> None:
    if not tts.available():
        raise SystemExit("нет весов модели — сначала прогон без --backfill")
    with srs.session() as conn:
        rows = conn.execute(
            "SELECT id, example FROM cards WHERE example <> '' AND suspended = 0"
        ).fetchall()
    done = skipped = 0
    for row in rows:
        if tts.card_audio(row["id"]):
            skipped += 1
            continue
        if tts.speak_card(row["id"], row["example"], voice):
            done += 1
            print(f"  [{row['id']}] {tts.strip_ipa(row['example'])[:60]}")
    silent = len(rows) - done - skipped
    print(f"\nозвучено: {done}, уже было: {skipped}, без звука: {silent}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backfill", action="store_true",
                        help="не качать, только озвучить карточки без звука")
    args = parser.parse_args()
    if not args.backfill:
        download()
    backfill(load_settings().tts_voice)


if __name__ == "__main__":
    main()
