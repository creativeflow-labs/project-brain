from __future__ import annotations

import json
import re
from calendar import monthrange
from datetime import datetime
from zoneinfo import ZoneInfo

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
SEOUL = ZoneInfo("Asia/Seoul")


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
    if any(
        value in lowered
        for value in (
            "일정",
            "마감",
            "언제",
            "날짜",
            "시간",
            "schedule",
            "deadline",
        )
    ):
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


def _is_state_query(question: str) -> bool:
    lowered = question.lower()
    if any(value in lowered for value in ("왜", "이유", "원인", "까였", "거절된 이유")):
        return False
    return any(
        value in lowered
        for value in (
            "현재 상태",
            "진행 상태",
            "지금 상태",
            "현재 단계",
            "어디까지 진행",
            "완료됐",
            "완료되었",
            "보류됐",
            "취소됐",
            "다음 행동",
        )
    )


def _requested_fields(question: str) -> list[str]:
    lowered = question.lower()
    fields: list[str] = []
    mappings = (
        ("title", ("제목", "title")),
        ("published_at", ("업로드 시간", "업로드 시각", "발행 시간", "발행일")),
        ("contact", ("담당자", "연락처", "contact")),
        ("money", ("비용", "협업비", "금액", "가격", "fee", "rate")),
        ("status", ("상태", "진행", "완료", "보류", "취소")),
        ("reason", ("왜", "이유", "원인", "까였", "거절")),
    )
    for field_name, markers in mappings:
        if any(marker in lowered for marker in markers):
            fields.append(field_name)
    return fields


def _time_field(question: str) -> str | None:
    lowered = question.lower()
    if any(value in lowered for value in ("업로드", "발행", "게시")):
        return "published_at"
    if any(value in lowered for value in ("수신", "받은 메일", "받았")):
        return "received_at"
    if any(value in lowered for value in ("발신", "보낸 메일", "보냈")):
        return "sent_at"
    if any(value in lowered for value in ("수정일", "업데이트", "갱신")):
        return "updated_at"
    return "occurred_at" if _requested_field(question) == "date" else None


def _time_range(question: str) -> tuple[datetime | None, datetime | None]:
    now = datetime.now(SEOUL)
    lowered = question.lower()
    year_month = re.search(
        r"(?:(?P<year>20\d{2})\s*[년./-]\s*)?(?P<month>1[0-2]|0?[1-9])\s*월",
        lowered,
    )
    if year_month:
        year = int(year_month.group("year") or now.year)
        month = int(year_month.group("month"))
    elif "이번 달" in lowered or "이번달" in lowered:
        year, month = now.year, now.month
    elif "지난 달" in lowered or "지난달" in lowered:
        year = now.year if now.month > 1 else now.year - 1
        month = now.month - 1 if now.month > 1 else 12
    else:
        return None, None
    start = datetime(year, month, 1, tzinfo=SEOUL)
    end = datetime(
        year,
        month,
        monthrange(year, month)[1],
        23,
        59,
        59,
        999999,
        tzinfo=SEOUL,
    )
    return start, end


def _cardinality(question: str, time_start: datetime | None) -> str:
    lowered = question.lower()
    if any(
        value in lowered
        for value in (
            "전부",
            "전체",
            "모두",
            "목록",
            "리스트",
            "각각",
            "몇 건",
            "몇 개",
        )
    ):
        return "all"
    if time_start is not None and any(
        value in lowered for value in ("제목", "영상", "메일", "제안", "업무")
    ):
        return "all"
    return "one"


def _record_type(question: str) -> str | None:
    lowered = question.lower()
    if any(value in lowered for value in ("유튜브", "youtube", "영상")):
        return "video"
    return None


def _retrieval_mode(cardinality: str, record_type: str | None) -> str:
    return "scoped" if cardinality == "all" and record_type else "hybrid"


def _parse_plan_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=SEOUL) if parsed.tzinfo is None else parsed


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

    time_start, time_end = _time_range(resolved)
    cardinality = _cardinality(resolved, time_start)
    record_type = _record_type(resolved)
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
    if time_start is not None:
        queries.append(
            " ".join(
                [
                    *entity_terms[:6],
                    f"{time_start.year}년 {time_start.month}월",
                    *(_requested_fields(question)[:4]),
                ]
            ).strip()
        )

    return QueryPlan(
        original_question=question.strip(),
        resolved_question=resolved,
        search_queries=list(dict.fromkeys(query for query in queries if query)),
        entity_terms=entity_terms[:10],
        requested_field=_requested_field(question),
        requested_fields=_requested_fields(question),
        cardinality=cardinality,
        time_start=time_start,
        time_end=time_end,
        time_field=_time_field(question),
        is_state_query=_is_state_query(question),
        retrieval_mode=_retrieval_mode(cardinality, record_type),
        record_type=record_type,
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
            "requested_fields": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 8,
            },
            "cardinality": {
                "type": "string",
                "enum": ["one", "many", "all"],
            },
            "time_start": {"type": ["string", "null"]},
            "time_end": {"type": ["string", "null"]},
            "time_field": {
                "type": ["string", "null"],
                "enum": [
                    "published_at",
                    "received_at",
                    "sent_at",
                    "updated_at",
                    "occurred_at",
                    None,
                ],
            },
            "is_state_query": {"type": "boolean"},
        },
        "required": [
            "resolved_question",
            "search_queries",
            "entity_terms",
            "requested_field",
            "requested_fields",
            "cardinality",
            "time_start",
            "time_end",
            "time_field",
            "is_state_query",
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
                "찾을 수 있는 짧은 검색 쿼리를 작성한다. 사용자가 요구한 항목과 기간, 한 건인지 전체 "
                "목록인지 보존한다. is_state_query는 현재 진행 상태나 현재 단계를 직접 묻는 경우에만 "
                "참으로 지정하고, 거절 이유·원인·설명 질문에는 거짓으로 지정한다. 기록에 없는 사실은 "
                "추가하지 않는다."
            ),
            input=json.dumps(
                {
                    "history": recent,
                    "current_question": question,
                    "current_datetime": datetime.now(SEOUL).isoformat(),
                },
                ensure_ascii=False,
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
        deterministic_start, deterministic_end = _time_range(
            payload["resolved_question"]
        )
        deterministic_cardinality = _cardinality(
            payload["resolved_question"], deterministic_start
        )
        has_cause_marker = any(
            marker in question.lower()
            for marker in ("왜", "이유", "원인", "까였", "거절된 이유")
        )
        record_type = _record_type(payload["resolved_question"])
        final_cardinality = (
            "all"
            if deterministic_cardinality == "all"
            else payload["cardinality"]
        )
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
            requested_fields=[
                item.strip() for item in payload["requested_fields"] if item.strip()
            ],
            cardinality=final_cardinality,
            time_start=(
                deterministic_start or _parse_plan_datetime(payload["time_start"])
            ),
            time_end=deterministic_end or _parse_plan_datetime(payload["time_end"]),
            time_field=_time_field(question) or payload["time_field"],
            is_state_query=bool(payload["is_state_query"] and not has_cause_marker),
            retrieval_mode=_retrieval_mode(final_cardinality, record_type),
            record_type=record_type,
        )
    except Exception:
        return None


def plan_queries(
    question: str, history: list[ConversationMessage]
) -> QueryPlan:
    return _llm_plan(question, history) or _fallback_plan(question, history)
