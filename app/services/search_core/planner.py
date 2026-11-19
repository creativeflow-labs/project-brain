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
from app.services.search_core.types import QueryPlan, ScopeMode

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
SCOPE_QUANTIFIER = r"(?:전체|전부|모든|모두|전반|all|every|across)"
SCOPE_PREFIX = re.compile(r"^(?:(?:그럼|그러면|이번에는|이번엔|이제|이어서)[\s,]+)+")
SCOPE_REFERENCE = re.compile(r"^(?:그|이|저|해당|직전|이전|앞선|같은)(?:\s|$)")
SCOPE_IDENTIFIER = re.compile(
    r"^(?:[“\"‘']([^”\"’']+)[”\"’']|([A-Za-z][A-Za-z0-9._-]{2,}))"
    r"(?=\s|[의와과에은는이가는부터까지]|$)"
)
SCOPE_POSSESSIVE = re.compile(r"^(.{2,60}?)의\s+")
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


def _parse_plan_datetime(value: object, *, end_of_day: bool = False) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if end_of_day and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()):
        parsed = parsed.replace(hour=23, minute=59, second=59, microsecond=999999)
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


def _surface_scope(question: str) -> tuple[ScopeMode, list[str]]:
    """Conservative signals from the current wording, independent of data source."""
    text = SCOPE_PREFIX.sub("", question.strip()).strip()
    if not text or SCOPE_REFERENCE.match(text):
        return "open", []
    # A leading quantifier without a named qualifier replaces the previous
    # target. A qualifier before the quantifier keeps the range within it.
    if re.match(rf"^{SCOPE_QUANTIFIER}(?=\s|[를을는가만도]|[?.!]|$)", text, re.I):
        return "collection", []
    if match := SCOPE_IDENTIFIER.match(text):
        term = (match[1] or match[2]).strip().rstrip(".").lower()
        if term and not re.fullmatch(SCOPE_QUANTIFIER, term, re.I):
            return "entity", [term]
    if match := SCOPE_POSSESSIVE.match(text):
        term = match[1].strip().lower()
        if not re.search(r"[,!?/]", term):
            return "entity", [term]
    return "open", []


def _apply_request_contract(
    plan: QueryPlan,
    question: str,
) -> QueryPlan:
    task_mode = _task_mode(question)
    scope_mode, locked_entities = _surface_scope(question)
    # The independent interpretation can recognize a qualifier elsewhere in
    # the sentence, e.g. "전체 자료 중 …만". Only a named, anchored term may
    # narrow a leading broad scope.
    if scope_mode == "open" or (
        scope_mode == "collection"
        and plan.scope_mode == "entity"
        and plan.locked_entity_terms
    ):
        scope_mode, locked_entities = plan.scope_mode, plan.locked_entity_terms
    return replace(
        plan,
        task_mode=task_mode,
        scope_mode=scope_mode,
        locked_entity_terms=locked_entities,
        cardinality="all" if scope_mode == "collection" else plan.cardinality,
        is_state_query=False if task_mode == "compose" else plan.is_state_query,
    )


def _can_use_deterministic_plan(question: str) -> bool:
    task_mode = _task_mode(question)
    scope_mode, _ = _surface_scope(question)
    return (
        task_mode == "answer"
        and scope_mode == "entity"
        and _requested_field(question) in {"contact", "money"}
    )


