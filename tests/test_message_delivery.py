from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import app.action_routes as routes
from app.connectors.naverworks import NaverWorksConnector, NaverWorksDeliveryError, TOKEN_URL
from app.db import SessionLocal
from app.main import app
from app.models import ConnectorCredential, MessageDelivery
from app.services.message_delivery import delivery_intent, split_message
from app.services.delivery_text import plain_delivery_text
from app.services.security import encrypt_json, decrypt_json

HEADERS = {"X-Project-Brain-Client":"1"}


def test_generated_delivery_plain_text_keeps_evidence_urls_and_content():
    source = "## 운영목표\n- **실무 중심:** __업무 연결__ [1]\n* `sk_kim` 참고 [자료](https://example.invalid/a_b)\n```text\n본문\n```"
    assert plain_delivery_text(source) == "운영목표\n• 실무 중심: 업무 연결 [1]\n• sk_kim 참고 자료 (https://example.invalid/a_b)\n본문"


def test_search_preview_and_sent_text_match_without_changing_ui_answer(setup_sender, monkeypatch):
    client, project, _ = setup_sender
    answer = "## 목표\n- **실무 활용:** 업무에 연결합니다. [1]"
    monkeypatch.setattr(routes, "answer_question", lambda *args: SimpleNamespace(
        answer=answer, citations=[SimpleNamespace(title="원본", source_url="https://example.invalid/source")],
        evidence_status="supported"))
    sent = []
    monkeypatch.setattr(NaverWorksConnector, "send_text", lambda self, *args: sent.append(args))
    response = prepare(client, project, "메신저 나에게 운영목표를 정리해서 전달해줘.").json()
    preview = response["preview"]
    assert response["search_result"]["answer"] == answer
    assert preview["text"] == "목표\n• 실무 활용: 업무에 연결합니다. [1]\n\n[근거]\n[1] 원본\nhttps://example.invalid/source"
    assert not sent
    client.post('/api/actions/confirm', headers=HEADERS, json=approval(project, preview))
    assert sent == [("123", "self@company.invalid", preview["text"])]


def test_literal_markdown_is_preserved(setup_sender):
    client, project, _ = setup_sender
    preview = prepare(client, project, '메신저 나에게 "**원문** sk_kim" 그대로 보내줘.').json()["preview"]
    assert preview["text"] == "**원문** sk_kim"


@pytest.fixture
def setup_sender():
    with TestClient(app) as client:
        project = client.post('/api/projects',json={"name":"Delivery synthetic"}).json()["id"]
        other = client.post('/api/projects',json={"name":"Delivery other"}).json()["id"]
        with SessionLocal() as db:
            db.add(ConnectorCredential(project_id=project,provider="naverworks",encrypted_payload=encrypt_json({
                "bot_id":"123", "self_user_id":"self@company.invalid", "self_name":"본인",
                "client_id":"fake", "client_secret":"fake", "service_account":"fake", "private_key":"fake"})))
            db.commit()
        yield client,project,other


def prepare(client, project, question='네이버웍스 나에게 "테스트 중" 보내줘.'):
    return client.post('/api/assistant/request',headers=HEADERS,json={"project_id":project,"question":question})


def approval(project, preview):
    return {"project_id":project,"action_id":preview["id"],"approval_token":preview["approval_token"]}


@pytest.mark.parametrize("question", ["크랩 유튜브 6월 목록을 알려줘", "네이버웍스에서 담당자 발언을 찾아줘", "전달해줘", "노션의 전송 방법 알려줘"])
def test_ordinary_questions_not_routed_as_send(question):
    assert delivery_intent(question) is None


@pytest.mark.parametrize("question", ['네이버웍스 나에게 "테스트 중" 보내줘.', '네이버 웍스 메신저 나에게 아래의 내용을 전달해줘.\n"테스트 중"', "네이버웍스 내게 ‘테스트 중’ 전송해줘"])
def test_quoted_literal_never_requires_retrieval(question):
    intent = delivery_intent(question)
    assert intent.mode == "literal" and intent.content == "테스트 중"


