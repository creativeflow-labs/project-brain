# Project Brain

Notion, Gmail, 일반 문서를 프로젝트 단위로 연결하고 원문 근거와 함께 답하는 팀용 AI 지식 비서입니다.

## Project Overview

Project Brain은 흩어진 프로젝트 기록을 연결해 무엇이 왜 결정됐고, 현재 어떤 정보가 근거가 되는지 설명하는 읽기 전용 AI 비서를 지향합니다. 단순한 전사 검색보다 팀과 프로젝트에 밀착된 맥락, 출처, 변경 이력을 정확하게 다루는 것이 제품의 핵심입니다.

`v0.2.1`은 데이터 연결과 로컬 실행 흐름의 보존 기준선입니다. 현재 개발 브랜치의 `v0.5.2-alpha.1`은 이 구조를 유지하면서 단건 검색 후보를 PostgreSQL에서 먼저 제한하고, 전체 목록 검색은 완전 탐색으로 보존하는 검색 코어 정리 버전입니다.

### Core Experience

```text
프로젝트 생성
  → Notion·Gmail·파일 연결
  → 프로젝트 범위 검색
  → 원문 근거를 포함한 답변
  → 출처에서 직접 검증
```

한 프로젝트의 데이터가 다른 프로젝트 검색에 섞이지 않도록 `project_id`를 수집·청크·검색 단계에서 강제합니다.

주요 제품·기술 결정은 [docs/DECISIONS.md](docs/DECISIONS.md), 개발 순서와 완료 기준은 [docs/ROADMAP.md](docs/ROADMAP.md), 출시별 변경 사항은 [CHANGELOG.md](CHANGELOG.md)에 기록합니다.

## Features

- 프로젝트 생성과 전환
- PDF·DOCX·TXT·MD 일반 업로드
- PDF 페이지, DOCX 제목·표, Markdown 제목 위치 보존
- Notion 접근 범위 자동 발견·증분 동기화와 지원 형식 첨부파일 가져오기
- Gmail 본문과 지원 형식 첨부파일 동기화
- PostgreSQL 17 + pgvector 저장
- 의미 검색, BM25, 정확 일치, 제목·메타데이터를 결합한 프로젝트 단위 검색
- 후속 질문의 대상 복원과 독립형 검색 질문 생성
- Notion 부모 페이지와 Gmail 동일 스레드 문맥 복원
- 지원됨·충돌·근거 부족 내부 판정과 제한 재검색
- 완료 주장·최종 승인·수정 요청을 구분하는 업무 상태 판정
- 현재 상태·기준 시점·다음 확인·최근 시간축 표시
- 한 줄 결론·판정 상태·역할별 사실·다음 확인으로 구성된 구조화 답변
- 담당자와 금액의 역할 구분 및 같은 역할 안의 충돌만 판정
- 검색 계획·근거 문서·판정 상태 추적
- OpenAI Responses API 기반 근거 종합 답변
- OpenAI 키가 없을 때 로컬 임베딩과 추출형 답변
- 답변별 원본·위치·관련도 표시
- 원본 문서 및 인덱스 삭제

## Tech Stack

| Layer | Technology |
|---|---|
| Backend | Python 3.12, FastAPI |
| Database | PostgreSQL 17, pgvector |
| ORM | SQLAlchemy 2 |
| AI | OpenAI Responses API, Embeddings API |
| Search workflow | LangGraph 1.2 |
| Local fallback | 결정적 로컬 임베딩, 추출형 답변 |
| Connectors | Notion API, Gmail API OAuth 2.0 |
| Parsing | PDF, DOCX, TXT, Markdown |
| Frontend | HTML, CSS, Vanilla JavaScript |
| Runtime | Docker Compose |
| Tests | pytest |

## Project Structure

```text
app/
├── connectors/        # Notion·Gmail 연결
├── services/          # 수집·파싱·청킹·임베딩·검색·보안
│   └── search_core/   # 질문 계획·검색·문맥·판정·상태·시간축·판정 보고
├── static/            # 웹·모바일 반응형 UI
├── main.py            # FastAPI 엔드포인트
├── models.py          # 프로젝트·문서·청크·연결정보 모델
└── schemas.py         # API 요청·응답 스키마
docs/
├── DECISIONS.md       # 주요 아키텍처 결정
└── ROADMAP.md         # 단계별 개발 계획과 완료 기준
sample-data/           # 비식별 테스트 자료
tests/                 # API·파서·연결·보안 테스트
```