def extend_plan_for_retry(plan: QueryPlan, retry_queries: list[str]) -> QueryPlan:
    if plan.task_mode != "compose":
        return replace(
            plan,
            search_queries=list(
                dict.fromkeys([*plan.search_queries, *retry_queries])
            ),
        )

    # A retry may not substitute an unrelated target for the planned scope.
    if plan.scope_mode == "collection":
        clarification = f"{plan.resolved_question} 관련 원문 교차 확인"
        return replace(plan, search_queries=list(dict.fromkeys(
            [*plan.search_queries, clarification]
        )))
    if not plan.locked_entity_terms:
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
    scope_mode, locked_entities = _surface_scope(question)
    if prior_basis and _is_follow_up(question) and scope_mode == "open":
        scope_mode = "inherited"
        _, locked_entities = _surface_scope(prior_basis)
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
    queries = [resolved]
    if question.strip() != resolved:
        queries.append(question.strip())
    if entity_terms:
        field_hint = {
            "contact": "담당자 연락처",
            "money": "비용 예산 견적 금액",
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
        retrieval_mode="hybrid",
        scope_mode=scope_mode,
        locked_entity_terms=locked_entities,
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
            "content_record_scope": {
                "type": ["object", "null"],
                "description": (
                    "Explicit record selection for a request to explain, compare or summarize "
                    "record contents, independent of list/count eligibility. Null when no "
                    "structured record selector is stated."
                ),
                "properties": {
                    "record_type": {
                        "type": "string",
                        "description": "Record type as a lowercase singular machine identifier.",
                    },
                    "group_terms": {
                        "type": "array", "items": {"type": "string"}, "maxItems": 5,
                        "description": "Names in the request identifying the collection or group.",
                    },
                    "title_terms": {
                        "type": "array", "items": {"type": "string"}, "maxItems": 5,
                        "description": "Explicit record classifications visible in titles, not body topics.",
                    },
                },
                "required": ["record_type", "group_terms", "title_terms"],
                "additionalProperties": False,
            },
            "collection_operation": {
                "type": ["string", "null"], "enum": ["count", "list", None],
                "description": (
                    "list includes a bare question asking which records match a type and filters "
                    "without the word list. null for contents, explanation, comparison or a scalar fact."
                ),
            },
            "collection_scope_terms": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
            "collection_record_type": {
                "type": ["string", "null"],
                "description": "A singular, lowercase English machine identifier (e.g. document, event), not a translated display label.",
            },
            "scope_mode": {"type": "string", "enum": ["open", "entity", "inherited", "collection"]},
            "scope_terms": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
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
            "content_record_scope",
            "collection_operation",
            "collection_scope_terms",
            "collection_record_type",
            "scope_mode",
            "scope_terms",
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
                "현재 질문의 생략된 대상만 복원해 독립 질문으로 만든다. "
                "현재 질문에 대상 또는 자료 범위가 명시되면 이전 범위보다 우선하며, 이전 질문의 대상·조건을 "
                "resolved_question이나 search_queries에 추가하지 않는다. scope_mode는 지정 대상이면 entity, "
                "이전 대상 제한을 풀고 자료 범위를 종합하면 collection, 현재 질문에서 생략되어 이전 대상을 "
                "정말 가져온 경우만 inherited, 그 외 open이다. 특정 대상 내부의 전체는 entity다. "
                "scope_terms에는 지정 대상의 원문 표현만 넣는다. 범위 표시어 자체는 대상이 아니다. "
                "같은 사건의 반대·최신 근거도 찾을 수 있는 짧은 검색 쿼리를 작성한다. "
                "사용자가 요구한 항목과 기간, 한 건인지 전체 "
                "목록인지 보존한다. is_state_query는 현재 진행 상태나 현재 단계를 직접 묻는 경우에만 "
                "참으로 지정하고, 거절 이유·원인·설명 질문에는 거짓으로 지정한다. 기록에 없는 사실은 "
                "추가하지 않는다."
                " 정확한 목록이나 개수를 요청한 경우에만 collection_operation을 지정한다. "
                "범위와 조건이 붙은 자료 종류 뒤에 '은?' 또는 '는?'으로 끝나고, 이전 대화에서 "
                "이어받을 작업이나 본문 분석 요청이 없다면, 어떤 항목이 있는지 나열하는 요청(list)이다. "
                "예: '동관 3월 정기 점검 기록은?'은 분류·월·자료 묶음으로 점검 기록을 나열한다. "
                "'그 점검 기록의 원인을 설명해줘'는 목록 요청이 아니다. 현재의 독립적인 자료 선택 요청에 "
                "이전 답변이 수행한 설명·비교·판정을 가져오지 않는다. 목록에서 출력 항목을 생략하면 title만 쓴다. "
                "collection_record_type은 요청된 기록 유형, collection_scope_terms는 자료 묶음을 "
                "한정하는 질문 속 명칭, collection_title_terms는 제목에 명시적으로 요구된 검색어다. "
                "collection_scope_terms는 자료 묶음의 실제 고유 명칭만 넣고, 이름 뒤 일반적인 유형명은 넣지 않는다. "
                "기간을 붙여 게시물·발행물 목록을 선택하면 업로드나 게시라는 말이 생략되어도 "
                "그 기록의 게시일(published_at)을 기간 필드로 쓴다. "
                "기록 유형은 번역한 표시명이 아니라 영문 단수 식별자를 사용한다. 구조화 목록의 "
                "requested_fields에는 표시 문구 대신 title과 해당 time_field 같은 명시적 키를 쓴다. "
                "구조화된 기록 유형·자료 묶음·날짜·제목만으로 모든 조건과 출력 필드를 표현할 수 "
                "있을 때만 collection_filters_supported=true로 제안한다. 실제 데이터가 있는지나 모든 "
                "시각이 채워졌는지는 추측하지 않는다. 서버가 조회 단계에서 확인한다. "
                "내용의 의미나 상태 판단, 제외·OR 조건, 순위·개수 제한이 필요하면 "
                "false로 한다. 기간이 명시된 경우 검증 가능한 시작과 종료를 모두 적는다. "
                "본문 설명·비교·요약에서는 collection_operation=null, collection_filters_supported=false다. "
                "그렇더라도, 질문이 기록 유형과 함께 특정 자료 묶음·기간·분류 중 하나 이상을 지정하면 "
                "그 대상 조건을 content_record_scope에 채운다. 이는 본문에서 찾을 주제가 아니라 "
                "설명할 자료를 고르는 조건이다. 제목 필터에는 제목에 드러나는 기록 분류만 넣는다. "
                "기간이 출판·게시 대상 기록을 제한하면 time_field=published_at을 사용한다. "
                "일반적인 관련 내용 검색이거나 제외·OR 조건 등 대상을 이 필드로 표현할 수 없으면 "
                "content_record_scope=null이다. "
                "현재 요청에 없는 이전 질문의 작업이나 조건을 계승하지 않는다."
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
        record_type = payload.get("collection_record_type")
        if record_type:
            record_type = record_type.strip().casefold() or None
        collection_operation = payload.get("collection_operation")
        content_scope = payload.get("content_record_scope") if not collection_operation else None
        model_start = _parse_plan_datetime(payload["time_start"])
        model_end = _parse_plan_datetime(
            payload["time_end"], end_of_day=bool(collection_operation or content_scope)
        )
        structured_dates = bool(
            (model_start is None and model_end is None and deterministic_start is None)
            or (model_start is not None and model_end is not None and model_start <= model_end)
        )
        content_scope_filters_supported = bool(content_scope and structured_dates)
        if content_scope:
            record_type = content_scope["record_type"].strip().casefold() or None
        final_cardinality = (
            "all"
            if deterministic_cardinality == "all"
            else payload["cardinality"]
        )
        scope_mode = payload["scope_mode"]
        scope_source = question if scope_mode == "entity" else ""
        if scope_mode == "inherited":
            scope_source = "\n".join(
                message.content for message in history if message.role == "user"
            )
            if not scope_source:
                scope_mode = "open"
        scope_source = scope_source.casefold()
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
                model_start if (collection_operation and structured_dates) or content_scope_filters_supported
                else deterministic_start or model_start
            ),
            time_end=(
                model_end if (collection_operation and structured_dates) or content_scope_filters_supported
                else deterministic_end or model_end
            ),
            time_field=_time_field(question) or payload["time_field"],
            is_state_query=bool(payload["is_state_query"] and not has_cause_marker),
            retrieval_mode="hybrid",
            record_type=record_type,
            scope_mode=scope_mode,
            locked_entity_terms=[
                item.strip().casefold()
                for item in payload["scope_terms"]
                if item.strip()
                and item.strip().casefold() in scope_source
                and not re.fullmatch(SCOPE_QUANTIFIER, item.strip(), re.I)
            ],
            collection_title_terms=[
                term.strip() for term in (
                    content_scope["title_terms"] if content_scope
                    else payload.get("collection_title_terms", [])
                ) if term.strip()
            ],
            collection_filters_supported=bool(
                payload.get("collection_filters_supported", False) and structured_dates
            ),
            collection_operation=collection_operation,
            collection_scope_terms=[
                term.strip() for term in (
                    content_scope["group_terms"] if content_scope
                    else payload.get("collection_scope_terms", [])
                ) if term.strip()
            ],
            content_scope_filters_supported=content_scope_filters_supported,
        )
    except Exception:
        return None


