"""Рендер листа урока: markdown агента → PDF под рукописное решение.

Лист — единственный носитель дня (решение 2026-08-11): теория, тесты и
письменные задания приходят одним документом, а не россыпью сообщений и
опросов. Разорванный на девять уведомлений урок нельзя ни пройти подряд,
ни перечитать через неделю; PDF можно.

Почему не Google Docs: лист читают глазами и пишут по нему стилусом —
нужен фиксированный A4 с полями под ответ, а не редактируемый документ.

Почему без Drive: готовый PDF уходит прямо в Telegram файлом — с iPad он
открывается в GoodNotes из шторки «Поделиться», без похода в Drive и без
расширения OAuth-скоупа. Копия остаётся в `worksheets/` памяти тьютора,
чтобы разбор мог на неё сослаться и лист можно было переслать заново.

Свой синтаксис не выдумываем, берём markdown как есть:
- `___` отдельной строкой → `<hr>`, CSS делает из него линейку для письма;
- `- [ ] вариант` / `- [x] верный` (списки задач GFM) → квадратик под
  галочку. Правильный вариант помечает агент, но порядок задаёт код: см.
  `prepare_choices`.
"""

import io
import re
from datetime import datetime
from pathlib import Path
from random import Random
from typing import NamedTuple

import markdown as md
from pypdf import PdfReader, PdfWriter
from weasyprint import CSS, HTML

from tutor.config import PROJECT_ROOT, Settings

# DejaVu Sans есть и на хосте, и в образе (fonts-dejavu-core) — латиница
# и кириллица одним шрифтом, без сюрпризов с квадратиками.
STYLE = """
@page {{
  size: A4;
  margin: 16mm 15mm 18mm 15mm;
  @bottom-left  {{ content: "{footer}"; font-size: 8.5pt; color: #999; }}
  @bottom-right {{ content: counter(page) " / " counter(pages);
                   font-size: 8.5pt; color: #999; }}
}}
body {{ font-family: "DejaVu Sans", sans-serif; font-size: 11pt;
        line-height: 1.45; color: #111; }}
h1 {{ font-size: 16pt; margin: 0 0 1mm; }}
h1 + p {{ color: #555; margin-bottom: 5mm; }}
/* Секции листа (теория / проверка / письмо) — глазу нужен якорь, иначе
   документ на две страницы читается как сплошной текст. */
h2 {{ font-size: 12.5pt; margin: 7mm 0 2.5mm; page-break-after: avoid;
      border-bottom: 1px solid #ccc; padding-bottom: 1mm; }}
h3 {{ font-size: 11pt; margin: 4mm 0 1.5mm; page-break-after: avoid; }}
p, li {{ margin: 0 0 2mm; }}
ol, ul {{ margin: 0 0 3mm 0; padding-left: 7mm; }}
li {{ page-break-inside: avoid; }}
/* Задания разделены линейками, поэтому markdown делает из каждого свой <ol>
   и нумерация сбрасывается на «1.». Сквозной счётчик по спискам верхнего
   уровня чинит это; вложенные списки нумеруются как обычно. Счётчик
   обнуляется на каждом заголовке секции: в листе их несколько, и номера
   должны совпадать с теми, что агент написал в markdown, — по ним потом
   сверяют ответы. */
body, h2 {{ counter-reset: task; }}
body > ol {{ list-style: none; }}
body > ol > li {{ counter-increment: task; position: relative; }}
body > ol > li::before {{ content: counter(task) "."; font-weight: 600;
                          position: absolute; left: -7mm; }}
strong {{ font-weight: 600; }}
em {{ color: #333; }}
code {{ font-family: "DejaVu Sans Mono", monospace; font-size: 10pt;
        background: #f3f3f3; padding: 0 1mm; }}
/* `___` в markdown — строка под ответ: место сверху, тонкая линия снизу */
hr {{ border: none; border-bottom: 1px solid #b9b9b9;
      height: 9mm; margin: 0 0 2mm; }}
blockquote {{ margin: 0 0 3mm; padding-left: 4mm; border-left: 2px solid #ddd;
              color: #444; }}
table {{ border-collapse: collapse; margin: 0 0 3mm; width: 100%; }}
th, td {{ border: 1px solid #ddd; padding: 1.2mm 2mm; text-align: left;
          vertical-align: top; }}
th {{ background: #f6f6f6; font-weight: 600; }}
/* Квадратик под галочку стилусом: рисуем рамкой, а не глифом шрифта —
   так размер не зависит от кегля и в него попадают пером. */
.box {{ display: inline-block; width: 4.2mm; height: 4.2mm;
        border: 1px solid #777; border-radius: 0.5mm;
        vertical-align: -0.7mm; margin-right: 2mm; }}
/* Вопрос с вариантами через разрыв страницы читается как два разных вопроса
   (поймали на первом боевом листе: A и B на одной странице, C и D на другой). */
.choices {{ page-break-inside: avoid; }}
"""