def test_collection_query_instruction_stripped_without_changing_content():
    question="크랩 유튜브 6월 영상 제목을 업로드 시간을 포함해서 리스트로 제시해줘."
    intent=delivery_intent("네이버 웍스 메신저 나에게 아래의 내용을 정리해서 전달해줘.\n\n* "+question)
    assert intent.mode == "search" and intent.content == question
    assert delivery_intent('네이버웍스 팀원에게 "전달" 보내줘').mode == "blocked"
    assert delivery_intent("네이버웍스 나에게 전달해줘").mode == "blocked"


@pytest.mark.parametrize("question", [
    "메신저 나에게 크랩 채널의 운영목표를 정리해서 전달해줘.",
    "네이버웍스 나에게 크랩 채널의 운영목표를 정리해서 전달해줘.",
    '네이버웍스 나에게 "크랩 채널의 운영목표"를 정리해서 전달해줘.',
    '메신저로 나한테 “크랩 채널의 운영목표”를 요약해서 보내주세요.',
    "크랩 채널의 운영목표를 정리해서 네이버웍스로 내게 보내줘.",
    "네이버 웍스 메신저 나에게 아래의 내용을 정리해서 전달해줘.\n크랩 채널의 운영목표",
    '네이버웍스 나에게 아래 내용을 요약해서 전달해줘.\n"크랩 채널의 운영목표"',
])
def test_natural_summary_then_delivery_routes_to_search(question):
    intent=delivery_intent(question)
    assert intent.mode == "search" and "크랩 채널의 운영목표" in intent.content
    assert "정리해줘" in intent.content
    assert "네이버웍스" not in intent.content and "메신저" not in intent.content


@pytest.mark.parametrize("question,body", [
    ('메신저 나에게 "테스트 중" 보내줘.', '테스트 중'),
    ('메신저로 나에게 "운영목표를 정리해줘" 그대로 보내줘.', '운영목표를 정리해줘'),
    ('네이버웍스 나한테 "테스트 중"이라고 전달해줘.', '테스트 중'),
    ('메신저 나에게 아래 내용을 그대로 전달해줘.\n크랩 운영목표 정리해줘.', '크랩 운영목표 정리해줘.'),
    ('메신저 나에게 아래 내용을 보내줘.\n회의 자료 정리했습니다.', '회의 자료 정리했습니다.'),
    ('네이버웍스 나에게 전달해줘: 테스트 중', '테스트 중'),
])
def test_literal_content_kept_without_using_content_as_instructions(question,body):
    intent=delivery_intent(question)
    assert intent.mode == "literal" and intent.content == body


@pytest.mark.parametrize("question", [
    '메신저에서 "나에게 보내줘"라는 메시지를 찾아줘.',
    '네이버웍스에서 나에게 보내준 메시지를 정리해줘.',
    '네이버웍스에서 나에게 보내줘라고 말한 사람은 누구야?',
    '메신저로 전달하는 방법을 알려줘.',
    '네이버웍스에서 김수경의 운영목표 발언을 검색해줘.',
])
def test_read_requests_and_reported_speech_do_not_create_delivery(question):
    assert delivery_intent(question) is None


@pytest.mark.parametrize("question", [
    '메신저 나에게 그 결과 보내줘.',
    '네이버웍스 나에게 운영목표 전달해줘.',
    '네이버웍스 나에게 운영목표를 정리해서 그대로 보내줘.',
    '슬랙 메신저로 나에게 운영목표를 정리해서 보내줘.',
    '네이버웍스 나에게 말고 팀원에게 "테스트 중" 보내줘.',
])
def test_ambiguous_or_unsupported_delivery_requests_ask_instead_of_guessing(question):
    assert delivery_intent(question).mode == "blocked"


