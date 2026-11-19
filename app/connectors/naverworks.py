"""Bounded, read-only NAVER WORKS monitoring API download and CSV validation."""
from __future__ import annotations

import csv
import io
import time
from datetime import datetime, timedelta
from urllib.parse import urlsplit, quote

import httpx
from google.auth import crypt, jwt


TOKEN_URL = "https://auth.worksmobile.com/oauth2/v2.0/token"
CONTENTS_URL = "https://www.worksapis.com/v1.0/monitoring/message-contents/download"
SCOPES = "monitoring.read"
MAX_DOWNLOAD_BYTES = 10 * 1024 * 1024


class NaverWorksError(ValueError):
    """Only static, non-sensitive messages may be returned to the UI."""


class NaverWorksDeliveryError(NaverWorksError):
    def __init__(self, message: str, *, uncertain: bool = False):
        super().__init__(message)
        self.uncertain = uncertain


def validate_period(start: datetime, end: datetime) -> None:
    if start.utcoffset() is None or end.utcoffset() is None:
        raise NaverWorksError("조회 시각에는 시간대가 필요합니다.")
    if not timedelta(0) < end - start <= timedelta(days=31):
        raise NaverWorksError("조회 종료는 시작 이후이며, 조회 기간은 최대 31일입니다.")


def _check_http(response: httpx.Response) -> None:
    if response.status_code in (401, 403):
        raise NaverWorksError("인증 또는 모니터링 접근 권한을 확인해 주세요. Lite 이상과 monitoring.read 권한이 필요합니다.")
    if response.status_code == 429:
        raise NaverWorksError("네이버웍스 호출 제한입니다. 자동 반복하지 않았습니다. 잠시 후 다시 확인해 주세요.")
    if response.status_code >= 400:
        raise NaverWorksError(f"네이버웍스 요청 실패(HTTP {response.status_code}). 원문 응답과 인증값은 표시하지 않습니다.")


def _download_url_allowed(url: str) -> bool:
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    # Exact hosts only: generic file storage and the monitoring API's KR audit
    # download host observed in its authenticated 302 response. No wildcards.
    return (parts.scheme == "https" and parts.hostname in {"apis-storage.worksmobile.com", "kr1-audit.worksmobile.com"}
            and port in (None, 443) and not parts.username and not parts.password)


def inspect_csv(data: bytes) -> dict:
    if not data or len(data) > MAX_DOWNLOAD_BYTES:
        raise NaverWorksError("빈 결과 또는 10MB를 초과한 결과입니다. 조회 기간을 줄여 주세요.")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise NaverWorksError("예상과 다른 문자 인코딩입니다. 실제 파일 형식 확인이 필요합니다.") from exc
    if "downloadLogs error" in text:
        raise NaverWorksError("네이버웍스가 다운로드 내부 오류를 반환했습니다. 정상 자료로 처리하지 않았습니다.")
    if text.lstrip().startswith(("<", "{")):
        raise NaverWorksError("CSV가 아닌 응답입니다. 자료로 저장하지 않았습니다.")
    try:
        rows = csv.reader(io.StringIO(text), strict=True)
        columns = next(rows)
        if not 2 <= len(columns) <= 100 or len(set(columns)) != len(columns):
            raise NaverWorksError("CSV 열 구조가 예상과 다릅니다. 실제 형식 확인이 필요합니다.")
        count = 0
        for row in rows:
            if not row:
                continue
            if len(row) != len(columns):
                raise NaverWorksError("열 수가 다른 행이 있습니다. 정상 자료로 처리하지 않았습니다.")
            count += 1
            if count > 50000:
                raise NaverWorksError("확인 가능한 50,000행을 초과했습니다. 조회 기간을 줄여 주세요.")
    except (csv.Error, StopIteration) as exc:
        raise NaverWorksError("CSV 구조를 해석하지 못했습니다. 실제 형식 확인이 필요합니다.") from exc
    return {"columns": columns, "rows": count, "bytes": len(data)}


