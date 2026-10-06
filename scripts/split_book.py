#!/usr/bin/env python3
"""Разовая нарезка Solutions Intermediate (3rd ed) по юнитам.

Зачем: сессия тьютора не должна открывать 165-страничный PDF, чтобы посмотреть
один юнит. Режем один раз, дальше агент читает `book/wb/unit-03.pdf` и получает
ровно то, что нужно.

Запуск: uv run python scripts/split_book.py [--materials docs/materials] [--out tutors/english/book]

Границы — данные, а не эвристика: они выведены из книги один раз (2026-08-09)
и перекрёстно проверены тремя независимыми источниками, см. комментарии к
таблицам. Скрипт печатает первую строку каждого куска — глазами видно,
что нарезано по месту.

Ключевой факт нумерации: **0-based индекс страницы PDF равен печатному номеру
страницы** и в Student's Book, и в Workbook (проверено по колонтитулам).
Поэтому таблицы ниже читаются прямо из оглавления учебника.
"""

import argparse
import re
import sys
from pathlib import Path

from pypdf import PdfReader, PdfWriter

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Юниты книги: номер → (название, ...). Introduction в книге называется «Unit I»,
# у нас это unit-00 — так имена файлов остаются предсказуемыми для агента.
UNIT_NAMES = {
    0: "Introduction", 1: "Generations", 2: "Leisure time", 3: "The human body",
    4: "Home", 5: "Technology", 6: "High flyers", 7: "Artists", 8: "Messages",
    9: "Journeys",
}

# Student's Book, 165 страниц. Границы юнитов — из оглавления, подтверждены
# страницами «Unit map» (8, 52, 64, 86 нашлись текстом) и разделами
# «Map of resources» книги учителя («5A Vocabulary Student's Book, pages 52–53»).
# Exam Skills Trainer идёт после нечётных юнитов и вложен в них: он проверяет
# именно этот материал.
SB_UNITS = {
    0: (4, 7), 1: (8, 19), 2: (20, 29), 3: (30, 41), 4: (42, 51),
    5: (52, 63), 6: (64, 73), 7: (74, 85), 8: (86, 95), 9: (96, 107),
}
SB_REFS = {
    # оглавление: по нему агент собирает syllabus.md сам — своими глазами
    # по книге, а не с моих слов (границы выше — тоже с него, но проверяемо)
    "contents": (2, 3),
    "b2-exam-skills-trainer": (108, 111),
    "culture-bank": (112, 120),
    "vocabulary-builder": (121, 125),
    "grammar-builder": (126, 143),
    "extra-speaking-tasks": (144, 144),
    "dvd-worksheets-and-self-check": (145, 162),
}

# Workbook, 137 страниц, скан без текстового слоя — границы сняты с оглавления
# (стр. 3 файла) глазами и проверены по колонтитулу: PDF-страница 9 (1-based)
# подписана «8 Unit 1 Generations».
WB_UNITS = {
    0: (4, 7), 1: (8, 17), 2: (18, 29), 3: (30, 39), 4: (40, 51),
    5: (52, 61), 6: (62, 73), 7: (74, 83), 8: (84, 95), 9: (96, 107),
}
WB_REFS = {
    "contents": (3, 3),  # одна страница, но полная карта: юниты, уроки A–H, номера страниц
    "b2-exam-skills-trainer": (108, 111),
    "cumulative-review": (112, 121),
    "writing-bank": (122, 125),
    "functions-bank": (126, 127),
    "wordlist": (128, 135),
    "irregular-verbs": (136, 136),
}

# Essentials Teacher's Book, 96 страниц. Два разных набора ключей:
#   - ответы и транскрипты к Student's Book: разделы начинаются страницей
#     «Map of resources» (найдены текстом: 3, 5, 10, 15, 20, 26, 31, 36, 41, 46);
#   - «Workbook answer keys and transcripts» (с 60-й) — ЭТАЛОН ПРОВЕРКИ листов.
# Границы ключей к тетради выведены по ссылкам «page NN» внутри страницы:
# страница отнесена к юниту, если ссылается на его диапазон ≥2 раз. Пограничные
# страницы попадают в оба юнита (на одной странице хвост одного и начало
# другого) — поэтому диапазоны местами перекрываются, это не ошибка.
KEYS_SB = {
    0: (3, 4), 1: (5, 9), 2: (10, 14), 3: (15, 19), 4: (20, 25),
    5: (26, 30), 6: (31, 35), 7: (36, 40), 8: (41, 45), 9: (46, 51),
}
KEYS_WB = {
    0: (60, 60), 1: (61, 63), 2: (64, 67), 3: (67, 70), 4: (70, 72),
    5: (73, 75), 6: (76, 79), 7: (80, 82), 8: (82, 86), 9: (86, 90),
}

SOURCES = {
    "sb": "solutions-intermediate-3ed-students-book.pdf",
    "wb": "solutions-intermediate-3ed-workbook.pdf",
    "tb": "solutions-intermediate-3ed-essentials-teachers-book.pdf",
}


def _head(reader: PdfReader, page: int) -> str:
    """Первая осмысленная строка страницы — для глазной проверки нарезки.
    У скана (workbook) текстового слоя нет, там честное «скан»."""
    text = (reader.pages[page].extract_text() or "").strip()
    if not text:
        return "(скан без текстового слоя)"
    line = " ".join(text.split())
    return line[:64]


def cut(reader: PdfReader, first: int, last: int, out: Path) -> None:
    writer = PdfWriter()
    for i in range(first, last + 1):
        writer.add_page(reader.pages[i])
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("wb") as f:
        writer.write(f)


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--materials", type=Path, default=PROJECT_ROOT / "docs" / "materials")
    parser.add_argument("--out", type=Path, default=PROJECT_ROOT / "tutors" / "english" / "book")
    args = parser.parse_args()

    readers = {}
    for key, name in SOURCES.items():
        path = args.materials / name
        if not path.exists():
            raise SystemExit(f"Нет файла {path}. Учебники лежат в {args.materials} "
                             "(в git их нет — только на диске).")
        readers[key] = PdfReader(path)

    # (каталог, откуда режем, таблица юнитов, таблица справочных разделов)
    jobs = [
        ("sb", "sb", SB_UNITS, SB_REFS),
        ("wb", "wb", WB_UNITS, WB_REFS),
        ("keys", "tb", KEYS_WB, {}),        # ключи к тетради — эталон проверки
        ("keys/sb", "tb", KEYS_SB, {}),     # ответы и транскрипты к учебнику
    ]

    total = 0
    for folder, source, units, refs in jobs:
        reader = readers[source]
        print(f"\n=== {folder}/ ← {SOURCES[source]} ===")
        for num, (first, last) in sorted(units.items()):
            out = args.out / folder / f"unit-{num:02d}.pdf"
            cut(reader, first, last, out)
            total += 1
            print(f"  unit-{num:02d}.pdf  стр {first:3}–{last:3} "
                  f"({last - first + 1:2}) {UNIT_NAMES[num]:15} | {_head(reader, first)}")
        for name, (first, last) in refs.items():
            out = args.out / folder / f"ref-{slug(name)}.pdf"
            cut(reader, first, last, out)
            total += 1
            print(f"  ref-{slug(name)}.pdf  стр {first}–{last} | {_head(reader, first)}")

    print(f"\nГотово: {total} файлов в {args.out}")
    print("Проверь глазами колонку справа: заголовок должен совпадать с юнитом.")


if __name__ == "__main__":
    sys.exit(main())