def test_source_mention_is_not_confused_with_delivery_channel():
    intent=delivery_intent("메신저 나에게 지메일 협업 메일을 정리해서 보내줘.")
    assert intent.mode == "search" and "지메일 협업 메일" in intent.content


@pytest.mark.parametrize("question", [
    "메신저 나에게 크랩 채널의 운영목표를 정리해서 전달해줘.",
    "네이버웍스 나에게 크랩 채널의 운영목표를 정리해서 전달해줘.",
    '네이버웍스 나에게 "크랩 채널의 운영목표"를 정리해서 전달해줘.',
])
def test_reported_cases_create_preview_of_search_result_not_subject(setup_sender,monkeypatch,question):
    client,project,_=setup_sender
    calls=[]
    sent=[]
    def search(db,project,query,*args):
        calls.append(query)
        return SimpleNamespace(answer="찾은 자료 기준 크랩의 운영목표는 AI를 실무에 연결하는 것입니다.",
            citations=[SimpleNamespace(title="크랩 운영 기록",source_url="https://example.invalid/source")],
            evidence_status="supported")
    monkeypatch.setattr(routes,"answer_question",search)
    monkeypatch.setattr(NaverWorksConnector,"send_text",lambda *args:sent.append(args))
    response=prepare(client,project,question)
    assert response.status_code == 200 and response.json()["kind"] == "delivery_preview"
    preview=response.json()["preview"]
    assert preview["text"].startswith("찾은 자료 기준")
    assert "[근거]" in preview["text"] and "https://example.invalid/source" in preview["text"]
    assert len(calls)==1 and "운영목표" in calls[0]
    assert preview["status"]=="pending" and not sent


def test_literal_preview_requires_approval_and_ordinary_request_unchanged(setup_sender,monkeypatch):
    client,project,_=setup_sender
    sent=[]
    monkeypatch.setattr(NaverWorksConnector,"send_text",lambda *args:sent.append(args))
    preview=prepare(client,project).json()["preview"]
    assert preview["status"]=="pending" and preview["text"]=="테스트 중" and not sent
    assert preview["recipient_id"]=="self@company.invalid"
    with SessionLocal() as db:
        row=db.get(MessageDelivery,preview["id"])
        assert row.approval_hash != preview["approval_token"]
    response=client.post('/api/assistant/request',headers=HEADERS,json={"project_id":project,"question":"너 이름이 뭐니?"})
    assert response.status_code==200 and response.json()["kind"]=="answer"
    assert "Project Brain" in response.json()["result"]["answer"]


def test_double_approval_is_single_send_and_recipient_cannot_be_forged(setup_sender,monkeypatch):
    client,project,_=setup_sender
    sent=[]
    monkeypatch.setattr(NaverWorksConnector,"send_text",lambda self,*args:sent.append(args))
    preview=prepare(client,project).json()["preview"]
    body={**approval(project,preview),"recipient_id":"other@company.invalid","text":"forged"}
    first=client.post('/api/actions/confirm',headers=HEADERS,json=body)
    assert first.json()["status"]=="sent" and first.json()["completed_parts"]==1
    second=client.post('/api/actions/confirm',headers=HEADERS,json=body)
    assert second.json()["status"]=="sent"
    assert sent==[("123","self@company.invalid","테스트 중")]


def test_cross_project_wrong_nonce_csrf_and_cancel_never_send(setup_sender,monkeypatch):
    client,project,other=setup_sender
    sent=[]
    monkeypatch.setattr(NaverWorksConnector,"send_text",lambda *args:sent.append(args))
    preview=prepare(client,project).json()["preview"]
    body=approval(project,preview)
    assert client.post('/api/actions/confirm',json=body).status_code==403
    assert client.post('/api/actions/confirm',headers={**HEADERS,"Origin":"https://evil.invalid"},json=body).status_code==403
    assert client.post('/api/actions/confirm',headers=HEADERS,json={**body,"project_id":other}).status_code==404
    assert client.post('/api/actions/confirm',headers=HEADERS,json={**body,"approval_token":"x"*40}).status_code==403
    assert client.get(f'/api/actions/{preview["id"]}',params={"project_id":other}).status_code==404
    assert client.post('/api/actions/cancel',headers=HEADERS,json=body).json()["status"]=="cancelled"
    assert client.post('/api/actions/confirm',headers=HEADERS,json=body).json()["status"]=="cancelled"
    assert not sent


