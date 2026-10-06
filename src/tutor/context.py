"""Bounded, deterministic product context for one tutor session.

Canonical memory remains in tutors/{tutor}. This module only selects bounded
views for a loop so the model does not rediscover the same state through many
shell turns. Truncation is explicit and the original path remains available.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
import hashlib
from pathlib import Path
import re


@dataclass(frozen=True, slots=True)
class ContextPart:
    path: str
    strategy: str
    original_chars: int
    included_chars: int
    truncated: bool
    sha256: str
    content: str

    def manifest(self) -> dict[str, object]:
        data = asdict(self)
        data.pop("content")
        return data


@dataclass(frozen=True, slots=True)
class ContextBundle:
    text: str
    parts: tuple[ContextPart, ...]
    char_budget: int

    @property
    def included_chars(self) -> int:
        return sum(part.included_chars for part in self.parts)

    @property
    def manifest(self) -> list[dict[str, object]]:
        return [part.manifest() for part in self.parts]


_DEFAULT_BUDGETS = {
    "deadlines": 16_000,
    "chat": 32_000,
    "morning": 65_000,
    "evening": 70_000,
    "weekly": 80_000,
    "episode": 50_000,
    "check": 80_000,
    "onboarding": 70_000,
    "replan": 120_000,
    "review": 50_000,
}

_FILE_SPECS: dict[str, tuple[tuple[tuple[str, ...], int, str], ...]] = {
    "deadlines": (
        (("deadlines.md",), 11_000, "head_tail"),
        (("progress.md",), 5_000, "current"),
    ),
    "morning": (
        (("progress.md",), 18_000, "current"),
        (("deadlines.md",), 10_000, "head_tail"),
        (("curriculum.md", "syllabus.md"), 15_000, "head_tail"),
        (("errors.md",), 10_000, "open_errors"),
        (("profile.md",), 8_000, "head_tail"),
        (("ritual.md",), 5_000, "tail"),
    ),
    "evening": (
        (("progress.md",), 18_000, "current"),
        (("deadlines.md",), 8_000, "head_tail"),
        (("curriculum.md", "syllabus.md"), 13_000, "head_tail"),
        (("errors.md",), 14_000, "open_errors"),
        (("profile.md",), 8_000, "head_tail"),
        (("ritual.md",), 5_000, "tail"),
        (("wordbank.md",), 4_000, "tail"),
    ),
    "weekly": (
        (("progress.md",), 20_000, "current"),
        (("curriculum.md", "syllabus.md"), 16_000, "head_tail"),
        (("errors.md",), 12_000, "open_errors"),
        (("profile.md",), 8_000, "head_tail"),
        (("deadlines.md",), 6_000, "head_tail"),
        (("ritual.md",), 4_000, "tail"),
        (("wordbank.md",), 4_000, "tail"),
    ),
    "episode": (
        (("progress.md",), 14_000, "current"),
        (("profile.md",), 10_000, "head_tail"),
        (("sources.md",), 10_000, "head_tail"),
        (("syllabus.md",), 14_000, "head_tail"),
    ),
    "check": (
        (("progress.md",), 20_000, "current"),
        (("errors.md",), 20_000, "open_errors"),
        (("profile.md",), 12_000, "head_tail"),
        (("curriculum.md", "syllabus.md"), 16_000, "head_tail"),
        (("ritual.md",), 8_000, "tail"),
        (("wordbank.md",), 8_000, "tail"),
    ),
    "chat": (
        (("progress.md",), 7_000, "chat_progress"),
        (("curriculum.md", "syllabus.md"), 12_000, "chat_plan"),
        (("deadlines.md",), 5_000, "chat_deadlines"),
        (("errors.md",), 4_000, "chat_errors"),
        (("profile.md",), 4_000, "chat_profile"),
    ),
}

_DATE_IN_NAME = re.compile(r"(20\d{2})-(\d{2})-(\d{2})")
_H2 = re.compile(r"^## (?P<title>.+)$", re.MULTILINE)
_WORDS = re.compile(r"[\w-]+", re.UNICODE)
_QUERY_STOPWORDS = {
    "and", "what", "which", "with", "from", "this", "that", "then", "today",
    "без", "блок", "блока", "блоке", "будем", "быть", "взяться", "давай",
    "для", "дальше", "его", "есть", "изучать", "какой", "какую", "какие",
    "материал", "материала", "мне", "можно", "надо", "первый", "потом",
    "продолжим", "сегодня", "стоит", "теперь", "того", "тут", "этого",
}
_ORDINALS = {
    "первый": "1", "первая": "1", "первого": "1", "first": "1", "one": "1",
    "второй": "2", "вторая": "2", "второго": "2", "second": "2", "two": "2",
    "третий": "3", "третья": "3", "третьего": "3", "third": "3", "three": "3",
    "четвертый": "4", "четвёртый": "4", "fourth": "4", "four": "4",
    "пятый": "5", "fifth": "5", "five": "5",
}


def _query_terms(query: str) -> set[str]:
    words = {word.casefold().strip("_-") for word in _WORDS.findall(query)}
    terms = {word for word in words if len(word) >= 4 and word not in _QUERY_STOPWORDS}
    ordinals = {_ORDINALS[word] for word in words if word in _ORDINALS}
    for ordinal in ordinals:
        if words & {"блок", "блока", "блоке", "block"}:
            terms.update({f"блок {ordinal}", f"block {ordinal}"})
        elif words & {"unit", "юнит", "юнита"}:
            terms.update({f"unit-{int(ordinal):02d}", f"unit {ordinal}"})
        else:
            terms.add(ordinal)
    # Russian inflection and English suffixes should still meet their source term.
    terms.update(word[:6] for word in tuple(terms) if len(word) >= 8)
    return terms


def _h2_sections(text: str) -> tuple[str, list[tuple[str, str]]]:
    matches = list(_H2.finditer(text))
    if not matches:
        return "", [("", text)]
    preamble = text[:matches[0].start()].strip()
    sections: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections.append((match.group("title").strip(), text[match.start():end].strip()))
    return preamble, sections


def _section_score(title: str, body: str, terms: set[str]) -> int:
    title_folded = title.casefold()
    body_folded = body.casefold()
    score = 0
    for term in terms:
        score += title_folded.count(term) * 100
        score += body_folded.count(term)
    return score


def _pack_sections(
    text: str,
    limit: int,
    query: str,
    *,
    preferred_heading: re.Pattern[str] | None = None,
    include_preamble: int = 2_500,
    fallback_sections: int = 1,
) -> tuple[str, bool]:
    """Select complete relevant Markdown sections before applying a hard clip."""
    if len(text) <= limit:
        return text, False
    preamble, sections = _h2_sections(text)
    terms = _query_terms(query)
    ranked: list[tuple[int, int, str]] = []
    for index, (title, body) in enumerate(sections):
        score = _section_score(title, body, terms)
        if preferred_heading and preferred_heading.search(title):
            score += 100
        if score > 0:
            ranked.append((-score, index, body))
    if not ranked:
        ranked = [(0, index, body) for index, (_, body) in enumerate(sections[:fallback_sections])]

    chosen: list[tuple[int, str]] = []
    remaining = limit
    if preamble and include_preamble:
        clipped_preamble, _ = _clip(preamble, min(include_preamble, remaining), "head_tail")
        chosen.append((-1, clipped_preamble))
        remaining -= len(clipped_preamble) + 2
    for _, index, body in sorted(ranked):
        if remaining < 500:
            break
        section_cap = max(2_000, limit // 2)
        clipped, _ = _clip(
            body,
            min(len(body), remaining, section_cap),
            "head_tail",
        )
        chosen.append((index, clipped))
        remaining -= len(clipped) + 2
    selected = "\n\n".join(body for _, body in sorted(chosen))
    if not selected:
        return _clip(text, limit, "head_tail")[0], True
    return selected[:limit], True


_CURRENT_HEADING = re.compile(
    r"(?:текущее состояние|current (?:unit|state)|waiting on learner)", re.IGNORECASE
)
_PLAN_HEADING = re.compile(
    r"(?:status:\s*in progress|актуаль)", re.IGNORECASE
)
_DEADLINE_HEADING = re.compile(
    r"(?:актив|реальные даты|real dates|waiting|in flight|в пол[её]те)", re.IGNORECASE
)
_ERROR_HEADING = re.compile(r"(?:open|открыт)", re.IGNORECASE)
_PROFILE_HEADING = re.compile(
    r"(?:goal|цель|constraint|огранич|observation|наблюден|reasoning|формат)",
    re.IGNORECASE,
)


def _chat_clip(text: str, limit: int, strategy: str, query: str) -> tuple[str, bool]:
    if strategy == "chat_progress":
        return _pack_sections(
            text, limit, query, preferred_heading=_CURRENT_HEADING,
            include_preamble=1_000, fallback_sections=2,
        )
    if strategy == "chat_plan":
        return _pack_sections(
            text, limit, query, preferred_heading=_PLAN_HEADING,
            include_preamble=3_500, fallback_sections=1,
        )
    if strategy == "chat_deadlines":
        return _pack_sections(
            text, limit, query, preferred_heading=_DEADLINE_HEADING,
            include_preamble=1_000, fallback_sections=2,
        )
    if strategy == "chat_errors":
        return _pack_sections(
            text, limit, query, preferred_heading=_ERROR_HEADING,
            include_preamble=500, fallback_sections=1,
        )
    return _pack_sections(
        text, limit, query, preferred_heading=_PROFILE_HEADING,
        include_preamble=1_500, fallback_sections=2,
    )


def _clip(text: str, limit: int, strategy: str) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    omitted = len(text) - limit
    marker = f"\n\n...[bounded context omitted {omitted} chars; open source file if needed]...\n\n"
    room = max(0, limit - len(marker))
    if strategy == "tail":
        return marker + text[-room:], True
    if strategy == "current":
        match = re.search(r"^## Текущее состояние\s*$", text, re.MULTILINE)
        if match:
            following = text[match.start():]
            next_heading = re.search(r"^## (?!Текущее состояние).+$", following[3:], re.MULTILINE)
            section = following if next_heading is None else following[: next_heading.start() + 3]
            if len(section) <= limit:
                return section, True
            return section[:room] + marker, True
    if strategy == "open_errors":
        match = re.search(r"^## (?:Открытые|Open).*$", text, re.MULTILINE | re.IGNORECASE)
        if match:
            following = text[match.start():]
            next_heading = re.search(r"^## .+$", following[3:], re.MULTILINE)
            section = following if next_heading is None else following[: next_heading.start() + 3]
            if len(section) <= limit:
                return section, True
            return section[:room] + marker, True
    head = room // 3
    tail = room - head
    return text[:head] + marker + text[-tail:], True


def _first_existing(cwd: Path, candidates: tuple[str, ...]) -> Path | None:
    for candidate in candidates:
        path = cwd / candidate
        if path.is_file():
            return path
    return None


def _part(
    cwd: Path,
    path: Path,
    limit: int,
    strategy: str,
    *,
    query: str = "",
) -> ContextPart:
    text = path.read_text(encoding="utf-8")
    if strategy.startswith("chat_"):
        clipped, truncated = _chat_clip(text, limit, strategy, query)
    else:
        clipped, truncated = _clip(text, limit, strategy)
    return ContextPart(
        path=str(path.relative_to(cwd)),
        strategy=strategy,
        original_chars=len(text),
        included_chars=len(clipped),
        truncated=truncated,
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        content=clipped,
    )


def _recent_notes(cwd: Path, now: datetime, limit: int) -> list[ContextPart]:
    notes = cwd / "notes"
    if not notes.is_dir() or limit <= 0:
        return []
    cutoff = now.date() - timedelta(days=7)
    candidates: list[Path] = []
    for path in notes.rglob("*.md"):
        match = _DATE_IN_NAME.search(path.name)
        if match:
            day = datetime(*map(int, match.groups())).date()
            if day >= cutoff:
                candidates.append(path)
    # Stable names such as notes/units/unit-02.md carry the current unit.
    candidates.extend(path for path in notes.glob("units/*.md") if path not in candidates)
    result: list[ContextPart] = []
    remaining = limit
    for path in sorted(candidates, key=lambda item: item.stat().st_mtime, reverse=True):
        if remaining < 1_000:
            break
        part = _part(cwd, path, min(8_000, remaining), "tail")
        result.append(part)
        remaining -= part.included_chars
    return result


def build_context(
    cwd: Path,
    loop_name: str,
    *,
    dialog: str = "",
    query: str = "",
    now: datetime | None = None,
    char_budget: int | None = None,
) -> ContextBundle:
    """Build a bounded view without reading notes/archive indiscriminately."""
    now = now or datetime.now().astimezone()
    budget = char_budget or _DEFAULT_BUDGETS.get(loop_name, 70_000)
    parts: list[ContextPart] = []
    remaining = budget
    for candidates, requested, strategy in _FILE_SPECS.get(loop_name, _FILE_SPECS["chat"]):
        if remaining < 1_000:
            break
        path = _first_existing(cwd, candidates)
        if path is None:
            continue
        part = _part(
            cwd,
            path,
            min(requested, remaining),
            strategy,
            query=query,
        )
        parts.append(part)
        remaining -= part.included_chars
    if loop_name == "weekly" and remaining >= 1_000:
        recent = _recent_notes(cwd, now, min(20_000, remaining))
        parts.extend(recent)
        remaining -= sum(part.included_chars for part in recent)
    if dialog and loop_name != "chat" and remaining >= 500:
        clipped, truncated = _clip(dialog, min(12_000, remaining), "tail")
        wrapped = f"## Диалог в Telegram за последние 48 ч\n\n{clipped}"
        parts.append(ContextPart(
            path="telegram:48h",
            strategy="tail",
            original_chars=len(dialog),
            included_chars=len(wrapped),
            truncated=truncated,
            sha256=hashlib.sha256(dialog.encode("utf-8")).hexdigest(),
            content=wrapped,
        ))

    rendered = [
        "## Bounded product context",
        "",
        "This snapshot is derived from canonical files. Use it first. Do not reopen",
        "a source file unless its manifest says truncated/missing or the task needs",
        "an exact artifact. State updates still go to the canonical source files.",
    ]
    for part in parts:
        suffix = " [TRUNCATED]" if part.truncated else ""
        rendered.extend(["", f"### {part.path}{suffix}", "", part.content])
    return ContextBundle("\n".join(rendered), tuple(parts), budget)


def context_budget_for(loop_name: str) -> int:
    return _DEFAULT_BUDGETS.get(loop_name, 70_000)
