from __future__ import annotations

import json
import re

from openai import OpenAI

from app.config import get_settings
from app.models import ConversationMessage
from app.services.search_core.types import QueryPlan

settings = get_settings()

STOP_WORDS = {
    "그리고",
    "그러나",
    "그래서",
    "관련",
    "대한",
    "대해",
    "무엇",
    "누구",
    "얼마",
    "어디",
    "언제",
    "어떻게",
    "알려줘",
    "인가요",
    "이야",
    "그럼",
    "해당",
    "the",
    "and",
    "for",
    "from",
    "what",
    "who",
    "how",
}
FOLLOW_UP_MARKERS = (
    "그럼",
    "그러면",
    "그건",
    "그게",
    "그 담당",
    "해당",
    "그 프로젝트",
    "그 협업",
    "이어서",
)


def _keywords(text: str) -> list[str]:
    values: list[str] = []
    for token in re.findall(r"[가-힣]{2,}|[a-zA-Z][a-zA-Z0-9._-]{1,}", text.lower()):
        if token not in STOP_WORDS and token not in values:
            values.append(token)
    return values


def _requested_field(question: str) -> str:
    lowered = question.lower()
    if any(value in lowered for value in ("담당자", "연락처", "contact", "누구")):
        return "contact"
    if any(value in lowered for value in ("비용", "협업비", "예산", "금액", "가격", "fee", "rate")):
        return "money"
    if any(value in lowered for value in ("일정", "마감", "언제", "날짜", "schedule", "deadline")):
        return "date"
    if any(
        value in lowered
        for value in (
            "승인",
            "확정",
            "상태",
            "결정",
            "진행",
            "완료",
            "보류",
            "취소",
            "수정",
            "현재 단계",
            "다음 행동",
            "어떻게 됐",
            "어떻게 되었",
        )
    ):
        return "status"
    return "general"


def _is_follow_up(question: str) -> bool:
    stripped = question.strip()
    return stripped.startswith(FOLLOW_UP_MARKERS) or (
        len(stripped) <= 34
        and any(term in stripped for term in ("그", "비용", "담당", "일정", "상태"))
    )


def _fallback_plan(
    question: str, history: list[ConversationMessage]
) -> QueryPlan:
    prior_user = next(
        (message for message in reversed(history) if message.role == "user"), None
    )
    prior_basis = ""
    if prior_user:
        prior_basis = prior_user.resolved_question or prior_user.content

    resolved = question.strip()
    if prior_basis and _is_follow_up(question):
        resolved = f"{prior_basis} / 후속 질문: {question.strip()}"

    current_terms = _keywords(question)
    resolved_terms = _keywords(resolved)
    entity_terms = [
        term
        for term in resolved_terms
        if re.search(r"[a-z0-9]", term) or term not in current_terms
    ]
    if not entity_terms:
        entity_terms = resolved_terms[:6]

    queries = [resolved]
    if question.strip() != resolved:
        queries.append(question.strip())
    if entity_terms:
        field_hint = {
            "contact": "담당자 연락처",
            "money": "협업비 비용 예산 제안 금액",
            "date": "일정 날짜 마감",
            "status": "현재 상태 진행 완료 수정 요청 승인 보류 취소 최신",
        }.get(_requested_field(question), "")
        queries.append(" ".join([*entity_terms[:6], field_hint]).strip())

    return QueryPlan(
        original_question=question.strip(),
        resolved_question=resolved,
        search_queries=list(dict.fromkeys(query for query in queries if query)),
        entity_terms=entity_terms[:10],
        requested_field=_requested_field(question),
    )


def _llm_plan(
    question: str, history: list[ConversationMessage]
) -> QueryPlan | None:
    if not settings.openai_api_key:
        return None
    recent = [
        {
            "role": message.role,
            "content": message.content[-1200:],
            "resolved_question": message.resolved_question,
        }
        for message in history[-settings.search_history_messages :]
    ]
    schema = {
        "type": "object",
        "properties": {
            "resolved_question": {"type": "string"},
            "search_queries": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": 5,
            },
            "entity_terms": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 10,
            },
            "requested_field": {
                "type": "string",
                "enum": ["contact", "money", "date", "status", "general"],
            },
        },
        "required": [
            "resolved_question",
            "search_queries",
            "entity_terms",
            "requested_field",
        ],
        "additionalProperties": False,
    }
    try:
        response = OpenAI(api_key=settings.openai_api_key).responses.create(
            model=settings.openai_chat_model,
            reasoning={"effort": "none"},
            store=False,
            instructions=(
                "당신은 팀 지식 검색 쿼리 설계기다. 대화 기록은 검색 문맥일 뿐 사실 근거가 아니다. "
                "현재 질문의 생략된 대상만 복원해 독립 질문으로 만들고, 같은 사건의 반대·최신 근거도 "
                "찾을 수 있는 짧은 검색 쿼리를 작성한다. 기록에 없는 사실은 추가하지 않는다."
            ),
            input=json.dumps(
                {"history": recent, "current_question": question}, ensure_ascii=False
            ),
            text={
                "format": {
                    "type": "json_schema",
                    "name": "project_brain_query_plan",
                    "schema": schema,
                    "strict": True,
                }
            },
        )
        payload = json.loads(response.output_text)
        return QueryPlan(
            original_question=question.strip(),
            resolved_question=payload["resolved_question"].strip(),
            search_queries=list(
                dict.fromkeys(
                    [
                        payload["resolved_question"].strip(),
                        *[
                            item.strip()
                            for item in payload["search_queries"]
                            if item.strip()
                        ],
                    ]
                )
            ),
            entity_terms=[
                item.strip().lower()
                for item in payload["entity_terms"]
                if item.strip()
            ],
            requested_field=payload["requested_field"],
        )
    except Exception:
        return None


def plan_queries(
    question: str, history: list[ConversationMessage]
) -> QueryPlan:
    return _llm_plan(question, history) or _fallback_plan(question, history)
