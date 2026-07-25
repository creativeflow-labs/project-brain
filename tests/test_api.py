from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.main import app
from app.models import SearchTrace
from app.services.ingestion import IngestionInput, ingest_document
from app.services.parser import ParsedSection


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
        assert payload["citations"][0]["document_id"] == document_id
        assert "김민서" in payload["citations"][0]["excerpt"]

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
        assert {item["source_type"] for item in first_payload["citations"]} >= {
            "notion",
            "gmail",
        }
        with SessionLocal() as db:
            trace = db.get(SearchTrace, first_payload["trace_id"])
            assert trace is not None
            assert trace.trace_json["retry_count"] == 0

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
