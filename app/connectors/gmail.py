from __future__ import annotations

import base64
import html
import os
import re
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from sqlalchemy.orm import Session

from app.services.ingestion import IngestionInput, ingest_document
from app.services.parser import SUPPORTED_EXTENSIONS, ParsedSection, parse_file_bytes

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def _decode_base64url(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _strip_html(value: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", value)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p>", "\n\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"[ \t]+", " ", html.unescape(text)).strip()


def _header(payload: dict, name: str) -> str:
    for item in payload.get("headers", []):
        if item.get("name", "").lower() == name.lower():
            return item.get("value", "")
    return ""


def _walk_parts(part: dict):
    yield part
    for child in part.get("parts", []) or []:
        yield from _walk_parts(child)


@dataclass
class GmailSyncResult:
    messages: int = 0
    attachments: int = 0
    unchanged: int = 0
    skipped_attachments: int = 0
    errors: list[str] = field(default_factory=list)


def credentials_to_dict(credentials: Credentials) -> dict:
    return {
        "token": credentials.token,
        "refresh_token": credentials.refresh_token,
        "token_uri": credentials.token_uri,
        "client_id": credentials.client_id,
        "client_secret": credentials.client_secret,
        "scopes": credentials.scopes,
    }


def credentials_from_dict(payload: dict) -> Credentials:
    return Credentials(
        token=payload.get("token"),
        refresh_token=payload.get("refresh_token"),
        token_uri=payload.get("token_uri"),
        client_id=payload.get("client_id"),
        client_secret=payload.get("client_secret"),
        scopes=payload.get("scopes") or GMAIL_SCOPES,
    )


class GmailConnector:
    def __init__(self, credentials: Credentials) -> None:
        self.credentials = credentials
        self.service = build("gmail", "v1", credentials=credentials, cache_discovery=False)

    def sync(
        self, db: Session, project_id: str, query: str, max_results: int
    ) -> GmailSyncResult:
        result = GmailSyncResult()
        listing = (
            self.service.users()
            .messages()
            .list(userId="me", q=query, maxResults=max_results)
            .execute()
        )
        for reference in listing.get("messages", []):
            try:
                self._sync_message(db, project_id, reference["id"], result)
            except Exception as exc:
                result.errors.append(f"{reference['id']}: {exc}")
        return result

    def _sync_message(
        self, db: Session, project_id: str, message_id: str, result: GmailSyncResult
    ) -> None:
        message = (
            self.service.users()
            .messages()
            .get(userId="me", id=message_id, format="full")
            .execute()
        )
        payload = message.get("payload", {})
        subject = _header(payload, "Subject") or "(제목 없음)"
        sender = _header(payload, "From")
        recipient = _header(payload, "To")
        date_value = _header(payload, "Date")
        body_plain: list[str] = []
        body_html: list[str] = []
        attachments: list[dict] = []

        for part in _walk_parts(payload):
            mime = part.get("mimeType", "")
            filename = part.get("filename", "")
            body = part.get("body", {})
            if filename and (body.get("attachmentId") or body.get("data")):
                attachments.append(part)
            elif mime == "text/plain" and body.get("data"):
                body_plain.append(_decode_base64url(body["data"]).decode("utf-8", errors="replace"))
            elif mime == "text/html" and body.get("data"):
                body_html.append(_strip_html(_decode_base64url(body["data"]).decode("utf-8", errors="replace")))

        body_text = "\n\n".join(body_plain).strip() or "\n\n".join(body_html).strip()
        if body_text:
            locator = f"이메일 {date_value}" if date_value else "이메일 본문"
            _, changed = ingest_document(
                db,
                IngestionInput(
                    project_id=project_id,
                    source_type="gmail",
                    external_id=message_id,
                    title=subject,
                    text=body_text,
                    mime_type="message/rfc822",
                    source_url=f"https://mail.google.com/mail/u/0/#all/{message_id}",
                    metadata={
                        "thread_id": message.get("threadId"),
                        "from": sender,
                        "to": recipient,
                        "date": date_value,
                    },
                    sections=[ParsedSection(text=body_text, locator=locator)],
                ),
            )
            result.messages += int(changed)
            result.unchanged += int(not changed)

        for part_index, part in enumerate(attachments):
            filename = part.get("filename", "")
            extension = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
            if extension not in SUPPORTED_EXTENSIONS:
                result.skipped_attachments += 1
                continue
            body = part.get("body", {})
            if body.get("data"):
                file_bytes = _decode_base64url(body["data"])
            else:
                attachment = (
                    self.service.users()
                    .messages()
                    .attachments()
                    .get(userId="me", messageId=message_id, id=body["attachmentId"])
                    .execute()
                )
                file_bytes = _decode_base64url(attachment["data"])
            parsed = parse_file_bytes(file_bytes, filename, part.get("mimeType"))
            _, changed = ingest_document(
                db,
                IngestionInput(
                    project_id=project_id,
                    source_type="gmail_attachment",
                    external_id=f"{message_id}:{body.get('attachmentId', part_index)}",
                    title=parsed.title,
                    text=parsed.text,
                    filename=filename,
                    mime_type=parsed.mime_type,
                    source_url=f"https://mail.google.com/mail/u/0/#all/{message_id}",
                    metadata={"message_id": message_id, "subject": subject, "from": sender},
                    warnings=parsed.warnings,
                    sections=parsed.sections,
                ),
            )
            result.attachments += int(changed)
            result.unchanged += int(not changed)