def test_expired_and_changed_configuration_require_fresh_preview(setup_sender,monkeypatch):
    client,project,_=setup_sender
    sent=[]
    monkeypatch.setattr(NaverWorksConnector,"send_text",lambda *args:sent.append(args))
    old=prepare(client,project).json()["preview"]
    with SessionLocal() as db:
        row=db.get(MessageDelivery,old["id"])
        row.expires_at=datetime.now(timezone.utc)-timedelta(seconds=1)
        db.commit()
    assert client.post('/api/actions/confirm',headers=HEADERS,json=approval(project,old)).status_code==409
    new=prepare(client,project).json()["preview"]
    with SessionLocal() as db:
        saved=db.scalar(select(ConnectorCredential).where(ConnectorCredential.project_id==project,ConnectorCredential.provider=="naverworks"))
        values=decrypt_json(saved.encrypted_payload); values["self_user_id"]="changed@company.invalid"
        saved.encrypted_payload=encrypt_json(values); db.commit()
    assert client.post('/api/actions/confirm',headers=HEADERS,json=approval(project,new)).status_code==409
    assert not sent


def test_uncertain_send_and_process_interruption_never_auto_retry(setup_sender,monkeypatch):
    client,project,_=setup_sender
    calls=[]
    def fail(*args):
        calls.append(1)
        raise NaverWorksDeliveryError("uncertain",uncertain=True)
    monkeypatch.setattr(NaverWorksConnector,"send_text",fail)
    preview=prepare(client,project).json()["preview"]
    for _ in range(2):
        assert client.post('/api/actions/confirm',headers=HEADERS,json=approval(project,preview)).json()["status"]=="unknown"
    assert len(calls)==1
    second=prepare(client,project).json()["preview"]
    with SessionLocal() as db:
        row=db.get(MessageDelivery,second["id"]); row.status="sending"; db.commit()
    assert client.post('/api/actions/confirm',headers=HEADERS,json=approval(project,second)).json()["status"]=="sending"
    assert len(calls)==1


def test_search_instruction_uses_existing_search_and_freezes_results(setup_sender,monkeypatch):
    client,project,_=setup_sender
    queries=[]
    def search(db,project,question,*args):
        queries.append(question)
        return SimpleNamespace(answer="전체 16건\n"+"자료 내용 "*500,citations=[],evidence_status="supported")
    monkeypatch.setattr(routes,"answer_question",search)
    question="크랩 유튜브 6월 영상 제목을 업로드 시간을 포함해서 리스트로 제시해줘."
    response=prepare(client,project,"네이버웍스 나에게 아래 내용을 정리해서 전달해줘.\n* "+question)
    assert response.status_code==200 and queries==[question]
    preview=response.json()["preview"]
    assert preview["part_count"]>1 and preview["text"].startswith("전체 16건")


def test_long_unicode_is_not_silently_truncated():
    text="😀한글"*1000
    parts=split_message(text)
    assert len(parts)>1
    assert all(len(part.encode("utf-16-le"))//2 <= 2000 for part in parts)
    assert "".join(part.split("\n",1)[1] for part in parts)==text


def test_network_failure_is_uncertain_no_retry(monkeypatch):
    sent=[]
    def handler(request):
        sent.append(request)
        raise httpx.ReadTimeout("private response",request=request)
    monkeypatch.setattr(NaverWorksConnector,"authenticate",lambda self,scope:"token")
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(NaverWorksDeliveryError) as exc:
            NaverWorksConnector({},client=client).send_text("123","self@company.invalid","안녕")
    assert exc.value.uncertain and "private" not in str(exc.value) and len(sent)==1