# Абзац, внутри которого есть квадратик, — это вопрос с вариантами
_CHOICE_P_RE = re.compile(r"<p>((?:(?!</p>).)*?<span class=\"box\".*?)</p>", re.S)

_CHOICE_RE = re.compile(r"^(\s*)[-*]\s+\[([ xX])\]\s+(.+?)\s*$")
_LETTERS = "ABCDEFGHIJ"
# «1.», «- », «**» вокруг вопроса: в ключе нужен сам вопрос. `___` не трогаем —
# по пропуску вопрос и узнаётся.
_NOISE_RE = re.compile(r"^[\s>*-]*(\d+[.)])?\s*|[*`]")


class Answer(NamedTuple):
    """Строка ключа: вопрос и буквы верных вариантов ('B' или 'B, D')."""
    question: str
    letters: str


def _question_of(lines: list[str], start: int) -> str:
    """Ближайшая непустая строка над блоком вариантов — это вопрос."""
    for line in reversed(lines[:start]):
        if line.strip():
            return _NOISE_RE.sub("", line.strip())[:80]
    return "(без вопроса)"


def _render_choices(group: list[re.Match], rng: Random) -> tuple[list[str], str]:
    """Блок `- [ ]` → строки с квадратиками; возвращает их и буквы верных."""
    # Отступ округляем вверх до 4 пробелов: с меньшим python-markdown считает
    # абзац концом списка, и варианты вываливаются из-под своего задания.
    depth = -(-len(group[0].group(1)) // 4)
    indent = " " * (4 * depth)
    options = [(m.group(3), m.group(2) != " ") for m in group]
    if len(options) > len(_LETTERS):
        raise ValueError(f"вариантов в одном вопросе больше {len(_LETTERS)} "
                         f"(«{group[0].group(3)[:40]}…») — разбей вопрос")
    if any(correct for _, correct in options):
        rng.shuffle(options)
    letters = ", ".join(_LETTERS[i] for i, (_, correct) in enumerate(options) if correct)
    # Строки абзаца, а не <p> целиком: свой блочный тег внутри пункта списка
    # python-markdown выкидывает наружу, и варианты уезжают из-под задания.
    # <br> тегом, а не двумя пробелами на конце: пробелы теряются при правке.
    rows = [f'{indent}<span class="box"></span> **{_LETTERS[i]}.** {text}<br>'
            for i, (text, _) in enumerate(options)]
    rows[-1] = rows[-1].removesuffix("<br>")
    return rows, letters or "—"


def prepare_choices(markdown_text: str,
                    rng: Random | None = None) -> tuple[str, list[Answer]]:
    """Варианты ответа → квадратики под галочку; ключ уезжает наверх, не в лист.

    Порядок вариантов перемешивает код, а не модель: модель систематически
    ставит верный первым (та же причина, что у shuffle_options для quiz-
    опросов), и лист, где ответ всегда A, проверяет не знание, а внимание.
    Поэтому буквы ключа известны только после перемешивания — тул отдаёт их
    агенту, тот кладёт их в память, и вечерний разбор проверяет по ним.

    Блок без единого `[x]` — не ошибка, а чек-лист («отметь, что услышал»):
    порядок в нём осмысленный, поэтому его не трогаем, а в ключе пишем «—».
    """
    rng = rng or Random()
    lines = markdown_text.splitlines()
    out: list[str] = []
    key: list[Answer] = []
    group: list[re.Match] = []
    group_start = 0

    def flush() -> None:
        if not group:
            return
        rows, letters = _render_choices(group, rng)
        if out and out[-1].strip():  # вопрос строкой выше — не слипаться с ним
            out[-1] += "<br>"
        out.extend(rows)
        key.append(Answer(_question_of(lines, group_start), letters))
        group.clear()

    for i, line in enumerate(lines):
        match = _CHOICE_RE.match(line)
        if match:
            if not group:
                group_start = i
            group.append(match)
            continue
        flush()
        out.append(line)
    flush()
    return "\n".join(out), key


def _safe(name: str) -> str:
    """Имя от модели → безопасный кусок пути (без слэшей и '..')."""
    return "".join(c if c.isalnum() or c in "-_" else "-" for c in name).strip("-")


def worksheets_dir(settings: Settings) -> Path:
    return PROJECT_ROOT / settings.tutor_dir / "worksheets"


def worksheet_path(settings: Settings, when: datetime, unit: str) -> Path:
    """worksheets/<дата>-<юнит>.pdf — на разбор ссылаются этим именем."""
    return worksheets_dir(settings) / f"{when:%Y-%m-%d}-{_safe(unit) or 'worksheet'}.pdf"


def free_worksheet_path(settings: Settings, when: datetime, unit: str) -> Path:
    """Тот же путь, но не поверх существующего листа: `...-2.pdf`, `...-3.pdf`.

    Два листа за день бывают — ручной прогон петли, пересдача темы. Перезапись
    оставила бы вечернюю проверку с ключом от одного листа и PDF от другого,
    причём молча: имена-то совпадают.
    """
    base = worksheet_path(settings, when, unit)
    path, n = base, 1
    while path.exists():
        n += 1
        path = base.with_name(f"{base.stem}-{n}.pdf")
    return path


# Страница учебника — это лист, который ученик читает, а не решает: текст,
# фотографии, схемы. Больше четырёх за раз — уже не «вот текст к уроку», а
# «вот тебе юнит, разбирайся»: лист перестаёт быть единственным носителем дня.
MAX_BOOK_PAGES = 4

# Только sb и wb: ключи (`book/keys/`) — эталон проверки, ученику их
# отправлять нельзя ни по какому поводу, поэтому их здесь просто нет.
BOOK_SOURCES = {"sb": "Student's Book", "wb": "Workbook"}


def book_pdf_path(settings: Settings, source: str, unit: str) -> Path:
    return (PROJECT_ROOT / settings.tutor_dir / "book" / _safe(source)
            / f"{_safe(unit)}.pdf")


def parse_pages(spec: str, total: int) -> list[int]:
    """«2», «1-3», «1,4-5» → индексы страниц с 1. Ошибка — текстом для агента.

    Нумерация — та же, что у Read по этому файлу (страницы внутри юнита), а не
    печатные номера книги: агент читает файл перед тем, как слать из него
    страницы, и видит ровно эти номера.
    """
    pages: list[int] = []
    for chunk in spec.replace(" ", "").split(","):
        if not chunk:
            continue
        bounds = chunk.split("-")
        if len(bounds) > 2 or not all(b.isdigit() for b in bounds):
            raise ValueError(f"не разобрать номера страниц «{spec}»: "
                             "пиши «2», «1-3» или «1,4-5»")
        first, last = int(bounds[0]), int(bounds[-1])
        if first > last:
            raise ValueError(f"диапазон «{chunk}» задом наперёд")
        pages.extend(range(first, last + 1))
    if not pages:
        raise ValueError("не указано ни одной страницы")
    pages = sorted(dict.fromkeys(pages))
    if pages[0] < 1 or pages[-1] > total:
        raise ValueError(f"в файле {total} страниц, а запрошены {pages[0]}–{pages[-1]}")
    if len(pages) > MAX_BOOK_PAGES:
        raise ValueError(f"{len(pages)} страниц за раз — многовато, "
                         f"максимум {MAX_BOOK_PAGES}: выбери разворот урока")
    return pages


def extract_pages(src: Path, spec: str) -> tuple[bytes, list[int]]:
    """Страницы юнита → отдельный PDF. Возвращает байты и что именно вырезано."""
    reader = PdfReader(src)
    pages = parse_pages(spec, len(reader.pages))
    writer = PdfWriter()
    for number in pages:
        writer.add_page(reader.pages[number - 1])
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue(), pages


def render_worksheet(markdown_text: str, title: str, footer: str,
                     rng: Random | None = None) -> tuple[bytes, list[Answer]]:
    """markdown → (PDF-байты, ключ). Заголовок отдельно: он же имя файла в TG.

    Ключ считается здесь, а не у вызывающего: единственный вход в рендер —
    единственное место, где `[x]` может утечь на лист ученику.
    """
    prepared, key = prepare_choices(markdown_text, rng)
    body = md.markdown(prepared, extensions=["sane_lists", "tables"])
    # класс вешаем по факту готового HTML, а не в markdown: свой блочный тег
    # python-markdown выкинул бы из пункта списка вместе с вариантами
    body = _CHOICE_P_RE.sub(r'<p class="choices">\1</p>', body)
    html = f"<h1>{title}</h1>\n{body}"
    data = HTML(string=html).write_pdf(
        stylesheets=[CSS(string=STYLE.format(footer=footer.replace('"', "'")))])
    return data, key
