import json
import csv
import io
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from google.auth import jwt
from sqlalchemy import select

import app.naverworks_routes as routes
from app.connectors.naverworks import (
    CONTENTS_URL, TOKEN_URL, NaverWorksConnector, NaverWorksError,
    _download_url_allowed, inspect_csv, validate_period,
)
from app.db import SessionLocal
from app.main import app
from app.models import ConnectorCredential, Document
from app.services.security import decrypt_json
from app.services.naverworks_sync import message_inputs
from app.services.search_core.retrieval import retrieve_candidates
from app.services.search_core.types import QueryPlan


@pytest.fixture(scope="module")
def credentials():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return {
        "client_id": "synthetic-client", "client_secret": "synthetic-secret",
        "service_account": "test.serviceaccount@example.invalid",
        "private_key": key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                         serialization.NoEncryption()).decode(),
    }


def dates():
    start = datetime(2026, 11, 1, tzinfo=timezone.utc)
    return start, start + timedelta(days=1)


def test_jwt_auth_uses_library_signature_and_minimal_scope(credentials):
    def handle(request):
        assert str(request.url) == TOKEN_URL
        form = parse_qs(request.content.decode())
        assert form["scope"] == ["monitoring.read"]
        private = serialization.load_pem_private_key(credentials["private_key"].encode(), password=None)
        public = private.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        payload = jwt.decode(form["assertion"][0], certs=public)
        assert payload["iss"] == credentials["client_id"]
        assert payload["sub"] == credentials["service_account"]
        assert payload["exp"] - payload["iat"] == 300
        return httpx.Response(200, json={"access_token":"synthetic-token", "scope":"monitoring.read"})
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert NaverWorksConnector(credentials, client=client).authenticate() == "synthetic-token"


@pytest.mark.parametrize("code", [400, 401, 403, 429, 500])
def test_auth_errors_do_not_expose_remote_body_or_secrets(credentials, code):
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(code, text="REMOTE_PRIVATE_CONTENT"))) as client:
        with pytest.raises(NaverWorksError) as error:
            NaverWorksConnector(credentials, client=client).authenticate()
    assert "REMOTE_PRIVATE_CONTENT" not in str(error.value)
    assert credentials["client_secret"] not in str(error.value)


def test_invalid_private_key_fails_before_network():
    connector = NaverWorksConnector({"client_id":"x", "client_secret":"y", "service_account":"z", "private_key":"PRIVATE_INVALID"})
    try:
        with pytest.raises(NaverWorksError) as error:
            connector.authenticate()
        assert "PRIVATE_INVALID" not in str(error.value)
    finally:
        connector.close()


@pytest.mark.parametrize("url", ["http://apis-storage.worksmobile.com/f", "https://localhost/f",
    "https://apis-storage.worksmobile.com.evil.invalid/f", "https://evil@apis-storage.worksmobile.com/f",
    "https://apis-storage.worksmobile.com:8443/f", "https://apis-storage.worksmobile.com:bad/f"])
def test_download_url_cannot_leak_token(url):
    assert not _download_url_allowed(url)


def test_bounded_csv_download_and_channel_scope(credentials):
    data = '\ufefftime,sender,text\n2026-11-01,fictional,"first\nsecond"\n'.encode()
    calls = []
    def handle(request):
        calls.append(str(request.url))
        if str(request.url) == TOKEN_URL:
            return httpx.Response(200, json={"access_token":"token"})
        if str(request.url).startswith(CONTENTS_URL):
            assert request.url.params["channelId"] == "test-room"
            assert request.url.params["botMessageFilterType"] == "exclude"
            return httpx.Response(302, headers={"Location":"https://apis-storage.worksmobile.com/f"})
        assert request.headers["authorization"] == "Bearer token"
        return httpx.Response(200, content=data)
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        raw, result = NaverWorksConnector(credentials, client=client).download(*dates(), "test-room")
    assert raw == data and result["rows"] == 1
    assert result["columns"] == ["time", "sender", "text"]
    assert len(calls) == 3


