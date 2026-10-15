from __future__ import annotations

import json
import re
from calendar import monthrange
from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

from app.config import get_settings
from app.models import ConversationMessage
from app.services.openai_client import get_openai_client
from app.services.search_core.conversation import assistant_identity_answer
from app.services.search_core.collection_contract import video_operation
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
    "그 메일",
    "이 메일",
    "위 메일",
    "앞선 메일",
    "그 제안",
    "이 제안",
    "앞선 제안",
    "해당",
    "그 프로젝트",
    "그 협업",
    "이어서",
)
ELLIPTICAL_FOLLOW_UP_PATTERN = re.compile(
    r"^(?:비용|협업비|금액|담당자|연락처|일정|상태)"
    r"(?:은|는|이|가)?(?:\s*(?:뭐야|누구야|얼마야|어때|어떻게 됐어))?[?.!]*$"
)
COMPOSE_MARKERS = (
    "작성해",
    "작성해줘",
    "작성해 줘",
    "초안",
    "템플릿",
    "만들어줘",
    "만들어 줘",
    "써줘",
    "써 줘",
    "draft",
    "template",
)
ENTITY_STOP_WORDS = {
    "ai",
    "youtube",
    "유튜브",
    "유료",
    "무료",
    "협업",
    "제안",
    "메일",
    "이메일",
    "답장",
    "회신",
    "파트너십",
    "프로젝트",
    "draft",
    "template",
}
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
    return stripped.startswith(FOLLOW_UP_MARKERS) or bool(
        ELLIPTICAL_FOLLOW_UP_PATTERN.fullmatch(stripped)
    )


def _conversation_basis(message: ConversationMessage) -> str:
    basis = message.resolved_question or message.content
    return basis.split(" / 후속 질문: ", 1)[0].strip()


def _task_mode(question: str) -> str:
    lowered = question.lower()
    return (
        "compose"
        if any(marker in lowered for marker in COMPOSE_MARKERS)
        else "answer"
    )


def _collaboration_collection_question(question: str) -> bool:
    compact = re.sub(r"\s+", "", question.lower())
    return "협업제안" in compact or (
        "협업" in compact
        and any(marker in compact for marker in ("메일", "이메일", "제안"))
    )


def _explicit_entity_terms(question: str) -> list[str]:
    candidates: list[str] = []
    patterns = (
        r"([A-Za-z][A-Za-z0-9._-]{2,}|[가-힣][가-힣0-9._-]{1,})\s*(?:와|과|의)?\s*(?:협업|제안)",
        r"\b([A-Za-z][A-Za-z0-9._-]{2,})\b(?=.{0,24}(?:협업|제안|메일|이메일))",
        r"\b([A-Za-z][A-Za-z0-9._-]{2,})\.(?:ai|com|io|co)\b",
        r"([A-Za-z][A-Za-z0-9._-]{2,})\s*(?:메일|이메일|답장|회신)",
    )
    for pattern in patterns:
        for value in re.findall(pattern, question):
            cleaned = value.strip(" ._- ").lower()
            if (
                cleaned
                and cleaned not in ENTITY_STOP_WORDS
                and cleaned not in STOP_WORDS
                and cleaned not in candidates
            ):
                candidates.append(cleaned)
    return candidates[:5]


def _apply_request_contract(
    plan: QueryPlan,
    question: str,
    history: list[ConversationMessage],
) -> QueryPlan:
    task_mode = _task_mode(question)
    explicit_entities = _explicit_entity_terms(question)
    prior_user = next(
        (message for message in reversed(history) if message.role == "user"), None
    )
    inherits_scope = bool(
        prior_user
        and not explicit_entities
        and _is_follow_up(question)
    )

    if explicit_entities:
        scope_mode = "entity"
        locked_entities = explicit_entities
    elif inherits_scope:
        scope_mode = "inherited"
        prior_basis = _conversation_basis(prior_user)
        locked_entities = _explicit_entity_terms(prior_basis)
    elif task_mode == "compose" and _collaboration_collection_question(question):
        scope_mode = "collection"
        locked_entities = []
    else:
        scope_mode = "open"
        locked_entities = []

    # 회사명이 없는 협업 템플릿 요청은 특정 브랜드를 임의 선택하지 않고,
    # 동일한 고정 쿼리로 관련 제안들의 공통 패턴을 수집합니다.
    if task_mode == "compose" and scope_mode == "collection":
        return replace(
            plan,
            task_mode=task_mode,
            scope_mode=scope_mode,
            locked_entity_terms=locked_entities,
            is_state_query=False,
            cardinality="all",
            entity_terms=["협업", "제안", "메일", "파트너십"],
            search_queries=[
                question.strip(),
                "협업 제안 메일",
                "파트너십 제안 이메일",
                "협업 제안 회신 조건 보상 일정 산출물 권한",
            ],
            retrieval_mode="hybrid",
            record_type=None,
        )
    return replace(
        plan,
        task_mode=task_mode,
        scope_mode=scope_mode,
        locked_entity_terms=locked_entities,
        is_state_query=False if task_mode == "compose" else plan.is_state_query,
    )