def scoped_plan_snapshot(plan: QueryPlan) -> dict | None:
    """Persist selectors, not results, only after a structured lookup succeeded."""
    if (
        plan.retrieval_mode != "scoped" or plan.collection_match_count is None
        or not plan.collection_filters_supported or not plan.record_type
        or plan.collection_operation not in {"list", "count"}
        or plan.scope_mode == "inherited" or _is_follow_up(plan.original_question)
        or SCOPE_REFERENCE.match(SCOPE_PREFIX.sub("", plan.original_question.strip()))
    ):
        return None
    return {
        "version": 1,
        "day": datetime.now(SEOUL).date().isoformat(),
        "question": plan.original_question,
        "resolved": plan.resolved_question,
        "queries": plan.search_queries,
        "entities": plan.entity_terms,
        "field": plan.requested_field,
        "fields": plan.requested_fields,
        "start": plan.time_start.isoformat() if plan.time_start else None,
        "end": plan.time_end.isoformat() if plan.time_end else None,
        "time_field": plan.time_field,
        "type": plan.record_type,
        "scope": plan.scope_mode,
        "locked": plan.locked_entity_terms,
        "title": plan.collection_title_terms,
        "operation": plan.collection_operation,
        "group": plan.collection_scope_terms,
    }


def repeated_scoped_plan(
    question: str, history: list[ConversationMessage],
) -> QueryPlan | None:
    question = question.strip()
    last_user = next((message for message in reversed(history) if message.role == "user"), None)
    if last_user is None or last_user.content.strip() != question:
        return None
    day = datetime.now(SEOUL).date().isoformat()
    for message in reversed(history):
        if message.role != "assistant":
            continue
        saved = (message.evidence_json or {}).get("scoped_plan")
        if (
            not isinstance(saved, dict) or saved.get("version") != 1
            or saved.get("question") != question or saved.get("day") != day
            or saved.get("operation") not in {"list", "count"}
            or saved.get("scope") not in {"open", "entity", "collection"}
        ):
            continue
        start = _parse_plan_datetime(saved.get("start"))
        end = _parse_plan_datetime(saved.get("end"))
        if (
            (saved.get("start") is not None) != (start is not None)
            or (saved.get("end") is not None) != (end is not None)
            or (start is None) != (end is None)
            or (start is not None and end is not None and start > end)
        ):
            continue
        return QueryPlan(
            original_question=question, resolved_question=saved["resolved"],
            search_queries=list(saved["queries"]), entity_terms=list(saved["entities"]),
            requested_field=saved["field"], requested_fields=list(saved["fields"]),
            cardinality="all", time_start=start, time_end=end, time_field=saved["time_field"],
            retrieval_mode="scoped", record_type=saved["type"], scope_mode=saved["scope"],
            locked_entity_terms=list(saved["locked"]), collection_title_terms=list(saved["title"]),
            collection_filters_supported=True, collection_operation=saved["operation"],
            collection_scope_terms=list(saved["group"]),
        )
    return None


