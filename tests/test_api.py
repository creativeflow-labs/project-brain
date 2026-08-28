from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import SessionLocal
from app.main import app
from app.models import ConversationMessage, SearchTrace, WorkStateSnapshot
from app.services.ingestion import IngestionInput, ingest_document
from app.services.parser import ParsedSection
from app.version import APP_VERSION


def test_home_forces_fresh_versioned_static_assets():
    with TestClient(app) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert f"/static/styles.css?v={APP_VERSION}" in response.text
    assert f"/static/app.js?v={APP_VERSION}" in response.text


def test_user_message_bubble_is_anchored_to_the_right_edge():
    with TestClient(app) as client:
        response = client.get("/static/styles.css")

    assert response.status_code == 200
    assert ".message.user > div { width: 100%; }" in response.text
    assert ".user .message-label, .user .bubble" in response.text
    assert "margin-left: auto" in response.text


def test_frontend_keeps_evidence_scoped_to_each_answer():
    with TestClient(app) as client:
        response = client.get("/static/app.js")

    assert response.status_code == 200
    assert "evidenceByAnswerId" in response.text
    assert "registerAnswerEvidence" in response.text
    assert "activateAnswerEvidence" in response.text
    assert "답변 ${sequence}의 근거" in response.text
    assert 'presentation === "artifact"' in response.text
    assert "artifact-warning" in response.text


def test_upload_search_and_delete_flow():
    with TestClient(app) as client:
        project_response = client.post(
            "/api/projects",
            json={"name": "MVP 테스트", "description": "프로젝트 격리 테스트"},
        )
        assert project_response.status_code == 201
        project_id = project_response.json()["id"]

        upload_response = client.post(
            "/api/documents/upload",
            data={"project_id": project_id},
            files={
                "file": (
                    "decision.md",
                    "# 확정 결정\n현재 방향은 A안이며 최종 승인자는 김민서 팀장이다.".encode(),
                    "text/markdown",
                )
            },
        )
        assert upload_response.status_code == 200
        document_id = upload_response.json()["id"]

        ask_response = client.post(
            "/api/ask",
            json={
                "project_id": project_id,
                "question": "현재 방향과 최종 승인자는 누구야?",
                "top_k": 3,
            },
        )
        assert ask_response.status_code == 200
        payload = ask_response.json()
        assert payload["mode"] == "extractive"
        assert payload["answer_id"]
        assert payload["citations"][0]["document_id"] == document_id
        assert "김민서" in payload["citations"][0]["excerpt"]
        with SessionLocal() as db:
            answer_message = db.get(ConversationMessage, payload["answer_id"])
            assert answer_message is not None
            assert answer_message.evidence_json["citations"][0]["document_id"] == document_id
            assert answer_message.evidence_json["citations"][0]["document_version"]

        delete_response = client.delete(f"/api/documents/{document_id}")
        assert delete_response.status_code == 204
        assert client.get(f"/api/projects/{project_id}/documents").json() == []


def test_project_scope_isolation():
    with TestClient(app) as client:
        first = client.post("/api/projects", json={"name": "첫 프로젝트"}).json()
        second = client.post("/api/projects", json={"name": "둘째 프로젝트"}).json()
        client.post(
            "/api/documents/upload",
            data={"project_id": first["id"]},
            files={"file": ("secret.txt", "첫 프로젝트 전용 정보".encode(), "text/plain")},
        )
        response = client.post(
            "/api/ask",
            json={"project_id": second["id"], "question": "전용 정보가 뭐야?", "top_k": 3},
        ).json()
        assert response["mode"] == "no_evidence"
        assert response["citations"] == []


