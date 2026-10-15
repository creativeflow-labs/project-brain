"""Narrow product identity responses, not a general-purpose intent router."""
from __future__ import annotations

import re


def assistant_identity_answer(question: str) -> str | None:
    compact = re.sub(r"[\s,.!?。！？]", "", question.lower())
    compact = re.sub(r"^(?:안녕하세요|안녕|얀녕|hello|hi)", "", compact)
    if compact not in {
        "너이름이뭐니", "너이름이뭐야", "너의이름이뭐니", "너의이름이뭐야",
        "너는누구야", "넌누구야", "이름이뭐니", "이름이뭐야",
    }:
        return None
    return "저는 피비(PB), Project Brain입니다. 팀의 업무를 기억하고 검증하는 프로젝트 AI 비서입니다."
