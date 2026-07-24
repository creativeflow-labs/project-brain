from fastapi.testclient import TestClient

from app.main import app


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

