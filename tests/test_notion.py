import json

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.connectors.notion import (
    NotionConnector,
    NotionDiscoveryResult,
    NotionPageSyncResult,
)
from app.db import SessionLocal
from app.main import app
from app.models import Chunk, ConnectorCredential, Document, Project
from app.services.security import decrypt_json
import app.services.notion_sync as notion_sync_service


def _response(status_code: int, payload: dict) -> httpx.Response:
    return httpx.Response(
        status_code,
        json=payload,
        headers={"Content-Type": "application/json"},
    )


def test_notion_discovery_collects_pages_and_data_source_entries():
    page_id = "11111111-1111-1111-1111-111111111111"
    entry_id = "22222222-2222-2222-2222-222222222222"
    source_id = "33333333-3333-3333-3333-333333333333"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/search":
            body = json.loads(request.content)
            object_type = body["filter"]["value"]
            if object_type == "page":
                return _response(
                    200,
                    {
                        "results": [{"object": "page", "id": page_id}],
                        "has_more": False,
                        "next_cursor": None,
                        "request_status": {"type": "complete"},
                    },
                )
            return _response(
                200,
                {
                    "results": [
                        {
                            "object": "data_source",
                            "id": source_id,
                            "title": [{"plain_text": "유튜브 제작리스트"}],
                        }
                    ],
                    "has_more": False,
                    "next_cursor": None,
                    "request_status": {"type": "complete"},
                },
            )
        if request.url.path == f"/v1/data_sources/{source_id}/query":
            return _response(
                200,
                {
                    "results": [{"object": "page", "id": entry_id}],
                    "has_more": False,
                    "next_cursor": None,
                },
            )
        return _response(404, {"message": "not found", "code": "object_not_found"})

    connector = NotionConnector(
        "test-token-value", transport=httpx.MockTransport(handler), sleep=lambda _: None
    )
    try:
        result = connector.discover_pages(100)
    finally:
        connector.close()

    assert set(result.pages) == {page_id, entry_id}
    assert result.data_sources == 1
    assert result.incomplete is False
    assert result.pages[entry_id]["_project_brain_collection_id"] == source_id
    assert result.pages[entry_id]["_project_brain_collection_title"] == (
        "유튜브 제작리스트"
    )


def test_notion_api_retries_rate_limit_without_exposing_token():
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        assert request.headers["Authorization"] == "Bearer test-token-value"
        if attempts["count"] == 1:
            return httpx.Response(
                429,
                json={"message": "rate limited", "code": "rate_limited"},
                headers={"Retry-After": "0"},
            )
        return _response(
            200, {"object": "user", "id": "user-id", "name": "Sue", "type": "person"}
        )

    connector = NotionConnector(
        "test-token-value", transport=httpx.MockTransport(handler), sleep=lambda _: None
    )
    try:
        identity = connector.validate_token()
    finally:
        connector.close()

    assert identity["name"] == "Sue"
    assert attempts["count"] == 2


