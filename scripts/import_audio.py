#!/usr/bin/env python3
"""Разовый импорт class audio Solutions в память тьютора.

Аудио лежит в `docs/materials/audio/` (вне памяти тьютора и вне образа), а тул
`send_audio` берёт файл из `tutors/english/audio/` — той же директории, что уже
приезжает в контейнер волюмом. Поэтому копируем, а не симлинкуем: симлинк
в `docs/` внутри контейнера никуда не ведёт.

Имена не трогаем: `track_1.13.mp3` — это ровно тот номер, которым учебник
помечает задание («$ 1.13 page 12» в ключах, «▼1.13» на странице тетради),
так что агент называет трек тем же номером, что видит в книге.

Запуск: uv run python scripts/import_audio.py [--tutor english]
"""

import argparse
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path,
                        default=PROJECT_ROOT / "docs" / "materials" / "audio")
    parser.add_argument("--tutor", default="english")
    args = parser.parse_args()

    tracks = sorted(args.source.glob("track_*.mp3"))
    if not tracks:
        raise SystemExit(f"В {args.source} нет файлов track_*.mp3 — положи туда class audio.")

    target = PROJECT_ROOT / "tutors" / args.tutor / "audio"
    target.mkdir(parents=True, exist_ok=True)

    copied = skipped = 0
    for track in tracks:
        dest = target / track.name
        if dest.exists() and dest.stat().st_size == track.stat().st_size:
            skipped += 1
            continue
        shutil.copy2(track, dest)
        copied += 1

    size_mb = sum(f.stat().st_size for f in target.glob("*.mp3")) / 1024 / 1024
    cds = sorted({f.name.split("_")[1].split(".")[0] for f in target.glob("*.mp3")})
    print(f"{target}: {len(list(target.glob('*.mp3')))} треков, {size_mb:.0f} МБ "
          f"(CD {', '.join(cds)}); скопировано {copied}, уже было {skipped}")


if __name__ == "__main__":
    sys.exit(main())
