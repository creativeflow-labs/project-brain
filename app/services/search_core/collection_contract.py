"""Conservative contract for deterministic video lists and counts only."""
import re


def video_operation(question: str) -> str:
    if any(word in question for word in ("내용", "분석", "요약", "자세히", "정리", "설명")):
        return "describe"
    if re.search(r"개수|갯수|몇\s*(?:개|편|건)|총\s*(?:몇|개|편|건)", question):
        return "count"
    if any(word in question for word in ("목록", "리스트", "제목")):
        return "list"
    return "describe"