def test_redirect_outside_documented_host_is_not_followed(credentials):
    calls = []
    def handle(request):
        calls.append(str(request.url))
        if str(request.url) == TOKEN_URL:
            return httpx.Response(200, json={"access_token":"token"})
        return httpx.Response(302, headers={"Location":"https://evil.invalid/f"})
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(NaverWorksError):
            NaverWorksConnector(credentials, client=client).download(*dates(), "test-room")
    assert len(calls) == 2


@pytest.mark.parametrize("data", [b"", b"a,b\n1\n", b"a,a\n1,2\n", b"a,b\n1,2\ndownloadLogs error secret",
                                  b"<html>error</html>", b'{"error":"private"}', b"\xff\xfe"])
def test_csv_failure_is_not_reported_as_valid(data):
    with pytest.raises(NaverWorksError):
        inspect_csv(data)


def test_dates_reject_naive_reversed_and_oversized_period():
    start, end = dates()
    for a, b in [(start.replace(tzinfo=None), end), (end, start), (start,start+timedelta(days=32))]:
        with pytest.raises(NaverWorksError):
            validate_period(a, b)


def test_api_connect_is_encrypted_scoped_and_download_does_not_ingest(monkeypatch, credentials):
    monkeypatch.setattr(routes.NaverWorksConnector, "authenticate", lambda self: "fake-token")
    monkeypatch.setattr(routes.NaverWorksConnector, "download", lambda self, *args: (b"a,b\n1,2\n", {"rows":1}))
    headers = {"X-Project-Brain-Client":"1"}
    with TestClient(app) as client:
        project = client.post('/api/projects', json={"name":"Works synthetic"}).json()["id"]
        other = client.post('/api/projects', json={"name":"Works other"}).json()["id"]
        payload = {"project_id":project, **credentials}
        assert client.post('/api/connectors/naverworks/connect', json=payload).status_code == 403
        response = client.post('/api/connectors/naverworks/connect', json=payload, headers=headers)
        assert response.status_code == 200 and response.json()["ingestion_enabled"] is True
        assert "synthetic-secret" not in response.text
        assert not client.get('/api/connectors/naverworks/status', params={"project_id":other}).json()["connected"]
        assert client.get('/api/connectors/naverworks/status', params={"project_id":"missing"}).status_code == 404
        with SessionLocal() as db:
            row = db.scalar(select(ConnectorCredential).where(ConnectorCredential.project_id==project, ConnectorCredential.provider=="naverworks"))
            assert "synthetic-secret" not in row.encrypted_payload
            assert decrypt_json(row.encrypted_payload)["client_secret"] == "synthetic-secret"
        start, end = dates()
        download = client.post('/api/connectors/naverworks/download', headers=headers,
            json={"project_id":project, "channel_id":"room", "start":start.isoformat(), "end":end.isoformat()})
        assert download.status_code == 200 and download.headers["X-Message-Rows"] == "1"
        assert download.headers["Cache-Control"] == "no-store"
        with SessionLocal() as db:
            assert db.scalar(select(Document).where(Document.project_id==project)) is None
        assert client.post('/api/connectors/naverworks/disconnect', json={"project_id":project}, headers=headers).status_code == 200
        assert not client.get('/api/connectors/naverworks/status', params={"project_id":project}).json()["connected"]


def test_cross_origin_setup_is_rejected():
    with TestClient(app) as client:
        response = client.post('/api/connectors/naverworks/disconnect', json={"project_id":"none"},
            headers={"Origin":"https://foreign.invalid", "X-Project-Brain-Client":"1"})
    assert response.status_code == 403


def works_csv(*, channel="room", date="2026-11-01T12:00:00+00:00", body="오로라 프로젝트 납품일은 11월 20일입니다.", duplicate=False):
    stream = io.StringIO()
    writer = csv.writer(stream)
    writer.writerow(["날짜", "보낸 사람", "받는 사람", "채널 ID", "메시지"])
    row = [date, "테스트 발신자", "테스트 수신자", channel, body]
    writer.writerow(row)
    if duplicate:
        writer.writerow(row)
    return stream.getvalue().encode("utf-8-sig")


