from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient
from sqlalchemy import select

import app.main as main_module
from app.db import SessionLocal
from app.main import app
from app.models import ConnectorCredential
from app.services.security import decrypt_json, encrypt_json


def _create_project(client: TestClient) -> str:
    response = client.post("/api/projects", json={"name": "Gmail OAuth 테스트"})
    assert response.status_code == 201
    return response.json()["id"]


def test_gmail_auth_url_contains_encrypted_pkce_state(monkeypatch):
    monkeypatch.setattr(main_module.settings, "google_client_id", "test-client-id")
    monkeypatch.setattr(
        main_module.settings, "google_client_secret", "test-client-secret"
    )

    with TestClient(app) as client:
        project_id = _create_project(client)
        response = client.get(
            "/api/connectors/gmail/auth-url", params={"project_id": project_id}
        )

    assert response.status_code == 200
    query = parse_qs(urlparse(response.json()["authorization_url"]).query)
    assert query["code_challenge_method"] == ["S256"]
    assert query["code_challenge"][0]

    oauth_payload = decrypt_json(
        query["state"][0],
        ttl_seconds=main_module.GMAIL_OAUTH_STATE_TTL_SECONDS,
    )
    assert oauth_payload["provider"] == "gmail"
    assert oauth_payload["project_id"] == project_id
    assert len(oauth_payload["code_verifier"]) >= 43


def test_gmail_callback_restores_code_verifier_and_stores_token(monkeypatch):
    captured: dict[str, str | None] = {}
    credentials = SimpleNamespace(
        token="access-token",
        refresh_token="refresh-token",
        token_uri="https://oauth2.googleapis.com/token",
        client_id="test-client-id",
        client_secret="test-client-secret",
        scopes=main_module.GMAIL_SCOPES,
    )

    class FakeFlow:
        def __init__(self):
            self.credentials = credentials

        def fetch_token(self, code: str):
            captured["code"] = code

    def fake_google_flow(
        state: str | None = None,
        code_verifier: str | None = None,
    ):
        captured["state"] = state
        captured["code_verifier"] = code_verifier
        return FakeFlow()

    monkeypatch.setattr(main_module, "_google_flow", fake_google_flow)

    with TestClient(app) as client:
        project_id = _create_project(client)
        state = encrypt_json(
            {
                "provider": "gmail",
                "project_id": project_id,
                "code_verifier": "v" * 64,
            }
        )
        response = client.get(
            "/api/connectors/gmail/callback",
            params={"state": state, "code": "authorization-code"},
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/?gmail=connected"
    assert captured == {
        "state": state,
        "code_verifier": "v" * 64,
        "code": "authorization-code",
    }

    with SessionLocal() as db:
        stored = db.scalar(
            select(ConnectorCredential).where(
                ConnectorCredential.project_id == project_id,
                ConnectorCredential.provider == "gmail",
            )
        )
        assert stored is not None
        payload = decrypt_json(stored.encrypted_payload)
        assert payload["refresh_token"] == "refresh-token"


def test_gmail_callback_rejects_tampered_state():
    with TestClient(app) as client:
        response = client.get(
            "/api/connectors/gmail/callback",
            params={"state": "tampered-state", "code": "authorization-code"},
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/?gmail=error&reason=invalid_state"
