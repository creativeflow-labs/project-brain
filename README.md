# Project Brain MVP

주요 제품·기술 결정은 [docs/DECISIONS.md](docs/DECISIONS.md)에 기록합니다.

프로젝트별로 선택한 문서·Notion 페이지·Gmail 메시지를 연결하고, 근거와 함께 답하는 읽기 전용 AI 비서입니다.

이 버전은 대표님이 VS Code에서 직접 실행하고 제품 흐름을 검증하기 위한 로컬 MVP입니다. 한 프로젝트의 데이터가 다른 프로젝트 검색에 섞이지 않도록 `project_id`를 모든 수집·청크·검색 단계에서 강제합니다.

## 구현 범위

- 프로젝트 생성과 전환
- PDF·DOCX·TXT·MD 일반 업로드
- PDF 페이지, DOCX 제목·표, Markdown 제목 위치 보존
- Notion 페이지 블록과 지원 형식 첨부파일 가져오기
- Gmail 본문과 지원 형식 첨부파일 동기화
- PostgreSQL 17 + pgvector 저장
- 의미 유사도와 키워드 중첩을 결합한 프로젝트 단위 검색
- OpenAI Responses API 기반 근거 종합 답변
- OpenAI 키가 없을 때 로컬 임베딩과 추출형 답변
- 답변별 원본·위치·관련도 표시
- 원본 문서 및 인덱스 삭제

## 가장 빠른 실행 방법

필수 프로그램:

- Docker Desktop
- VS Code

압축을 푼 폴더를 VS Code에서 열고 터미널에서 실행합니다.

```bash
docker compose up --build
```

브라우저에서 다음 주소를 엽니다.

```text
http://localhost:8000
```

### VS Code 작업 실행

이 저장소를 VS Code의 루트 폴더로 연 뒤 `Terminal > Run Task`에서 다음 작업을 실행할 수 있습니다.

- `Project Brain: 시작`
- `Project Brain: 상태 확인`
- `Project Brain: 앱 로그`
- `Project Brain: 테스트`
- `Project Brain: 앱 재시작`
- `Project Brain: 중지`

기본 빌드 작업은 `Project Brain: 시작`이며 `⇧⌘B`로 실행할 수 있습니다.

처음에는 `.env`의 `OPENAI_API_KEY`가 비어 있으므로 비용 없이 파일 파싱·검색·출처 표시를 테스트할 수 있습니다. `sample-data/project-onboarding-sample.md`를 업로드하고 다음 질문을 권장합니다.

```text
고객이 2차 시안을 거절한 이유와 현재 확정된 방향, 최종 승인자는 누구인가요?
```

종료:

```bash
docker compose down
```

데이터까지 완전히 지우려는 경우에만 다음 명령을 사용합니다.

```bash
docker compose down -v
```

`-v`는 PostgreSQL 볼륨을 삭제하므로 기존 테스트 데이터가 복구되지 않습니다.

## OpenAI 생성형 답변 활성화

`.env`에서 다음 값을 입력한 뒤 앱을 재시작합니다.

```dotenv
OPENAI_API_KEY=sk-...
```

기본 답변 모델은 비용과 품질 균형을 고려한 `gpt-5.6-terra`, 임베딩은 `text-embedding-3-small`의 384차원을 사용합니다. 서버는 Responses API를 `store=False`로 호출합니다.

키를 바꾼 뒤 기존 문서는 이전 방식으로 생성된 임베딩을 보유합니다. 정확한 비교를 위해 테스트 프로젝트의 문서를 삭제하고 다시 업로드하는 것이 좋습니다.

## Notion 연결

1. Notion에서 Internal Integration을 만듭니다.
2. 가져올 페이지에서 해당 Integration을 연결(공유)합니다.
3. 화면의 `데이터 연결 > Notion`에서 Integration token과 페이지 URL을 입력합니다.
4. 여러 페이지는 한 줄에 하나씩 입력합니다.

Notion 토큰은 현재 MVP에서 서버에 저장하지 않고 해당 가져오기 요청에만 사용합니다. 페이지 블록을 읽은 뒤, 페이지 안의 PDF·DOCX·TXT·MD 파일도 내려받아 공통 파서로 전달합니다.