## Installation

필수 프로그램:

- Docker Desktop
- VS Code

저장소를 내려받고 프로젝트 폴더를 VS Code에서 엽니다.

```bash
git clone https://github.com/creativeflow-labs/project-brain.git
cd project-brain
cp .env.example .env
```

터미널에서 실행합니다.

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

처음에는 `.env`의 `OPENAI_API_KEY`가 비어 있으므로 비용 없이 파일 파싱·검색·출처 표시를 테스트할 수 있습니다. `sample-data/project-onboarding-sample.md`를 업로드하고 다음 질문으로 흐름을 확인할 수 있습니다.

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

## Configuration

실제 인증값은 `.env`에만 저장합니다. `.env`, 로컬 암호화 키, 업로드 원본, 데이터베이스 파일은 Git과 Docker 빌드 컨텍스트에서 제외됩니다.

### OpenAI 생성형 답변 활성화

`.env`에서 다음 값을 입력한 뒤 앱을 재시작합니다.

```dotenv
OPENAI_API_KEY=sk-...
```

기본 답변 모델은 비용과 품질 균형을 고려한 `gpt-5.6-terra`, 임베딩은 `text-embedding-3-small`의 384차원을 사용합니다. 서버는 Responses API를 `store=False`로 호출합니다.

키를 바꾼 뒤 기존 문서는 이전 방식으로 생성된 임베딩을 보유합니다. 정확한 비교를 위해 테스트 프로젝트의 문서를 삭제하고 다시 업로드하는 것이 좋습니다.

### Notion 연결

현재 사내 검증에서 한 계정의 접근 범위를 넓게 동기화하려면 Notion 개발자 화면의 `개인 액세스 토큰`을 사용합니다.

1. Notion 개발자 화면에서 `개인 액세스 토큰`을 만듭니다.
2. Notion API 기능을 허용하고 토큰을 복사합니다.
3. `데이터 연결 > Notion`에서 토큰과 인증 범위를 입력합니다.
4. 자동 동기화 주기를 선택하고 `연결 저장 후 동기화`를 누릅니다.

토큰은 PostgreSQL에 Fernet 암호화해 저장하며, 암호화 키가 비어 있으면 `data/secrets/token-encryption.key`에 권한 `600`으로 자동 생성합니다. 페이지 URL은 입력하지 않습니다.

동기화는 다음 순서로 작동합니다.

- Search API로 접근 가능한 페이지와 데이터 소스를 발견
- 데이터 소스 항목과 발견된 하위 페이지 순회
- `last_edited_time`과 콘텐츠 해시를 이용해 변경된 문서만 재처리
- 페이지별 오류 격리, 429·5xx 재시도, 최대 수집량 제한
- 삭제·보관·권한 회수로 404가 확인된 기존 문서와 첨부 인덱스 제거
- Notion이 호스팅하는 PDF·DOCX·TXT·MD만 크기 제한 안에서 다운로드

Notion 공식 Search API는 계정 내 모든 문서의 완전한 열거를 보장하지 않습니다. PAT 모드는 `최선 노력 계정 탐색`, 내부 연결과 향후 OAuth 모드는 `승인된 범위 동기화`로 상태 화면에 구분합니다.

### Gmail 연결

Gmail은 Google Cloud OAuth 설정이 있어야 합니다.

1. Google Cloud Console에서 Gmail API를 활성화합니다.
2. OAuth 동의 화면을 구성하고 테스트 사용자를 등록합니다.
3. Web application OAuth Client를 만듭니다.
4. Authorized redirect URI에 아래 주소를 등록합니다.

```text
http://localhost:8000/api/connectors/gmail/callback
```

5. 필요하면 암호화 키를 직접 생성합니다. 비워두면 로컬 보안 파일로 자동 생성됩니다.

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

### Docker 없이 로컬 실행

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

## Testing

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
- Notion 페이지·데이터 소스 자동 발견
- Notion 속성·본문·하위 페이지 인덱싱
- Notion 토큰 암호화 저장과 API 응답 비노출
- Gmail OAuth PKCE 상태 복원·변조 거부·토큰 암호화 저장

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
| `POST` | `/api/connectors/notion/connect` | Notion 토큰 암호화 저장 |
| `GET` | `/api/connectors/notion/status` | Notion 연결·동기화 상태 |
| `POST` | `/api/connectors/notion/sync` | Notion 접근 범위 증분 동기화 |
| `POST` | `/api/connectors/notion/import` | 이전 페이지 지정 방식 호환 API |
| `GET` | `/api/connectors/gmail/auth-url` | Gmail OAuth 시작 |
| `POST` | `/api/connectors/gmail/sync` | Gmail 본문·첨부 동기화 |

