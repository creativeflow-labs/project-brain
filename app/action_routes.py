"""Approval-gated execution wrapper; /api/ask and search core stay unchanged."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Project, MessageDelivery
from app.naverworks_routes import same_origin
from app.schemas import AskRequest
from app.services.message_delivery import (
    delivery_intent, load_sender, create_delivery, confirm_delivery, cancel_delivery, public_delivery,
)
from app.services.rag import answer_question
from app.services.delivery_text import plain_delivery_text

router = APIRouter(tags=["approved-actions"])


class ApprovalInput(BaseModel):
    project_id: str
    action_id: str
    approval_token: str = Field(min_length=32, max_length=200)


@router.post("/api/assistant/request", dependencies=[Depends(same_origin)])
def assistant_request(payload: AskRequest, db: Session = Depends(get_db)):
    if not db.get(Project, payload.project_id):
        raise HTTPException(404, "프로젝트를 찾을 수 없습니다.")
    intent = delivery_intent(payload.question)
    if intent is None:
        return {"kind": "answer", "result": answer_question(db, payload.project_id, payload.question, payload.top_k, payload.thread_id)}
    if intent.mode == "blocked":
        return {"kind": "notice", "message": intent.content}
    load_sender(db, payload.project_id)
    search_result = None
    text, warning = intent.content, ""
    if intent.mode == "search":
        search_result = answer_question(db, payload.project_id, intent.content, payload.top_k, payload.thread_id)
        text = plain_delivery_text(search_result.answer)
        if search_result.citations:
            text += "\n\n[근거]\n" + "\n".join(
                f"[{i}] {c.title}" + (f"\n{c.source_url}" if c.source_url else "")
                for i, c in enumerate(search_result.citations, 1))
        if search_result.evidence_status != "supported":
            warning = "검색 결과에 충돌 또는 근거 부족이 포함돼 있습니다. 전송문을 확인해 주세요."
    preview = create_delivery(db, payload.project_id, text, warning)
    return {"kind": "delivery_preview", "preview": preview, "search_result": search_result}


@router.post("/api/actions/confirm", dependencies=[Depends(same_origin)])
def confirm(payload: ApprovalInput, db: Session = Depends(get_db)):
    return confirm_delivery(db, payload.project_id, payload.action_id, payload.approval_token)


@router.post("/api/actions/cancel", dependencies=[Depends(same_origin)])
def cancel(payload: ApprovalInput, db: Session = Depends(get_db)):
    return cancel_delivery(db, payload.project_id, payload.action_id, payload.approval_token)


@router.get("/api/actions/{action_id}")
def status(action_id: str, project_id: str, db: Session = Depends(get_db)):
    action = db.get(MessageDelivery, action_id)
    if not action or action.project_id != project_id:
        raise HTTPException(404, "이 프로젝트의 전송 요청을 찾을 수 없습니다.")
    return public_delivery(action)