def test_notion_page_sync_indexes_properties_body_and_child_page():
    page_id = "44444444-4444-4444-4444-444444444444"
    child_id = "55555555-5555-5555-5555-555555555555"
    block_id = "66666666-6666-6666-6666-666666666666"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/v1/blocks/{page_id}/children":
            return _response(
                200,
                {
                    "results": [
                        {
                            "object": "block",
                            "id": block_id,
                            "type": "paragraph",
                            "paragraph": {
                                "rich_text": [{"plain_text": "최종 승인자는 김민서 팀장"}]
                            },
                            "has_children": False,
                        },
                        {
                            "object": "block",
                            "id": child_id,
                            "type": "child_page",
                            "child_page": {"title": "하위 페이지"},
                            "has_children": False,
                        },
                    ],
                    "has_more": False,
                    "next_cursor": None,
                },
            )
        return _response(404, {"message": "not found", "code": "object_not_found"})

    db = SessionLocal()
    project = Project(name="Notion 페이지 동기화 테스트")
    db.add(project)
    db.commit()
    db.refresh(project)
    page = {
        "object": "page",
        "id": page_id,
        "url": "https://notion.so/example",
        "last_edited_time": "2026-07-25T00:00:00.000Z",
        "parent": {
            "type": "data_source_id",
            "data_source_id": "88888888-8888-8888-8888-888888888888",
        },
        "_project_brain_collection_id": "88888888-8888-8888-8888-888888888888",
        "_project_brain_collection_title": "제품 결정 데이터",
        "properties": {
            "이름": {
                "type": "title",
                "title": [{"plain_text": "제품 결정 기록"}],
            },
            "상태": {"type": "status", "status": {"name": "확정"}},
        },
    }
    connector = NotionConnector(
        "test-token-value", transport=httpx.MockTransport(handler), sleep=lambda _: None
    )
    try:
        result = connector.sync_page(db, project.id, page)
        page["last_edited_time"] = "2026-07-25T01:00:00.000Z"
        unchanged_result = connector.sync_page(db, project.id, page)
    finally:
        connector.close()

    document = db.scalar(
        select(Document).where(
            Document.project_id == project.id,
            Document.external_id == page_id,
        )
    )
    chunks = db.scalars(
        select(Chunk).where(Chunk.document_id == document.id).order_by(Chunk.position)
    ).all()
    db.close()

    assert result.pages == 1
    assert unchanged_result.unchanged == 1
    assert result.child_page_ids == {child_id}
    assert document.title == "제품 결정 기록"
    assert document.metadata_json["child_page_ids"] == [child_id]
    assert document.metadata_json["last_edited_time"] == "2026-07-25T01:00:00.000Z"
    assert document.metadata_json["collection_id"] == (
        "88888888-8888-8888-8888-888888888888"
    )
    assert document.metadata_json["collection_title"] == "제품 결정 데이터"
    assert "상태: 확정" in "\n".join(chunk.text for chunk in chunks)
    assert "김민서" in "\n".join(chunk.text for chunk in chunks)


def test_notion_connection_encrypts_token_and_sync_status_hides_it(monkeypatch):
    class FakeConnector:
        def __init__(self, token: str):
            self.token = token

        def validate_token(self):
            return {"id": "user-id", "name": "Sue", "type": "person"}

        def discover_pages(self, max_pages: int):
            return NotionDiscoveryResult(
                pages={
                    "77777777-7777-7777-7777-777777777777": {
                        "id": "77777777-7777-7777-7777-777777777777",
                        "last_edited_time": "2026-07-25T00:00:00.000Z",
                    }
                }
            )

        def fetch_page(self, page_id: str):
            return {
                "id": page_id,
                "url": "https://notion.so/example",
                "last_edited_time": "2026-07-25T00:00:00.000Z",
                "properties": {},
            }

        def sync_page(self, db, project_id: str, page: dict):
            return NotionPageSyncResult(pages=1)

        def close(self):
            return None

    monkeypatch.setattr(notion_sync_service, "NotionConnector", FakeConnector)
    secret = "pat_secret_value_123456"

    with TestClient(app) as client:
        project = client.post("/api/projects", json={"name": "Notion 연결 테스트"}).json()
        connected = client.post(
            "/api/connectors/notion/connect",
            json={
                "project_id": project["id"],
                "access_token": secret,
                "auth_mode": "personal_access_token",
                "auto_sync_enabled": True,
                "sync_interval_minutes": 60,
            },
        )
        assert connected.status_code == 200
        assert secret not in connected.text

        synced = client.post(
            "/api/connectors/notion/sync",
            json={"project_id": project["id"], "force": False},
        )
        assert synced.status_code == 200
        assert synced.json()["status"] == "success"
        assert synced.json()["processed_pages"] == 1
        assert secret not in synced.text

        status = client.get(
            "/api/connectors/notion/status", params={"project_id": project["id"]}
        )
        assert status.status_code == 200
        assert secret not in status.text

    db = SessionLocal()
    credential = db.scalar(
        select(ConnectorCredential).where(
            ConnectorCredential.project_id == project["id"],
            ConnectorCredential.provider == "notion",
        )
    )
    assert secret not in credential.encrypted_payload
    assert decrypt_json(credential.encrypted_payload)["access_token"] == secret
    db.close()
