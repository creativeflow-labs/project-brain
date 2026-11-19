"""Separate transport, recipient, operation and content before search or preview.

Conservative local parsing: uncertain instructions ask for clarification. Neither
the parser nor the search model can authorize or execute a message delivery.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class DeliveryIntent:
    mode: str
    content: str
    channel: str = "naverworks"
    recipient: str = "self"


QUOTED = re.compile(r'"[^"\n]*(?:\n[^"\n]*)*"|“[^”]*”|\'[^\']*\'|‘[^’]*’', re.S)
CHANNEL = re.compile(r"(?:네이버\s*웍스|naver\s*works)(?:\s*메신저)?|메신저", re.I)
SELF = re.compile(r"나에게|나한테|내게|본인에게")
SEND = re.compile(r"(?:전달|전송|보내)\s*(?:해\s*)?(?:줘|주세요|주십시오|주실래요|줄래(?:요)?|주시겠어요)|(?:전달|전송)해(?=[.!?。\s]|$)")
PROCESS = re.compile(r"정리|요약|검색|찾아|찾아서|조회")
QUERY = re.compile(r"알려|찾아|검색|정리|요약|리스트|제시|목록|개수|누구|무엇|얼마|\?")
OTHER_CHANNEL = re.compile(r"(?:카카오톡|카톡|슬랙|slack|텔레그램|telegram|팀즈|teams|이메일|지메일|gmail)\s*(?:메신저(?:로)?|(?:으로|로)(?!부터))", re.I)
CLARIFY = "찾은 내용을 정리해 전달할까요, 입력한 문장을 그대로 전달할까요? 원하는 작업을 알려주세요."


def _masked(text: str) -> str:
    # Keep offsets while ignoring quoted payload as executable instructions.
    return QUOTED.sub(lambda m: " " * len(m.group()), text)


def _replace_outside_quotes(text: str, pattern: re.Pattern, replacement: str = "") -> str:
    spans = list(pattern.finditer(_masked(text)))
    for match in reversed(spans):
        text = text[:match.start()] + replacement + text[match.end():]
    return text


def _unquote(text: str) -> str:
    text = text.strip()
    if QUOTED.fullmatch(text):
        return text[1:-1].strip()
    return text


def _is_query(text: str) -> bool:
    outside = _masked(text)
    return bool(QUERY.search(outside) and ("?" in outside or re.search(
        r"(?:해\s*줘|해\s*주세요|알려\s*줘|알려\s*주세요|무엇|누구|얼마)", outside)))


def delivery_intent(question: str) -> DeliveryIntent | None:
    question = question.strip()
    header, newline, rest = question.partition("\n")
    instruction = _masked(header)
    channel = CHANNEL.search(instruction)
    send = SEND.search(instruction)
    if not channel or not send:
        return None
    tail = instruction[send.end():].strip(" .!?。")
    if tail and not tail.startswith((":", "：")):
        # A quoted/reported command in an ordinary question is not a send.
        return None
    if OTHER_CHANNEL.search(instruction):
        return DeliveryIntent("blocked", "현재 승인 발송은 네이버웍스만 지원합니다. 사용할 채널을 확인해 주세요.")
    # Recipient is checked in the instruction, never inside the quoted body.
    if not SELF.search(instruction):
        return DeliveryIntent("blocked", "현재는 본인에게만 보낼 수 있습니다. 수신자를 본인으로 지정해 주세요.")
    receivers = re.findall(r"([가-힣A-Za-z0-9_.@]+)(?:에게|한테)", instruction)
    if any(receiver not in {"나", "본인"} for receiver in receivers):
        return DeliveryIntent("blocked", "다른 사람이나 복수 수신자는 아직 지원하지 않습니다. 본인에게만 보낼 수 있습니다.")

    # A separate body is content, not an extra channel/recipient instruction.
    body = rest.strip().lstrip("*- ") if newline else ""
    colon_at = instruction.find(":")
    if not body and colon_at >= 0:
        body = header[colon_at+1:].strip()
        instruction = instruction[:colon_at]
    processed = bool(PROCESS.search(instruction))
    verbatim = bool(re.search(r"그대로|문구\s*그대로|원문\s*그대로", instruction))
    if processed and verbatim:
        return DeliveryIntent("blocked", CLARIFY)

    if body:
        content = _unquote(body)
        if not content:
            return DeliveryIntent("blocked", "전달할 내용이나 검색할 주제를 알려주세요.")
        if verbatim:
            return DeliveryIntent("literal", content)
        if processed or _is_query(content):
            # Preserve complete queries, particularly the existing collection
            # fast path; do not prepend transport instructions to the search.
            query = content if _is_query(content) else f"{content} 정리해줘."
            return DeliveryIntent("search", query)
        return DeliveryIntent("literal", content)

    # Inline content: remove only instruction tokens outside quotations.
    content = _replace_outside_quotes(header, re.compile(CHANNEL.pattern + r"(?:으로|로|에서|에|를)?", re.I))
    content = _replace_outside_quotes(content, SELF)
    content = _replace_outside_quotes(content, SEND)
    content = re.sub(r"\s*[.!。]\s*$", "", content).strip()
    content = _replace_outside_quotes(content, re.compile(r"(?:정리|요약|검색|조회)(?:해서|하여|한\s*뒤|하고|해|한\s*후)?|찾아서|찾아"))
    content = _replace_outside_quotes(content, re.compile(r"그대로|문구\s*그대로|원문\s*그대로"))
    content = content.strip(" ,:：")
    if not content or re.fullmatch(r"(?:그|이|위|아래|앞의|방금)(?:\s*(?:내용|결과|답변|메시지|문장))?(?:을|를)?", content):
        return DeliveryIntent("blocked", "어떤 내용을 전달할지 확인이 필요합니다. 검색할 주제나 전달할 문장을 알려주세요.")
    if processed:
        # Quotes can mark a SUBJECT, not only literal message text. Keep the
        # operation outside them: '"운영목표"를 정리해서 전달' must retrieve.
        subject = re.sub(r"(?:을|를)\s*$", "", content)
        subject = _unquote(subject)
        return DeliveryIntent("search", f"{subject} 정리해줘.")
    if verbatim or QUOTED.fullmatch(content):
        return DeliveryIntent("literal", _unquote(content))
    if re.search(r"(?:이?라고|라는\s*문구를)\s*$", content):
        return DeliveryIntent("literal", _unquote(re.sub(r"(?:이?라고|라는\s*문구를)\s*$", "", content)))
    return DeliveryIntent("blocked", CLARIFY)