Swagger 문서는 `http://localhost:8000/docs`에서 확인할 수 있습니다.

`POST /api/ask`는 기존 필드와 호환되며 선택적으로 `thread_id`를 받습니다. 응답의 `thread_id`를 다음 질문에 보내면 후속 질문의 생략된 프로젝트·사건 문맥을 복원합니다. 응답에는 내부 근거 상태인 `evidence_status`, 독립형 질문 `resolved_question`, 실행 추적 ID `trace_id`, 충돌 설명 `conflicts`도 포함됩니다.

## Security and Privacy

- `.env`와 외부 서비스 인증 파일은 Git에서 제외
- Notion·Gmail 토큰은 프로젝트별 Fernet 암호화 저장
- Gmail OAuth는 PKCE와 만료되는 암호화 상태 사용
- OpenAI Responses API 호출은 `store=False` 적용
- 외부 연결은 읽기 전용 권한부터 검증
- 업로드 원본과 로컬 데이터베이스는 저장소에 포함하지 않음

현재 버전은 로컬 사내 검증용입니다. 사용자 인증, 세부 권한, 고객사별 테넌트 격리, 감사 로그가 구현되기 전에는 인터넷에 공개 배포하지 않습니다.

## Current Limitations

- 스캔 PDF OCR 제외
- HWP/HWPX, PPTX, XLSX, 이미지, ZIP 제외
- Notion Search API의 계정 전체 열거 비보장
- Notion Webhook 실시간 동기화 제외, 현재는 설정 주기 기반 자동 동기화
- 외부 고객용 Notion OAuth 제외, 현재는 PAT·내부 연결 토큰
- Gmail은 검색식 기반 수동 동기화이며 Push Notification 제외
- 사용자 로그인과 세부 권한 제외, 프로젝트 단위 데이터 격리만 적용
- 백그라운드 큐 제외, 요청 안에서 파싱·임베딩 수행
- 단건 사실 검색은 PostgreSQL 후보 검색 후 애플리케이션에서 재순위화하며, 전체 목록 검색은 누락 방지를 위해 프로젝트 청크를 완전 탐색
- 악성코드 스캔 제외. 외부 공개 전에 ClamAV 또는 별도 검사 계층 필요
- 출처 검색 범위를 사용자가 직접 선택하는 UI 제외
- 모든 문서를 수집 시점에 사건·주장으로 영구 구조화하는 사전 인덱싱 제외
- 상태 판정은 현재 한국어 핵심 상태 표현 중심이며, 실제 조직별 표현 사전 튜닝 필요
- Calendar·업무관리 서비스가 없어 일정·담당자·의존 관계를 독립 시스템으로 교차 검증하지 못함

다음 개발 단계도 신규 접점 확장보다 실제 더미 데이터 기반 검색·판정 코어 검증을 우선합니다.

## Versioning

