"""Тул `add_card`: агент заводит карточку, дальше ей владеет код.

Ревью живёт в отдельном карточном боте (`python -m tutor.cards_bot`) — поток
карточек не должен засорять диалог с тьютором, а десятки тапов в день не могут
быть SDK-сессиями.
"""

from typing import Annotated, Any

from tutor import srs, tts
from tutor.config import Settings
from tutor.mcp_tools import create_mcp_server, tool

SERVER_NAME = "srs"
TOOL_NAMES = [f"mcp__{SERVER_NAME}__{name}" for name in ("add_card", "srs_stats")]


def _flag(value: Any) -> bool:
    """Булев аргумент тула: моделью он может прийти и строкой — `bool("false")`
    истинно, и карточка молча проскочила бы урок."""
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes", "1")
    return bool(value)


def make_server(settings: Settings):
    @tool(
        "add_card",
        "Завести карточку в интервальное повторение (FSRS). Новая карточка "
        "сначала показывается в карточном боте уроком — лицо и оборот сразу, — "
        "и только потом начинает спрашиваться, так что заводить можно и то, "
        "чего он ещё не знает. Повторный вызов с тем же front не плодит дубль "
        "— расписание уже заведённой карточки не сбивается",
        {
            "kind": Annotated[str, "Тип карточки — то, о чём она спрашивает. "
                                   "`recognition` (по умолчанию): лицо — само слово "
                                   "или фраза (`look up to`), вопрос «что это значит». "
                                   "Так входит КАЖДОЕ новое слово. "
                                   "`cloze`: лицо — предложение с пропуском, вопрос "
                                   "о форме, предлоге или коллокации. Только для того, "
                                   "что он уже видел: своей ошибки или уже заведённого "
                                   "слова. `production`: лицо — значение и ситуация, "
                                   "вопрос «как это сказать». Для активного словаря, "
                                   "когда узнавание уже уверенное"],
            "front": Annotated[str, "Лицо. recognition — слово или фраза; cloze — "
                                    "предложение с пропуском (`I ___ (go) to school`), "
                                    "ОДИН пропуск на карточку; production — значение "
                                    "по-русски с ситуацией"],
            "back": Annotated[str, "Оборот: значение или правильный ответ, одна строка. "
                                   "Для фразовых глаголов добавь, из чего складывается "
                                   "смысл («буквально „смотреть вверх на“ → уважать») "
                                   "— разобранная идиома запоминается, списочная нет"],
            "example": Annotated[str, "Живое предложение с этим словом — не то же, "
                                      "что на лице. Видно на уроке и на обороте, "
                                      "и оно же озвучивается. Слово, которое "
                                      "синтезатор прочтёт неверно — техжаргон "
                                      "(Kubernetes, CUDA, nginx), имя, омограф "
                                      "(lead, read, live), — помечай "
                                      "транскрипцией: `on [Kubernetes]"
                                      "(/ˌkuːbərˈnɛtɪs/) today`. На экране "
                                      "разметки не видно. Обычные английские "
                                      "слова не помечай: их он читает верно"],
            "hint": Annotated[str, "Подсказка по кнопке. У cloze обязательна: "
                                   "перевод пропущенного, первая буква, часть речи. "
                                   "У recognition — НЕ значение слова: подсказка, "
                                   "называющая ответ, превращает кнопку Hint в "
                                   "кнопку Show answer. Здесь она только сужает "
                                   "поиск («это про чувство, не про действие») "
                                   "или отсекает похожее («не ignore»). Нечего "
                                   "сузить — оставь пустым"],
            "context": Annotated[str, "Где встретилось: лист и задание, своя ошибка, "
                                      "серия — 1 строка"],
            "tags": Annotated[str, "Метки через запятую: юнит, тема, тип ошибки; "
                                   "для целевого словаря — `goal` и домен "
                                   "(`interview`, `reading`, `everyday`)"],
            "priority": Annotated[bool, "Показать раньше очереди. Для целевого "
                                        "словаря — того, что нужно ему на "
                                        "собеседовании и в статьях: такое слово "
                                        "не должно неделю ждать за лексикой "
                                        "первого юнита. Слово из учебника — нет"],
            "already_seen": Annotated[bool, "Пропустить урок и сразу поставить "
                                            "в расписание. Только для его "
                                            "СОБСТВЕННОЙ ошибки, разобранной "
                                            "сегодня: он уже видел её у себя в "
                                            "тексте. Слово с листа сюда не "
                                            "относится — лист он мог не дочитать"],
        },
    )
    async def add_card(args: dict[str, Any]) -> dict[str, Any]:
        try:
            with srs.session() as conn:
                card_id, created = srs.add_card(
                    conn, settings.tutor, args["front"], args["back"],
                    kind=args.get("kind") or "recognition",
                    example=args.get("example", ""), hint=args.get("hint", ""),
                    context=args.get("context", ""), tags=args.get("tags", ""),
                    priority=int(_flag(args.get("priority"))),
                    introduced=_flag(args.get("already_seen")))
        except Exception as e:
            return {"content": [{"type": "text", "text": f"ERROR: {e}"}], "is_error": True}
        verdict = "создана" if created else "уже была, оставил как есть"
        # озвучка не критична: нет модели или сорвался синтез — карточка живёт
        # молча, а не падает вместе с тулом
        voiced = tts.speak_card(card_id, args.get("example", ""), settings.tts_voice)
        note = ", озвучен пример" if voiced else ""
        return {"content": [{"type": "text",
                             "text": f"card_id={card_id} ({verdict}){note}"}]}

    @tool("srs_stats", "Сводка по карточкам этого тьютора: всего, ждут первого "
                       "показа, к повторению сейчас, показано и повторено сегодня", {})
    async def srs_stats(_: dict[str, Any]) -> dict[str, Any]:
        try:
            with srs.session() as conn:
                data = srs.stats(conn, settings.tutor)
        except Exception as e:
            return {"content": [{"type": "text", "text": f"ERROR: {e}"}], "is_error": True}
        return {"content": [{"type": "text", "text":
                             f"карточек всего: {data['total']}, к повторению: {data['due']}, "
                             f"ждут знакомства: {data['waiting']}, "
                             f"выучено сегодня: {data['learned_today']}, "
                             f"повторено сегодня: {data['reviewed_today']}"}]}

    return create_mcp_server(SERVER_NAME, tools=[add_card, srs_stats])