def test_monitoring_host_exact_allowlist():
    assert _download_url_allowed("https://kr1-audit.worksmobile.com/download/messages")
    for url in ["http://kr1-audit.worksmobile.com/a", "https://kr1-audit.worksmobile.com.evil.invalid/a",
                "https://kr1-audit.worksmobile.com:8443/a", "https://user@kr1-audit.worksmobile.com/a"]:
        assert not _download_url_allowed(url)


def test_actual_csv_schema_normalization_dedup_and_multiline():
    items = message_inputs(works_csv(body="납품일 변경\n11월 20일", duplicate=True), "p", "room", *dates(), "업무방")
    assert len(items) == 1
    item = items[0]
    assert item.source_type == "naverworks" and item.project_id == "p"
    assert "납품일 변경\n11월 20일" in item.text
    assert "2026-11-01 21:00:00" in item.text
    assert item.metadata["channel_id"] == "room"
    assert item.metadata["occurred_at"] == "2026-11-01T12:00:00+00:00"
    equivalent = message_inputs(works_csv(date="2026-11-01T21:00:00+09:00",body="납품일 변경\n11월 20일"),"p","room",*dates(),"다른 표시 이름")
    assert equivalent[0].external_id == item.external_id


@pytest.mark.parametrize("data", [works_csv(channel="other-room"), works_csv(date="2026-11-03T12:00:00+00:00"),
                                  works_csv(date="2026-11-01T12:00:00"), b"a,b\n1,2\n"])
def test_normalization_rejects_wrong_channel_time_and_schema(data):
    with pytest.raises(NaverWorksError):
        message_inputs(data,"p","room",*dates())


def test_empty_message_not_indexed_and_size_cap(monkeypatch):
    assert message_inputs(works_csv(body=" "),"p","room",*dates()) == []
    import app.services.naverworks_sync as service
    monkeypatch.setattr(service,"MAX_SYNC_MESSAGES",0)
    with pytest.raises(NaverWorksError):
        message_inputs(works_csv(),"p","room",*dates())


def test_sync_indexes_messages_searches_with_source_and_is_idempotent(monkeypatch, credentials):
    monkeypatch.setattr(routes.NaverWorksConnector,"authenticate",lambda self:"fake-token")
    monkeypatch.setattr(routes.NaverWorksConnector,"download",lambda self,*args:(works_csv(),{"rows":1}))
    headers = {"X-Project-Brain-Client":"1"}
    with TestClient(app) as client:
        project = client.post('/api/projects',json={"name":"Works ingestion test"}).json()["id"]
        other = client.post('/api/projects',json={"name":"Works isolated test"}).json()["id"]
        assert client.post('/api/connectors/naverworks/connect',headers=headers,json={"project_id":project,**credentials}).status_code == 200
        start,end=dates()
        payload={"project_id":project,"channel_id":"room","channel_name":"업무방","start":start.isoformat(),"end":end.isoformat()}
        assert client.post('/api/connectors/naverworks/sync',json=payload).status_code == 403
        first=client.post('/api/connectors/naverworks/sync',headers=headers,json=payload)
        assert first.status_code == 200 and first.json()["changed_messages"] == 1
        second=client.post('/api/connectors/naverworks/sync',headers=headers,json=payload)
        assert second.status_code == 200 and second.json()["unchanged_messages"] == 1
        with SessionLocal() as db:
            docs=db.scalars(select(Document).where(Document.project_id==project,Document.source_type=="naverworks")).all()
            assert len(docs)==1 and docs[0].chunks
            plan=QueryPlan(original_question="오로라 납품일",resolved_question="오로라 납품일",search_queries=["오로라 납품일"])
            assert any(c.document.id==docs[0].id for c in retrieve_candidates(db,project,plan))
            assert not retrieve_candidates(db,other,plan)
        status=client.get('/api/connectors/naverworks/status',params={"project_id":project}).json()
        assert status["last_sync_count"]==1 and status["channel_id"]=="room"
        assert status["last_sync_at"]