이 프로젝트는 [Semantic Versioning](https://semver.org/)을 따릅니다.

### v0.5.2-alpha.1 — 검색 코어 정리·단건 검색 가속

> Date: 2026.07.30

- 기존 답변 계약을 유지하면서 검색 점수 계산과 후보 조회를 분리했습니다.
- 단건 사실 질문은 pgvector와 PostgreSQL 텍스트 후보를 먼저 수집해 Python 재계산 범위를 줄입니다.
- 전체 목록은 정확한 개수와 누락 방지를 위해 기존 완전 탐색을 유지합니다.
- 검색 단계별 처리시간과 근거 문맥 예산을 기록·제한합니다.
- 검색 결과 동점과 다중 출처 보정 순서를 고정해 재현성을 높였습니다.

### v0.5.1-alpha.1 — 작성 요청·검색 범위 계약

> Date: 2026.07.30

- 사실 조회와 이메일·문서 작성 요청을 결정적으로 구분합니다.
- 특정 회사명이 없는 협업 제안 템플릿은 관련 제안 전체의 공통 패턴을 종합합니다.
- 재검색은 최초에 확정한 대상·범위를 벗어나지 않습니다.
- 근거 충돌은 보존하되 작성 결과물은 검증 보고서와 분리해 표시합니다.
- 전체 종합 근거 수를 제한해 기존 검색 속도와 비용을 보호합니다.

### v0.5.0-alpha.1 — 답변별 근거·범위 지정 검색

> Date: 2026.07.30

- 답변마다 당시 사용한 근거의 제목·위치·발췌·문서 버전을 스냅샷으로 보존합니다.
- 이전 답변을 선택하거나 인용 번호를 누르면 해당 답변의 근거 패널로 전환합니다.
- Notion 페이지에 상위 컬렉션 ID·이름·자료 유형·기록 시각을 함께 저장합니다.
- 전체 목록 질문은 일반 의미 검색과 분리해 자료 유형·기간·컬렉션 범위에서 레코드를 수집합니다.
- Notion 내부 기록 기준 답변과 외부 서비스의 실제 공개 완료 검증을 분리합니다.

### v0.4.0-alpha.4 — 적응형 답변·기간 목록 검색

> Date: 2026.07.30

- 확인됨 답변은 질문이 요구한 설명·목록·초안 형태를 유지합니다.
- 충돌·근거 부족·현재 상태에만 필요한 구조화 안내를 표시합니다.
- 원인 질문이 현재 상태 답변으로 바뀌지 않도록 상태 질문을 분리합니다.
- 월 단위 기간과 전체 목록 조건을 검색 계획에 반영하고 범위 밖 자료를 제외합니다.
- 검색에서 찾지 못한 사실을 데이터에 존재하지 않는 사실로 단정하지 않습니다.

### v0.4.0-alpha.3 — 답변 UI·근거 정렬

> Date: 2026.07.30

- 한 줄 결론·세부 내용·판단 근거·피비의 제안 순서로 답변 표시
- 사용자 친화적 확인 필요 상태와 향상된 본문 가독성
- 핵심 판단 근거 중복 제거 및 우측 패널의 관련 보조 원문 유지

### v0.4.0-alpha.2 — 결론 우선 판정 보고

> Date: 2026.07.30

- 한 줄 결론·판정 상태·역할별 사실·다음 확인 구조화
- 담당자와 금액의 역할 분리 및 출력 안전 검증
- 사실별 근거 번호와 모바일 답변 카드 UI

### v0.4.0-alpha.1 — 업무 상태·시간축 판정

> Date: 2026.07.30

- 완료 주장과 완료 승인을 분리
- 완료 이후 최신 수정 요청이 있으면 업무를 다시 열린 상태로 판정
- 상태 판정 스냅샷과 근거 시간축 저장
- 현재 상태·다음 확인·미니 시간축 UI 추가

### v0.3.0-alpha.1 — 하이브리드 검색·근거 판정

> Tag: `v0.3.0-alpha.1` | Date: 2026.07.25

- 대화 문맥 복원, 하이브리드 후보 검색, 문서 문맥 확장
- 다중 출처 충돌 탐지와 검색 실행 추적

### v0.2.1 — Gmail OAuth 안정화

> Tag: `v0.2.1` | Date: 2026.07.25

- Gmail OAuth PKCE `code_verifier`를 암호화된 일회성 상태로 관리
- 서버 재시작과 다중 인스턴스에서도 OAuth 상태 복원
- OAuth 상태 10분 만료와 변조 거부
- 권한 거부·토큰 교환 실패 사용자 안내
- 검색 코어 고도화 전 동작 기준선 확정

### v0.2.0 — Notion 지식공간 동기화

> Tag: `v0.2.0` | Date: 2026.07.25

- Notion 연결 저장·접근 범위 자동 발견
- 증분·자동 동기화
- 지원 형식 첨부파일 수집
- 연결 상태와 탐색 불완전성 표시

### v0.1.0 — Initial Local MVP

> Tag: `v0.1.0` | Date: 2026.07.25

- 프로젝트 단위 문서 업로드·검색
- Notion 페이지 지정 가져오기
- Gmail 본문·첨부 동기화
- PostgreSQL·pgvector와 근거 기반 답변 UI

### Branch Strategy

| Branch | Purpose |
|---|---|
| `main` | 검증을 통과한 기준 버전 |
| `develop` | 다음 버전 통합 |
| `feature/*` | 검색·연결·UI 기능 개발 |
| `fix/*` | 결함 수정 |

## Repository Policy

이 저장소는 Creative Flow의 비공개 제품 개발 저장소입니다. 별도 공개 결정이나 라이선스 부여 전에는 코드의 복제·배포·재사용을 허용하지 않습니다.
