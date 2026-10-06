"""Google Forms: квизы с автогрейдом (ADR-3: грейдящееся — в Forms).

**Создание отцеплено от сессий 2026-08-15** (ADR-15): у тьютора остался один вид
артефакта — Doc, конспект и ДЗ отдельными документами. В сессию едет только
`make_read_tools` (старые формы июля надо уметь перечитывать). Вернуть автогрейд
= вписать `create_form_quiz` обратно в `GOOGLE_TOOL_NAMES` и позвать `make_tools`
в harness.py.
"""

from typing import Annotated, Any

from googleapiclient.errors import HttpError

from tutor.config import Settings
from tutor.mcp_tools import tool
from tutor.tools.google_auth import build_service, call_tool, gexec

QUESTIONS_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"enum": ["MCQ", "text"], "description": "MCQ — автогрейд; text — развёрнутый ответ"},
                    "question": {"type": "string"},
                    "options": {"type": "array", "items": {"type": "string"}, "description": "только для MCQ"},
                    "correct_idx": {"type": "integer", "description": "индекс правильного варианта, только для MCQ"},
                    "points": {"type": "integer", "default": 1},
                    "feedback": {"type": "string", "description": "пояснение при неверном ответе"},
                },
                "required": ["type", "question"],
            },
        },
    },
    "required": ["title", "questions"],
}


def _question_to_request(q: dict[str, Any], index: int) -> dict[str, Any]:
    if q["type"] == "MCQ":
        grading: dict[str, Any] = {
            "pointValue": q.get("points", 1),
            "correctAnswers": {"answers": [{"value": q["options"][q["correct_idx"]]}]},
        }
        if q.get("feedback"):
            grading["whenWrong"] = {"text": q["feedback"]}
        question: dict[str, Any] = {
            "required": True,
            "grading": grading,
            # shuffle: Forms сам перемешивает варианты (грейдит по значению,
            # не по позиции) — модель систематически ставит верный ответ первым
            "choiceQuestion": {"type": "RADIO", "shuffle": True,
                               "options": [{"value": o} for o in q["options"]]},
        }
    else:  # text: автогрейда нет, очки выставляются при проверке
        question = {
            "required": True,
            "grading": {"pointValue": q.get("points", 1)},
            "textQuestion": {"paragraph": True},
        }
    return {"createItem": {
        "item": {"title": q["question"], "questionItem": {"question": question}},
        "location": {"index": index},
    }}


def create_form_quiz(title: str, questions: list[dict[str, Any]]) -> dict[str, str]:
    forms = build_service("forms", "v1")
    form = gexec(forms.forms().create(body={"info": {"title": title}}))
    requests = [{"updateSettings": {
        "settings": {"quizSettings": {"isQuiz": True}},
        "updateMask": "quizSettings.isQuiz",
    }}]
    requests += [_question_to_request(q, i) for i, q in enumerate(questions)]
    gexec(forms.forms().batchUpdate(formId=form["formId"], body={"requests": requests}))
    return {"form_id": form["formId"], "url": form["responderUri"],
            "note": "в память сохраняй form_id; токен из url (/d/e/...) для чтения ответов не подходит"}


def read_form_responses(form_id: str) -> list[dict[str, Any]]:
    if form_id.startswith("1FAIpQLS"):
        raise ValueError(
            f"'{form_id}' — токен публичной ссылки (/forms/d/e/...), а не form_id; "
            "возьми form_id из результата create_form_quiz")
    forms = build_service("forms", "v1")
    try:
        form = gexec(forms.forms().get(formId=form_id))
    except HttpError as e:
        if e.resp.status == 404:
            raise ValueError(
                f"форма '{form_id}' не найдена (404) — это точно form_id из "
                "create_form_quiz, а не id документа или токен из ссылки?") from e
        raise
    titles = {}  # questionId → текст вопроса, чтобы ответы читались человеком
    for item in form.get("items", []):
        question = item.get("questionItem", {}).get("question")
        if question:
            titles[question["questionId"]] = item.get("title", "")

    responses = gexec(forms.forms().responses().list(formId=form_id))
    out = []
    for r in responses.get("responses", []):
        answers = {}
        for qid, ans in r.get("answers", {}).items():
            values = [a.get("value", "") for a in ans.get("textAnswers", {}).get("answers", [])]
            answers[titles.get(qid, qid)] = "; ".join(values)
        out.append({"answers": answers, "score": r.get("totalScore"),
                    "submitted_at": r.get("lastSubmittedTime")})
    return out


def make_read_tools(settings: Settings) -> list:
    """Только чтение. Создание форм отцеплено 2026-08-15 (ADR-15): артефакт
    теперь один — Doc. Старые формы (placement, глубокая диагностика 07-14)
    остаются читаемыми, их сырые ответы нужны replan."""

    @tool("read_form_responses",
          "Прочитать ответы на старую Google Form (ответы, балл, время сдачи)",
          {"form_id": Annotated[str, "id формы из progress.md"]})
    async def _read(args: dict[str, Any]) -> dict[str, Any]:
        return await call_tool(read_form_responses, form_id=args["form_id"])

    return [_read]


def make_tools(settings: Settings) -> list:
    """Полный набор — в сессии не подключён, см. make_read_tools."""

    @tool("create_form_quiz",
          "Создать Google Form квиз с автогрейдом; вернёт ссылку для ученика",
          QUESTIONS_SCHEMA)
    async def _create(args: dict[str, Any]) -> dict[str, Any]:
        return await call_tool(create_form_quiz,
                               title=args["title"], questions=args["questions"])

    return [_create, *make_read_tools(settings)]