def _plain_dated_video_list(question: str) -> bool:
    """Keep the established plain monthly-list fast path, not topic filters."""
    allowed = {"크랩", "유튜브", "youtube", "crab", "영상", "영상의", "제목", "제목을", "제목과",
               "업로드", "시간", "시간을", "포함", "포함해서", "리스트", "리스트로",
               "목록", "목록을", "전체", "전부", "모두", "제시해줘", "알려줘", "채널", "함께",
               "채널의", "올해", "작년", "년", "월", "해당", "그럼", "몇", "개야",
               "개", "편", "편이야", "개수", "개수는", "몇편", "몇개", "영상은",
               "월에", "발행된", "시간과"}
    return video_operation(question) in {"list", "count"} and all(
        term in allowed for term in _keywords(question)
    )


def _can_use_deterministic_plan(question: str) -> bool:
    task_mode = _task_mode(question)
    explicit_entities = _explicit_entity_terms(question)
    if (
        task_mode == "answer"
        and explicit_entities
        and _requested_field(question) in {"contact", "money"}
    ):
        return True
    if (
        task_mode == "compose"
        and _collaboration_collection_question(question)
        and not explicit_entities
    ):
        return True

    time_start, _ = _time_range(question)
    return bool(
        time_start is not None
        and _cardinality(question, time_start) == "all"
        and _record_type(question) is not None
        and _plain_dated_video_list(question)
    )


def extend_plan_for_retry(plan: QueryPlan, retry_queries: list[str]) -> QueryPlan:
    if plan.task_mode != "compose":
        return replace(
            plan,
            search_queries=list(
                dict.fromkeys([*plan.search_queries, *retry_queries])
            ),
        )

    # 작성형 전체 종합은 이미 여러 문서를 모으므로 모델이 제안한 특정 회사명으로
    # 재검색하지 않습니다. 특정 대상 작성은 최초에 잠근 대상만 사용합니다.
    if plan.scope_mode == "collection" or not plan.locked_entity_terms:
        return plan
    locked_query = " ".join(
        [
            *plan.locked_entity_terms,
            *plan.requested_fields[:4],
            "최신 원문 조건",
        ]
    ).strip()
    if locked_query:
        return replace(
            plan,
            search_queries=list(
                dict.fromkeys([*plan.search_queries, locked_query])
            ),
        )
    return plan


def _fallback_plan(
    question: str, history: list[ConversationMessage]
) -> QueryPlan:
    prior_user = next(
        (message for message in reversed(history) if message.role == "user"), None
    )
    prior_basis = ""
    if prior_user:
        prior_basis = _conversation_basis(prior_user)

    resolved = question.strip()
    current_entities = _explicit_entity_terms(question)
    if prior_basis and _is_follow_up(question) and not current_entities:
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
        collection_filters_supported=(
            record_type == "video" and time_start is not None
            and _plain_dated_video_list(resolved.replace(" / 후속 질문: ", " "))
        ),
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
            "collection_title_terms": {
                "type": "array", "items": {"type": "string"}, "maxItems": 5,
            },
            "collection_filters_supported": {"type": "boolean"},
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
            "collection_title_terms",
            "collection_filters_supported",
        ],
        "additionalProperties": False,
    }
    try:
        client = get_openai_client()
        if client is None:
            return None
        response = client.responses.create(
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
                " 영상 개수·목록 질문의 collection_title_terms에는 제목에 반드시 포함되어야 하는 "
                "명시적인 분류·주제만 넣는다(예: 실전편). 채널명, 영상, 날짜는 넣지 않는다. "
                "모든 조건을 제목 부분문자열 AND와 기간으로 정확히 적용할 수 있을 때만 "
                "collection_filters_supported=true로 한다. 내용의 의미 분석, 공개 완료 여부, "
                "동의어 OR 조건이 필요하면 false로 한다. 내용 설명 질문도 false다. "
                "현재 질문에 없는 이전 질문의 분류·개수·목록 요구를 계승하지 않는다."
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
            collection_title_terms=[
                term.strip() for term in payload.get("collection_title_terms", []) if term.strip()
            ],
            collection_filters_supported=bool(
                payload.get("collection_filters_supported", False)
            ),
        )
    except Exception:
        return None


def plan_queries(
    question: str, history: list[ConversationMessage]
) -> QueryPlan:
    if assistant_identity_answer(question):
        return QueryPlan(original_question=question.strip(),
                         resolved_question=question.strip(), search_queries=[])
    if _can_use_deterministic_plan(question):
        plan = _fallback_plan(question, history)
    else:
        plan = _llm_plan(question, history) or _fallback_plan(question, history)
    plan = _apply_request_contract(plan, question, history)
    if plan.record_type == "video":
        operation = video_operation(question)
        if operation == "describe" or not plan.collection_filters_supported:
            # Multiple relevant records do not imply an unfiltered full-list answer.
            plan = replace(
                plan, retrieval_mode="hybrid",
                cardinality="many" if plan.cardinality == "all" else plan.cardinality,
            )
        elif operation in {"count", "list"}:
            plan = replace(plan, retrieval_mode="scoped", cardinality="all")
    return plan