class NaverWorksConnector:
    def __init__(self, credentials: dict, *, client: httpx.Client | None = None):
        self.credentials = credentials
        self.client = client or httpx.Client(timeout=30, follow_redirects=False)
        self.owns_client = client is None

    def close(self) -> None:
        if self.owns_client:
            self.client.close()

    def authenticate(self, scope: str = SCOPES) -> str:
        if scope not in {SCOPES, "bot.message"}:
            raise NaverWorksError("허용되지 않은 인증 범위입니다.")
        c = self.credentials
        if not all(isinstance(c.get(k), str) and c[k].strip()
                   for k in ("client_id", "client_secret", "service_account", "private_key")):
            raise NaverWorksError("Client ID·Client Secret·Service Account·Private Key가 모두 필요합니다.")
        try:
            signer = crypt.RSASigner.from_string(c["private_key"])
            now = int(time.time())
            assertion = jwt.encode(signer, {"iss": c["client_id"], "sub": c["service_account"],
                                            "iat": now, "exp": now + 300}).decode("ascii")
        except Exception as exc:
            raise NaverWorksError("RSA 개인 키 형식을 확인해 주세요. 키 내용은 표시하지 않습니다.") from exc
        try:
            response = self.client.post(TOKEN_URL, data={
                "assertion": assertion, "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "client_id": c["client_id"], "client_secret": c["client_secret"], "scope": scope,
            })
            _check_http(response)
            body = response.json()
            token = body.get("access_token")
            if not isinstance(token, str) or not token:
                raise NaverWorksError("인증 토큰을 받지 못했습니다. 계정·앱 설정을 확인해 주세요.")
            if scope not in str(body.get("scope", scope)).split():
                raise NaverWorksError("요청한 API 권한이 발급되지 않았습니다.")
            return token
        except (httpx.HTTPError, ValueError) as exc:
            if isinstance(exc, NaverWorksError):
                raise
            raise NaverWorksError("네이버웍스 인증 서버 연결 또는 응답 확인에 실패했습니다.") from exc

    def send_text(self, bot_id: str, user_id: str, text: str) -> None:
        if not bot_id.isdigit() or not user_id or not text or len(text.encode("utf-16-le")) // 2 > 2000:
            raise NaverWorksDeliveryError("봇·수신자·전송문 설정을 확인해 주세요.")
        token = self.authenticate("bot.message")
        try:
            response = self.client.post(
                f"https://www.worksapis.com/v1.0/bots/{bot_id}/users/{quote(user_id, safe='')}/messages",
                headers={"Authorization": f"Bearer {token}"},
                json={"content": {"type": "text", "text": text}},
                follow_redirects=False,
            )
        except httpx.HTTPError:
            raise NaverWorksDeliveryError("발송 결과를 확인할 수 없습니다. 중복 방지를 위해 자동 재전송하지 않습니다.", uncertain=True) from None
        if response.status_code == 201:
            return
        if 400 <= response.status_code < 500:
            raise NaverWorksDeliveryError(f"발송 요청이 거부됐습니다(HTTP {response.status_code}). 권한·봇 이용 설정을 확인해 주세요.")
        raise NaverWorksDeliveryError("예상과 다른 발송 응답입니다. 결과를 확인하기 전에는 다시 보내지 마세요.", uncertain=True)

    def download(self, start: datetime, end: datetime, channel_id: str) -> tuple[bytes, dict]:
        validate_period(start, end)
        if not channel_id.strip() or len(channel_id) > 200:
            raise NaverWorksError("지정 대화방 ID가 필요합니다. 조직 전체 수집은 하지 않습니다.")
        token = self.authenticate()
        headers = {"Authorization": f"Bearer {token}"}
        try:
            response = self.client.get(CONTENTS_URL, headers=headers, params={
                "startTime": start.isoformat(), "endTime": end.isoformat(),
                "language": "ko_KR", "channelId": channel_id, "botMessageFilterType": "exclude",
            })
            _check_http(response)
            location = response.headers.get("location", "")
            if response.status_code != 302 or not _download_url_allowed(location):
                raise NaverWorksError("공식 저장소의 다운로드 경로를 확인하지 못했습니다. 인증값을 전달하지 않았습니다.")
            chunks, size = [], 0
            with self.client.stream("GET", location, headers=headers, follow_redirects=False) as stream:
                _check_http(stream)
                if stream.status_code != 200:
                    raise NaverWorksError("추가 리디렉션 또는 예상과 다른 다운로드 응답으로 중단했습니다.")
                for chunk in stream.iter_bytes():
                    size += len(chunk)
                    if size > MAX_DOWNLOAD_BYTES:
                        raise NaverWorksError("10MB를 초과했습니다. 조회 기간을 줄여 주세요.")
                    chunks.append(chunk)
            data = b"".join(chunks)
            return data, inspect_csv(data)
        except httpx.HTTPError as exc:
            raise NaverWorksError("메시지 다운로드 연결에 실패했습니다. 자동 재시도하지 않았습니다.") from exc