def test_follow_up_restores_subject_and_compares_cross_source_evidence():
    with TestClient(app) as client:
        project = client.post(
            "/api/projects", json={"name": "MagicLight 회귀 테스트"}
        ).json()
        project_id = project["id"]

        with SessionLocal() as db:
            ingest_document(
                db,
                IngestionInput(
                    project_id=project_id,
                    source_type="notion",
                    external_id="notion-magiclight",
                    title="MagicLight.AI 유료 YouTube 협업",
                    text=(
                        "유료 YouTube 협업 제안\n"
                        "협업 제안 담당자: Brown\n"
                        "유료 협업비: 500만 원"
                    ),
                    source_url="https://notion.so/magiclight",
                    metadata={"last_edited_time": "2026-07-25T00:00:00Z"},
                    sections=[
                        ParsedSection(
                            text="유료 YouTube 협업 제안", locator="Notion 블록 title"
                        ),
                        ParsedSection(
                            text="협업 제안 담당자: Brown", locator="Notion 블록 contact"
                        ),
                        ParsedSection(
                            text="유료 협업비: 500만 원", locator="Notion 블록 fee"
                        ),
                    ],
                ),
            )
            ingest_document(
                db,
                IngestionInput(
                    project_id=project_id,
                    source_type="gmail",
                    external_id="gmail-magiclight-original",
                    title="Paid collaboration proposal from MagicLight.AI",
                    text=(
                        "Hello, this is Emily from MagicLight.AI Creator Partnerships. "
                        "We would like to discuss a paid YouTube collaboration. "
                        "We have not proposed a collaboration fee yet."
                    ),
                    source_url="https://mail.google.com/mail/u/0/#all/magiclight",
                    metadata={
                        "thread_id": "gmail-thread-magiclight",
                        "from": "Emily <emily@magiclight.ai>",
                        "to": "Sue",
                        "date": "Fri, 24 Jul 2026 10:00:00 +0900",
                    },
                ),
            )
            for index in range(14):
                ingest_document(
                    db,
                    IngestionInput(
                        project_id=project_id,
                        source_type="notion",
                        external_id=f"distractor-{index}",
                        title=f"유료 협업 운영 기록 {index}",
                        text="일반 협업 담당자와 비용을 검토하는 내부 체크리스트입니다.",
                    ),
                )

        first = client.post(
            "/api/ask",
            json={
                "project_id": project_id,
                "question": "MagicLight.AI 유료 협업 제안 담당자는 누구야?",
                "top_k": 6,
            },
        )
        assert first.status_code == 200
        first_payload = first.json()
        assert first_payload["thread_id"]
        assert first_payload["evidence_status"] == "conflict"
        assert first_payload["answer_view"]["type"] == "conflict"
        assert first_payload["answer_view"]["headline"] == (
            "협업 담당자를 하나로 확정할 수 없습니다."
        )
        assert "**" not in first_payload["answer"]
        assert [fact["role"] for fact in first_payload["answer_view"]["facts"]] == [
            "Notion · 담당자 기록",
            "Gmail · 원문 발신자",
        ]
        assert [
            item["source_type"] for item in first_payload["citations"][:2]
        ] == ["notion", "gmail"]
        assert {item["source_type"] for item in first_payload["citations"]} >= {
            "notion",
            "gmail",
        }
        with SessionLocal() as db:
            trace = db.get(SearchTrace, first_payload["trace_id"])
            assert trace is not None
            assert trace.trace_json["retry_count"] == 0
            assert set(trace.trace_json["timings_ms"]) == {
                "plan",
                "retrieve_candidates",
                "build_evidence",
                "state",
                "assess",
            }

        follow_up = client.post(
            "/api/ask",
            json={
                "project_id": project_id,
                "thread_id": first_payload["thread_id"],
                "question": "그럼 유료 협업비용이 얼마야?",
                "top_k": 6,
            },
        )
        assert follow_up.status_code == 200
        follow_up_payload = follow_up.json()
        assert follow_up_payload["thread_id"] == first_payload["thread_id"]
        assert "magiclight" in follow_up_payload["resolved_question"].lower()
        assert follow_up_payload["evidence_status"] == "conflict"
        assert follow_up_payload["answer_view"]["type"] == "conflict"
        assert follow_up_payload["answer_view"]["headline"] == (
            "현재 확정된 협업비를 확인할 수 없습니다."
        )
        assert {item["source_type"] for item in follow_up_payload["citations"]} >= {
            "notion",
            "gmail",
        }


def test_notion_parent_context_is_restored_from_separate_blocks():
    with TestClient(app) as client:
        project = client.post(
            "/api/projects", json={"name": "Notion 문맥 복원 테스트"}
        ).json()
        with SessionLocal() as db:
            ingest_document(
                db,
                IngestionInput(
                    project_id=project["id"],
                    source_type="notion",
                    external_id="notion-separated-blocks",
                    title="제니스 캠페인",
                    text="제니스 캠페인\n최종 승인자: 김민서\n캠페인 예산: 800만 원",
                    sections=[
                        ParsedSection(
                            text="제니스 캠페인", locator="Notion 블록 title"
                        ),
                        ParsedSection(
                            text="최종 승인자: 김민서", locator="Notion 블록 owner"
                        ),
                        ParsedSection(
                            text="캠페인 예산: 800만 원", locator="Notion 블록 budget"
                        ),
                    ],
                ),
            )

        response = client.post(
            "/api/ask",
            json={
                "project_id": project["id"],
                "question": "제니스 캠페인의 최종 승인자와 예산은?",
                "top_k": 3,
            },
        )
        assert response.status_code == 200
        citation = response.json()["citations"][0]
        assert "김민서" in citation["excerpt"]
        assert "800만 원" in citation["excerpt"]


def test_status_question_returns_current_state_and_persists_snapshot():
    with TestClient(app) as client:
        project = client.post(
            "/api/projects", json={"name": "현재 상태 API 테스트"}
        ).json()
        with SessionLocal() as db:
            ingest_document(
                db,
                IngestionInput(
                    project_id=project["id"],
                    source_type="notion",
                    external_id="api-old-complete",
                    title="제니스 시안 보드",
                    text="제니스 시안 상태: 완료",
                    metadata={"last_edited_time": "2026-07-20T00:00:00Z"},
                ),
            )
            ingest_document(
                db,
                IngestionInput(
                    project_id=project["id"],
                    source_type="gmail",
                    external_id="api-new-revision",
                    title="제니스 시안 고객 회신",
                    text="고객이 제니스 시안의 문구 수정을 요청했습니다.",
                    metadata={"date": "2026-07-22T00:00:00Z"},
                ),
            )

        response = client.post(
            "/api/ask",
            json={
                "project_id": project["id"],
                "question": "제니스 시안의 현재 상태는?",
                "top_k": 6,
            },
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["current_state"]["state"] == "revision_requested"
        assert payload["current_state"]["label"] == "수정 요청"
        assert payload["answer_view"]["type"] == "state"
        assert payload["answer_view"]["headline"] == (
            "현재 업무 상태는 수정 요청입니다."
        )
        assert "완료 확인" not in payload["answer"]
        assert {item["source_type"] for item in payload["citations"]} >= {
            "notion",
            "gmail",
        }
        with SessionLocal() as db:
            snapshot = db.scalar(
                select(WorkStateSnapshot).where(
                    WorkStateSnapshot.project_id == project["id"]
                )
            )
            assert snapshot is not None
            assert snapshot.state == "revision_requested"
            assert snapshot.assessment_json["decisive_document_ids"]
