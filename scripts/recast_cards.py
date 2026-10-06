"""Разовая переделка карточек первого прогона (2026-08-11..13) под новую модель.

Восемнадцать карточек — десять фразовых глаголов юнита 1E и словарь «привыкать
к новому» — были заведены как cloze с тремя пропусками на словах, которых
ученик не видел. Они переезжают в `recognition`: на лице само выражение, на
обороте значение и, где смысл прозрачен, из чего он складывается.

Расписание таких карточек сбрасывается: вопрос сменился, и история ответов на
прежний к нему не относится. Каждая пройдёт знакомство в боте заново, по пять
в день.

Оставшимся cloze (грамматика и собственные ошибки) дописываются подсказки там,
где без них карточка нерешаема.

Запуск: uv run python scripts/recast_cards.py [--dry-run]
"""

import argparse

from fsrs import Card

from tutor import srs

# front старой карточки → чем она становится. Значения взяты из её же оборота,
# сочинять здесь нечего; добавлен только разбор частиц там, где он честен —
# ложная этимология хуже её отсутствия.
RECOGNITION = {
    "We ___ ___ ___ time before the demo could even start.": (
        "run out of",
        "израсходовать весь запас чего-либо, остаться без — «запас выбежал»",
        "We ran out of time before the demo could even start.",
    ),
    "I can't ___ ___ ___ flaky tests that fail at random.": (
        "put up with",
        "терпеть, мириться с чем-то неприятным",
        "I can't put up with flaky tests that fail at random.",
    ),
    "I really ___ ___ ___ my old tech lead — she taught me everything.": (
        "look up to",
        "уважать, брать за образец — буквально «смотреть снизу вверх на кого-то»",
        "I really look up to my old tech lead — she taught me everything.",
    ),
    "I don't really ___ ___ ___ long meetings.": (
        "go in for",
        "увлекаться чем-то, быть любителем чего-то",
        "I don't really go in for long meetings.",
    ),
    "She ___ ___ ___ a Rust course to switch teams.": (
        "sign up for",
        "записаться на что-то, подписаться под участием",
        "She signed up for a Rust course to switch teams.",
    ),
    "He ___ ___ ___ skipping code review because nobody checked.": (
        "get away with",
        "сделать что-то плохое и не понести за это наказания — «уйти с этим»",
        "He got away with skipping code review because nobody checked.",
    ),
    "You can't ___ ___ ___ your word once you've promised.": (
        "go back on",
        "нарушить обещание, отступить от сказанного — «пойти назад по своему слову»",
        "You can't go back on your word once you've promised.",
    ),
    "A quick fix won't ___ ___ ___ the whole outage.": (
        "make up for",
        "компенсировать, возместить причинённое",
        "A quick fix won't make up for the whole outage.",
    ),
    "I need to ___ ___ ___ the team after my day off.": (
        "catch up with",
        "догнать тех, кто ушёл вперёд; наверстать упущенное",
        "I need to catch up with the team after my day off.",
    ),
    "I ___ ___ ___ my teammates — we never argue.": (
        "get on with",
        "ладить с кем-то, хорошо уживаться",
        "I get on with my teammates — we never argue.",
    ),
    "It took me a month to ___ ___ at the new company, but now I feel at home.": (
        "settle in",
        "освоиться, обжиться на новом месте или работе",
        "It took me a month to settle in at the new company.",
    ),
    "When you move abroad you have to ___ ___ a new culture.": (
        "adapt to",
        "приспособиться к новым условиям, поменяв своё поведение",
        "When you move abroad you have to adapt to a new culture.",
    ),
    "It took a week, but I finally got the ___ of the new deploy pipeline.": (
        "get the hang of something",
        "освоить, набить руку — научиться делать что-то как следует",
        "It took a week, but I finally got the hang of the new deploy pipeline.",
    ),
    "Since I started working from home I've settled into a comfortable morning ___.": (
        "routine",
        "заведённый распорядок: то, что делаешь регулярно и одинаково",
        "I've settled into a comfortable morning routine.",
    ),
    "It takes time to ___ ___ driving on the left.": (
        "adjust to",
        "привыкнуть к новому условию, подстроиться под него (+ существительное или -ing)",
        "It takes time to adjust to driving on the left.",
    ),
    "Living in a new time zone will ___ some getting used to.": (
        "take some getting used to",
        "требовать времени, чтобы стать привычным",
        "Living in a new time zone will take some getting used to.",
    ),
    "The streets felt completely ___ when I first arrived in Melbourne.": (
        "unfamiliar",
        "незнакомый, непривычный — тот, которого ты ещё не знаешь",
        "The streets felt completely unfamiliar when I first arrived.",
    ),
    "After months of standups, speaking English at work has become second ___.": (
        "second nature",
        "вторая натура: то, что делаешь автоматически, не задумываясь",
        "After months of standups, speaking English at work has become second nature.",
    ),
}

# Подсказки к тем cloze, что остаются: грамматика и собственные ошибки. Только
# там, где в самом задании нет опоры — у карточек с `(wear)`, `(play)`,
# `(pronoun)` подсказка уже встроена в лицо.
HINTS = {
    "The glasses felt strange at first, but I'll ___ ___ ___ them soon.":
        "привыкнуть к чему-то новому — три слова",
    "I played Dota for years but slowly lost interest ___ it.": "предлог",
    "I used to play a lot, but ___ time I stopped enjoying it.":
        "постепенно, с течением времени",
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    fresh = Card()  # состояние, с которого начинает новая карточка
    with srs.session() as conn:
        recast = missing = hinted = 0
        for old_front, (front, back, example) in RECOGNITION.items():
            row = conn.execute("SELECT id FROM cards WHERE tutor = 'english' AND front = ?",
                               (old_front,)).fetchone()
            if row is None:
                print(f"  нет в базе: {old_front[:50]}…")
                missing += 1
                continue
            print(f"  [{row['id']}] → {front}")
            if not args.dry_run:
                conn.execute(
                    "UPDATE cards SET kind = 'recognition', front = ?, back = ?, "
                    "example = ?, hint = '', introduced = NULL, last_review = NULL, "
                    "state = ?, step = ?, stability = ?, difficulty = ?, due = ? "
                    "WHERE id = ?",
                    (front, back, example, fresh.state.value, fresh.step,
                     fresh.stability, fresh.difficulty, fresh.due.isoformat(), row["id"]))
                conn.execute("DELETE FROM review_log WHERE card_id = ?", (row["id"],))
            recast += 1

        for front, hint in HINTS.items():
            if args.dry_run:
                continue
            hinted += conn.execute("UPDATE cards SET hint = ? WHERE tutor = 'english' "
                                   "AND front = ?", (hint, front)).rowcount

        # Оставшиеся cloze — его собственные ошибки за 11–13 августа. Они уже
        # разобраны в вечерних сообщениях: урок им не нужен, только повторение.
        seen = 0
        if not args.dry_run:
            seen = conn.execute(
                "UPDATE cards SET introduced = created, due = created "
                "WHERE tutor = 'english' AND kind = 'cloze' AND introduced IS NULL"
            ).rowcount

        print(f"\nв recognition: {recast}, не найдено: {missing}, "
              f"подсказок: {hinted}, ошибок сразу в расписание: {seen}")
        if not args.dry_run:
            print(srs.stats(conn, "english"))


if __name__ == "__main__":
    main()