def can_prefetch_embedding(question: str) -> bool:
    """Use the existing route rules; leave fast and context-dependent routes alone."""
    return bool(
        question.strip()
        and not assistant_identity_answer(question)
        and not _is_follow_up(question)
        and not _can_use_deterministic_plan(question)
        and _task_mode(question) == "answer"
    )


def plan_queries(
    question: str, history: list[ConversationMessage]
) -> QueryPlan:
    if assistant_identity_answer(question):
        return QueryPlan(original_question=question.strip(),
                         resolved_question=question.strip(), search_queries=[])
    scope_mode, _ = _surface_scope(question)
    if scope_mode != "open":
        history = []
    if _can_use_deterministic_plan(question):
        plan = _fallback_plan(question, history)
    else:
        plan = _llm_plan(question, history) or _fallback_plan(question, history)
    plan = _apply_request_contract(plan, question)
    if (
        plan.task_mode == "answer" and not plan.is_state_query
        and plan.collection_operation in {"count", "list"}
        and plan.collection_filters_supported and plan.record_type
        and (
            plan.collection_operation == "count"
            or set(plan.requested_fields).issubset({"title", plan.time_field})
        )
        and (plan.scope_mode not in {"entity", "inherited"} or plan.collection_scope_terms)
    ):
        plan = replace(plan, retrieval_mode="scoped", cardinality="all")
    else:
        plan = replace(
            plan, retrieval_mode="hybrid",
            cardinality="many" if plan.record_type and plan.cardinality == "all" else plan.cardinality,
            content_scope_filters_supported=bool(
                plan.content_scope_filters_supported
                and plan.task_mode == "answer" and not plan.is_state_query
                and not plan.collection_operation and plan.record_type
                and (plan.scope_mode not in {"entity", "inherited"} or plan.collection_scope_terms)
                and (plan.collection_scope_terms or plan.collection_title_terms
                     or (plan.time_start is not None and plan.time_end is not None))
            ),
        )
    return plan
