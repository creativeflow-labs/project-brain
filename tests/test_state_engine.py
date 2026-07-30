from app.db import SessionLocal, init_db
from app.models import Document, Project
from app.services.ingestion import IngestionInput, ingest_document
from app.services.search_core.state_engine import assess_current_state
from app.services.search_core.types import EvidencePacket, QueryPlan


def _packet(document: Document, text: str, score: float = 0.9) -> EvidencePacket:
    return EvidencePacket(
        document=document,
        anchor_chunks=[],
        context_text=text,
        score=score,
    )


def _status_plan() -> QueryPlan:
    return QueryPlan(
        original_question="제니스 시안의 현재 상태는?",
        resolved_question="제니스 시안의 현재 상태는?",
        search_queries=["제니스 시안 현재 상태"],
        entity_terms=["제니스", "시안"],
        requested_field="status",
    )


def test_revision_after_completion_reopens_work():
    init_db()
    with SessionLocal() as db:
        project = Project(name="상태 시간축 테스트")
        db.add(project)
        db.commit()
        notion, _ = ingest_document(
            db,
            IngestionInput(
                project_id=project.id,
                source_type="notion",
                external_id="zenith-status",
                title="제니스 시안 업무 보드",
                text="제니스 2차 시안 상태: 완료",
                metadata={"last_edited_time": "2026-07-20T09:00:00Z"},
            ),
        )
        gmail, _ = ingest_document(
            db,
            IngestionInput(
                project_id=project.id,
                source_type="gmail",
                external_id="zenith-revision",
                title="제니스 2차 시안 피드백",
                text="고객이 제니스 2차 시안의 타깃 연령대를 수정해 달라고 요청했습니다.",
                metadata={"date": "Wed, 22 Jul 2026 14:00:00 +0900"},
            ),
        )

        result = assess_current_state(
            _status_plan(),
            [
                _packet(notion, "제니스 2차 시안 상태: 완료"),
                _packet(gmail, "고객이 제니스 2차 시안의 타깃 연령대를 수정해 달라고 요청했습니다."),
            ],
        )

        assert result is not None
        assert result.state == "revision_requested"
        assert result.evidence_status == "supported"
        assert result.decisive_document_ids == [gmail.id]
        assert notion.id in result.stale_document_ids


def test_completion_record_without_approval_is_only_a_claim():
    init_db()
    with SessionLocal() as db:
        project = Project(name="완료 주장 테스트")
        db.add(project)
        db.commit()
        notion, _ = ingest_document(
            db,
            IngestionInput(
                project_id=project.id,
                source_type="notion",
                external_id="claimed-done",
                title="작업 보드",
                text="상태: 완료",
                metadata={"last_edited_time": "2026-07-25T09:00:00Z"},
            ),
        )

        result = assess_current_state(_status_plan(), [_packet(notion, "상태: 완료")])

        assert result is not None
        assert result.state == "completion_claimed"
        assert "승인" in (result.next_action or "")


def test_explicit_final_approval_verifies_completion():
    init_db()
    with SessionLocal() as db:
        project = Project(name="승인 완료 테스트")
        db.add(project)
        db.commit()
        gmail, _ = ingest_document(
            db,
            IngestionInput(
                project_id=project.id,
                source_type="gmail",
                external_id="final-approval",
                title="최종 검수 회신",
                text="고객이 최종 승인했습니다.",
                metadata={"date": "Mon, 27 Jul 2026 12:00:00 +0900"},
            ),
        )

        result = assess_current_state(
            _status_plan(), [_packet(gmail, "고객이 최종 승인했습니다.")]
        )

        assert result is not None
        assert result.state == "completed"
        assert result.next_action is None


def test_same_time_cross_source_states_are_reported_as_conflict():
    init_db()
    with SessionLocal() as db:
        project = Project(name="동시점 충돌 테스트")
        db.add(project)
        db.commit()
        notion, _ = ingest_document(
            db,
            IngestionInput(
                project_id=project.id,
                source_type="notion",
                external_id="same-time-done",
                title="업무 보드",
                text="상태: 완료",
                metadata={"last_edited_time": "2026-07-28T01:00:00Z"},
            ),
        )
        gmail, _ = ingest_document(
            db,
            IngestionInput(
                project_id=project.id,
                source_type="gmail",
                external_id="same-time-revision",
                title="수정 회신",
                text="고객이 수정을 요청했습니다.",
                metadata={"date": "Tue, 28 Jul 2026 01:00:00 +0000"},
            ),
        )

        result = assess_current_state(
            _status_plan(),
            [_packet(notion, "상태: 완료"), _packet(gmail, "고객이 수정을 요청했습니다.")],
        )

        assert result is not None
        assert result.state == "unknown"
        assert result.evidence_status == "conflict"
        assert {notion.id, gmail.id} == set(result.decisive_document_ids)


def test_proposal_is_not_misclassified_as_completed():
    init_db()
    with SessionLocal() as db:
        project = Project(name="제안 상태 테스트")
        db.add(project)
        db.commit()
        gmail, _ = ingest_document(
            db,
            IngestionInput(
                project_id=project.id,
                source_type="gmail",
                external_id="proposal-only",
                title="협업 제안",
                text="유료 협업을 제안드리며 금액과 일정을 논의하고 싶습니다.",
                metadata={"date": "2026-07-28"},
            ),
        )

        result = assess_current_state(
            _status_plan(),
            [_packet(gmail, "유료 협업을 제안드리며 금액과 일정을 논의하고 싶습니다.")],
        )

        assert result is not None
        assert result.state == "unknown"
        assert result.evidence_status == "insufficient"
