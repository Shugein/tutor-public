#!/usr/bin/env python3
"""Ротация progress.md: старые записи уезжают в archive/progress-YYYY-MM.md.

Зачем код, а не промпт. К 2026-08-15 `tutors/interview/progress.md` дорос до
310 КБ, и вечерняя петля 08-14 честно написала в своём же логе: «progress.md
огромный, прочитаю конец». Память, которую нельзя прочитать целиком, работает
как её отсутствие — тьютор перестал видеть собственный хвост долгов. Уборка
механическая и не требует суждения, поэтому её держит код: тот же принцип, что
с темпом новых карточек (`cards_new_per_day`) — правило, которое агент может
нарушить, не является ограничением.

Что считается записью. Блок — строка-заголовок (`## `, `### ` или `- ` на нулевом
отступе) со всем, что до следующего такого заголовка. Дата блока — первая
`YYYY-MM-DD` в его заголовке. Блоки без даты (постоянные разделы: «Onboarding»,
«План подготовки», «Waiting on the learner») не двигаются никогда. Ничего не
удаляется — записи переезжают дословно, в исходном порядке.

Идемпотентно: второй прогон подряд не делает ничего.

    uv run python scripts/rotate_progress.py                # все тьюторы, dry-run по --dry-run
    uv run python scripts/rotate_progress.py --tutor english --keep-days 30
"""

from __future__ import annotations

import argparse
import re
from datetime import date, timedelta
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tutor.config import PROJECT_ROOT, TUTORS  # noqa: E402

HEADER = re.compile(r"^(#{2,3} |- )")
DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
POINTER = "_История старше окна — в `archive/`"

# Порог, ниже которого файл не трогаем: пока память читается целиком, ротация
# только рвёт контекст. english на 2026-08-15 — 28 КБ, ему нечего чистить.
DEFAULT_MAX_KB = 60
DEFAULT_KEEP_DAYS = 14
FLOOR_DAYS = 3  # последние дни не архивируем никогда, каким бы ни был размер


class Block:
    __slots__ = ("day", "lines")

    def __init__(self, day: date | None, lines: list[str]):
        self.day = day
        self.lines = lines

    @property
    def size(self) -> int:
        return sum(len(line.encode()) for line in self.lines)

    @property
    def text(self) -> str:
        return "".join(self.lines)


def parse(text: str) -> tuple[list[str], list[Block]]:
    """Преамбула (до первого заголовка) + блоки."""
    preamble: list[str] = []
    blocks: list[Block] = []
    for line in text.splitlines(keepends=True):
        if HEADER.match(line):
            m = DATE.search(line)
            day = date(*map(int, m.groups())) if m else None
            blocks.append(Block(day, [line]))
        elif blocks:
            blocks[-1].lines.append(line)
        else:
            preamble.append(line)
    # Early English progress entries used a flat list: the date lived inside
    # the bullet body instead of its first line, so rotation found nothing.
    # Infer only for list blocks. Permanent named sections such as
    # "## Current unit" may mention old dates and must never move.
    for block in blocks:
        if block.day is None and block.lines[0].startswith("- "):
            match = DATE.search(block.text)
            if match:
                block.day = date(*map(int, match.groups()))
    return preamble, blocks


def split(blocks: list[Block], today: date, keep_days: int, max_kb: int
          ) -> tuple[list[Block], list[Block]]:
    """(остаётся, уезжает). Окно по дням, а сверху — бюджет размера: проблема
    не в возрасте записи, а в том, что файл перестал читаться."""
    floor = today - timedelta(days=FLOOR_DAYS)
    cutoff = today - timedelta(days=keep_days)
    stays = [b.day is None or b.day >= cutoff for b in blocks]

    budget = max_kb * 1024
    used = sum(b.size for b, keep in zip(blocks, stays) if keep)
    if used > budget:
        # подрезаем самые старые из оставшихся, пока не влезем в бюджет
        order = sorted((i for i, keep in enumerate(stays)
                        if keep and blocks[i].day is not None and blocks[i].day < floor),
                       key=lambda i: blocks[i].day)
        for i in order:
            if used <= budget:
                break
            stays[i] = False
            used -= blocks[i].size
    return ([b for b, keep in zip(blocks, stays) if keep],
            [b for b, keep in zip(blocks, stays) if not keep])


def rotate(progress: Path, today: date, keep_days: int, max_kb: int,
           dry_run: bool) -> str:
    text = progress.read_text(encoding="utf-8")
    if len(text.encode()) <= max_kb * 1024:
        return f"{progress}: {len(text.encode()) // 1024} КБ — ниже порога, не трогаю"

    preamble, blocks = parse(text)
    stays, moves = split(blocks, today, keep_days, max_kb)
    if not moves:
        return f"{progress}: архивировать нечего"

    by_month: dict[str, list[Block]] = {}
    for b in moves:
        by_month.setdefault(f"{b.day:%Y-%m}", []).append(b)

    # archive/, а не notes/: notes — Obsidian-vault ученика, и 170 КБ дампа
    # без frontmatter засоряют его граф и поиск
    archive_dir = progress.parent / "archive"
    if not dry_run:
        archive_dir.mkdir(parents=True, exist_ok=True)
        for month, month_blocks in by_month.items():
            path = archive_dir / f"progress-{month}.md"
            head = "" if path.exists() else (
                f"# Progress — архив {month}\n\n"
                f"_Выгружено из `progress.md` ротацией (scripts/rotate_progress.py). "
                f"Порядок записей — как в оригинале._\n\n")
            with path.open("a", encoding="utf-8") as fh:
                fh.write(head + "".join(b.text for b in month_blocks))

        if POINTER not in text:
            pointer = (f"{POINTER} по месяцам (`progress-YYYY-MM.md`); "
                       f"здесь — последние ~{keep_days} дней. Нужна старая запись — grep по архиву._\n\n")
            preamble = preamble[:1] + ["\n", pointer] if preamble else [pointer]
        tmp = progress.with_suffix(".md.tmp")
        tmp.write_text("".join(preamble) + "".join(b.text for b in stays), encoding="utf-8")
        tmp.replace(progress)  # атомарно: сессия рядом не прочитает половину файла

    moved_kb = sum(b.size for b in moves) // 1024
    left_kb = (sum(b.size for b in stays) + sum(len(x.encode()) for x in preamble)) // 1024
    return (f"{progress}: {len(text.encode()) // 1024} КБ → {left_kb} КБ; "
            f"в архив {len(moves)} записей ({moved_kb} КБ) "
            f"в {', '.join(sorted(by_month))}{' [dry-run]' if dry_run else ''}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tutor", choices=sorted(TUTORS), help="по умолчанию — все")
    ap.add_argument("--keep-days", type=int, default=DEFAULT_KEEP_DAYS)
    ap.add_argument("--max-kb", type=int, default=DEFAULT_MAX_KB)
    ap.add_argument("--today", help="YYYY-MM-DD, для тестов")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    today = date(*map(int, args.today.split("-"))) if args.today else date.today()
    names = [args.tutor] if args.tutor else sorted(TUTORS)
    for name in names:
        progress = PROJECT_ROOT / TUTORS[name].tutor_dir / "progress.md"
        if not progress.exists():
            print(f"{progress}: нет файла")
            continue
        print(rotate(progress, today, args.keep_days, args.max_kb, args.dry_run))


if __name__ == "__main__":
    main()