Notion이 발급하는 내부 파일 URL은 임시 URL이므로 동기화 중 즉시 다운로드합니다.

## Gmail 연결

Gmail은 Google Cloud OAuth 설정이 있어야 합니다.

1. Google Cloud Console에서 Gmail API를 활성화합니다.
2. OAuth 동의 화면을 구성하고 테스트 사용자를 등록합니다.
3. Web application OAuth Client를 만듭니다.
4. Authorized redirect URI에 아래 주소를 등록합니다.

```text
http://localhost:8000/api/connectors/gmail/callback
```

5. 암호화 키를 생성합니다.

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

6. `.env`에 값을 넣습니다.

```dotenv
GOOGLE_CLIENT_ID=...
GOOGLE_CLIENT_SECRET=...
TOKEN_ENCRYPTION_KEY=생성한_키
```

7. 앱을 재시작하고 `Google 계정 연결`을 누릅니다.
8. Gmail 검색식을 지정해 동기화합니다.

권장 검색식:

```text
label:프로젝트명 newer_than:90d
```

OAuth 범위는 `gmail.readonly`만 사용합니다. 토큰은 PostgreSQL에 Fernet 암호화하여 저장합니다. 프로덕션에서는 환경 변수 대신 Secret Manager 또는 KMS로 이전해야 합니다.

## Docker 없이 로컬 실행

이 방식은 빠른 코드 디버깅을 위해 SQLite를 사용합니다. 실제 pgvector 흐름은 Docker 실행으로 확인하세요.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env.local
DATABASE_URL=sqlite:///./data/project_brain.db \
UPLOAD_DIR=./data/uploads \
uvicorn app.main:app --reload
```

## 테스트

```bash
DATABASE_URL=sqlite:///./data/test_project_brain.db python -m pytest -q
```

테스트 범위:

- Markdown 제목 위치 보존
- DOCX 문단과 표 추출
- 위장 PDF 거부
- 긴 문서 청킹
- 업로드 → 검색 → 출처 → 삭제 API 흐름
- 프로젝트 간 데이터 격리

## API 요약

| 메서드 | 경로 | 기능 |
|---|---|---|
| `GET` | `/api/health` | DB·답변 모드 확인 |
| `POST` | `/api/projects` | 프로젝트 생성 |
| `GET` | `/api/projects` | 프로젝트 목록 |
| `POST` | `/api/documents/upload` | 파일 업로드·파싱·인덱싱 |
| `GET` | `/api/projects/{id}/documents` | 프로젝트 자료 목록 |
| `DELETE` | `/api/documents/{id}` | 문서와 청크 삭제 |
| `POST` | `/api/ask` | 프로젝트 범위 검색·답변 |
| `POST` | `/api/connectors/notion/import` | Notion 페이지·첨부 가져오기 |
| `GET` | `/api/connectors/gmail/auth-url` | Gmail OAuth 시작 |
| `POST` | `/api/connectors/gmail/sync` | Gmail 본문·첨부 동기화 |

Swagger 문서는 `http://localhost:8000/docs`에서 확인할 수 있습니다.

## 현재 MVP 제한

- 스캔 PDF OCR 제외
- HWP/HWPX, PPTX, XLSX, 이미지, ZIP 제외
- Notion은 수동 가져오기이며 Webhook 증분 동기화 제외
- Gmail은 검색식 기반 수동 동기화이며 Push Notification 제외
- 사용자 로그인과 세부 권한 제외, 프로젝트 단위 데이터 격리만 적용
- 백그라운드 큐 제외, 요청 안에서 파싱·임베딩 수행
- 대규모 데이터 최적화 제외. 현재 검색은 프로젝트의 청크를 읽어 애플리케이션에서 재순위화
- 악성코드 스캔 제외. 외부 공개 전에 ClamAV 또는 별도 검사 계층 필요

이 제한들은 구조적 누락이 아니라 테스트 범위를 빠르게 확보하기 위한 명시적 MVP 경계입니다.
