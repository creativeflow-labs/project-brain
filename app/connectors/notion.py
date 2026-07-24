from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Document
from app.services.ingestion import IngestionInput, ingest_document
from app.services.parser import ParsedSection, SUPPORTED_EXTENSIONS, parse_file_bytes

settings = get_settings()


class NotionApiError(RuntimeError):
    def __init__(self, message: str, status_code: int, code: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


def normalize_notion_id(value: str) -> str:
    candidate = value.split("?")[0].rstrip("/").split("/")[-1]
    match = re.search(r"([0-9a-fA-F]{32})$", candidate.replace("-", ""))
    if not match:
        raise ValueError(f"Notion 페이지 ID를 확인할 수 없습니다: {value}")
    raw = match.group(1)
    return f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}"


def _rich_text(items: list[dict] | None) -> str:
    return "".join(item.get("plain_text", "") for item in (items or []))


def _block_text(block: dict) -> str:
    block_type = block.get("type", "")
    value = block.get(block_type, {})
    text = _rich_text(value.get("rich_text"))
    if block_type == "to_do":
        return f"[{'x' if value.get('checked') else ' '}] {text}"
    if block_type in {"bulleted_list_item", "numbered_list_item"}:
        return f"- {text}"
    return text


def _page_title(page: dict) -> str:
    for prop in page.get("properties", {}).values():
        if prop.get("type") == "title":
            return _rich_text(prop.get("title")) or "Notion 페이지"
    return "Notion 페이지"


def _property_text(name: str, prop: dict) -> str:
    prop_type = prop.get("type", "")
    value = prop.get(prop_type)
    rendered = ""
    if prop_type in {"title", "rich_text"}:
        rendered = _rich_text(value)
    elif prop_type in {"number", "url", "email", "phone_number"}:
        rendered = "" if value is None else str(value)
    elif prop_type in {"select", "status"}:
        rendered = (value or {}).get("name", "")
    elif prop_type == "multi_select":
        rendered = ", ".join(item.get("name", "") for item in (value or []))
    elif prop_type == "date" and value:
        rendered = " ~ ".join(
            part for part in [value.get("start", ""), value.get("end", "")] if part
        )
    elif prop_type == "checkbox":
        rendered = "예" if value else "아니오"
    elif prop_type == "people":
        rendered = ", ".join(
            item.get("name") or item.get("id", "") for item in (value or [])
        )
    elif prop_type == "relation":
        rendered = ", ".join(item.get("id", "") for item in (value or []))
    elif prop_type == "formula" and value:
        formula_type = value.get("type", "")
        formula_value = value.get(formula_type)
        rendered = "" if formula_value is None else str(formula_value)
    elif prop_type == "rollup" and value:
        rollup_type = value.get("type", "")
        rollup_value = value.get(rollup_type)
        rendered = "" if rollup_value is None else str(rollup_value)
    return f"{name}: {rendered}" if rendered else ""


def _filename_from_url(url: str) -> str:
    return unquote(Path(urlparse(url).path).name)


@dataclass
class NotionDiscoveryResult:
    pages: dict[str, dict] = field(default_factory=dict)
    data_sources: int = 0
    incomplete: bool = False
    warnings: list[str] = field(default_factory=list)


@dataclass
class NotionPageSyncResult:
    pages: int = 0
    attachments: int = 0
    unchanged: int = 0
    removed: int = 0
    child_page_ids: set[str] = field(default_factory=set)
    warnings: list[str] = field(default_factory=list)


class NotionConnector:
    def __init__(
        self,
        token: str,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.sleep = sleep
        self.client = httpx.Client(
            timeout=30,
            transport=transport,
            headers={
                "Authorization": f"Bearer {token}",
                "Notion-Version": settings.notion_version,
                "Content-Type": "application/json",
                "User-Agent": "Project-Brain/0.2",
            },
        )

    def close(self) -> None:
        self.client.close()

    def _request(self, method: str, path: str, json: dict | None = None) -> dict:
        url = f"https://api.notion.com/v1{path}"
        for attempt in range(5):
            try:
                response = self.client.request(method, url, json=json)
            except httpx.RequestError as exc:
                if attempt == 4:
                    raise RuntimeError(f"Notion 네트워크 요청 실패: {exc}") from exc
                self.sleep(min(2**attempt, 8))
                continue
            if response.status_code == 429 or response.status_code >= 500:
                if attempt == 4:
                    break
                retry_after = response.headers.get("Retry-After")
                wait_seconds = float(retry_after) if retry_after else min(2**attempt, 8)
                self.sleep(max(wait_seconds, 0))
                continue
            if response.is_success:
                return response.json()
            break

        try:
            payload = response.json()
        except ValueError:
            payload = {}
        message = payload.get("message") or f"Notion API HTTP {response.status_code}"
        raise NotionApiError(message, response.status_code, payload.get("code", ""))

    def validate_token(self) -> dict:
        user = self._request("GET", "/users/me")
        return {
            "id": user.get("id", ""),
            "name": user.get("name") or user.get("type") or "Notion 연결",
            "type": user.get("type", ""),
        }

    def fetch_page(self, page_id: str) -> dict:
        return self._request("GET", f"/pages/{normalize_notion_id(page_id)}")

    def _search(self, object_type: str, max_items: int) -> tuple[list[dict], bool]:
        results: list[dict] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        incomplete = False
        while len(results) < max_items:
            body: dict = {
                "page_size": min(100, max_items - len(results)),
                "filter": {"property": "object", "value": object_type},
                "sort": {"direction": "descending", "timestamp": "last_edited_time"},
            }
            if cursor:
                body["start_cursor"] = cursor
            payload = self._request("POST", "/search", body)
            results.extend(payload.get("results", []))
            request_status = payload.get("request_status", {})
            if request_status and request_status.get("type") != "complete":
                incomplete = True
            if not payload.get("has_more"):
                break
            cursor = payload.get("next_cursor")
            if not cursor or cursor in seen_cursors:
                incomplete = True
                break
            seen_cursors.add(cursor)
        if len(results) >= max_items:
            incomplete = True
        return results[:max_items], incomplete

    def _query_data_source(self, data_source_id: str, max_items: int) -> list[dict]:
        results: list[dict] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        while len(results) < max_items:
            body: dict = {"page_size": min(100, max_items - len(results))}
            if cursor:
                body["start_cursor"] = cursor
            payload = self._request(
                "POST", f"/data_sources/{data_source_id}/query", body
            )
            results.extend(
                item for item in payload.get("results", []) if item.get("object") == "page"
            )
            if not payload.get("has_more"):
                break
            cursor = payload.get("next_cursor")
            if not cursor or cursor in seen_cursors:
                break
            seen_cursors.add(cursor)
        return results[:max_items]

    def discover_pages(self, max_pages: int) -> NotionDiscoveryResult:
        result = NotionDiscoveryResult()
        pages, pages_incomplete = self._search("page", max_pages)
        result.incomplete = pages_incomplete
        for page in pages:
            if page.get("id"):
                result.pages[page["id"]] = page

        remaining = max(0, max_pages - len(result.pages))
        if remaining == 0:
            result.incomplete = True
            return result

        data_sources, sources_incomplete = self._search("data_source", max_pages)
        result.data_sources = len(data_sources)
        result.incomplete = result.incomplete or sources_incomplete
        for source in data_sources:
            if len(result.pages) >= max_pages:
                result.incomplete = True
                break
            try:
                entries = self._query_data_source(
                    source["id"], max_pages - len(result.pages)
                )
                for page in entries:
                    result.pages[page["id"]] = page
            except Exception as exc:
                result.warnings.append(
                    f"데이터 소스 {source.get('id', '')[:8]} 조회 실패: {exc}"
                )
        return result

    def _all_blocks(self, block_id: str, depth: int = 0) -> list[dict]:
        if depth > 8:
            return []
        results: list[dict] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        while True:
            suffix = f"?page_size=100&start_cursor={cursor}" if cursor else "?page_size=100"
            payload = self._request("GET", f"/blocks/{block_id}/children{suffix}")
            for block in payload.get("results", []):
                results.append(block)
                if block.get("has_children") and block.get("type") not in {
                    "child_page",
                    "child_database",
                }:
                    results.extend(self._all_blocks(block["id"], depth + 1))
            if not payload.get("has_more"):
                break
            cursor = payload.get("next_cursor")
            if not cursor or cursor in seen_cursors:
                break
            seen_cursors.add(cursor)
        return results

    def _download_file(self, url: str) -> tuple[bytes, str]:
        maximum = settings.max_upload_mb * 1024 * 1024
        chunks: list[bytes] = []
        size = 0
        with httpx.stream(
            "GET",
            url,
            timeout=60,
            follow_redirects=True,
            headers={"User-Agent": "Project-Brain/0.2"},
        ) as response:
            response.raise_for_status()
            content_length = int(response.headers.get("Content-Length", "0") or 0)
            if content_length > maximum:
                raise ValueError(
                    f"첨부파일이 {settings.max_upload_mb}MB 제한을 초과했습니다."
                )
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > maximum:
                    raise ValueError(
                        f"첨부파일이 {settings.max_upload_mb}MB 제한을 초과했습니다."
                    )
                chunks.append(chunk)
            return b"".join(chunks), response.headers.get(
                "Content-Type", "application/octet-stream"
            )

    @staticmethod
    def _attachment_from_block(block: dict) -> dict | None:
        block_type = block.get("type")
        if block_type not in {"file", "pdf"}:
            return None
        payload = block.get(block_type, {})
        if payload.get("type") != "file" or not payload.get("file", {}).get("url"):
            return None
        caption = _rich_text(payload.get("caption"))
        url = payload["file"]["url"]
        return {
            "external_id": block["id"],
            "name": payload.get("name") or caption or _filename_from_url(url),
            "url": url,
        }

    @staticmethod
    def _attachments_from_properties(page: dict) -> list[dict]:
        attachments: list[dict] = []
        for property_id, prop in page.get("properties", {}).items():
            if prop.get("type") != "files":
                continue
            for index, item in enumerate(prop.get("files", [])):
                if item.get("type") != "file" or not item.get("file", {}).get("url"):
                    continue
                url = item["file"]["url"]
                attachments.append(
                    {
                        "external_id": f"{page['id']}:{property_id}:{index}",
                        "name": item.get("name") or _filename_from_url(url),
                        "url": url,
                    }
                )
        return attachments

    def sync_page(
        self, db: Session, project_id: str, page: dict
    ) -> NotionPageSyncResult:
        result = NotionPageSyncResult()
        page_id = page["id"]
        title = _page_title(page)
        blocks = self._all_blocks(page_id)
        sections: list[ParsedSection] = []
        property_lines = [
            rendered
            for name, prop in page.get("properties", {}).items()
            if (rendered := _property_text(name, prop))
        ]
        if property_lines:
            sections.append(
                ParsedSection(
                    text="\n".join(property_lines), locator="Notion 속성"
                )
            )

        attachment_refs = self._attachments_from_properties(page)
        for block in blocks:
            if block.get("type") == "child_page":
                result.child_page_ids.add(block["id"])
                continue
            attachment = self._attachment_from_block(block)
            if attachment:
                attachment_refs.append(attachment)
                continue
            text = _block_text(block).strip()
            if text:
                sections.append(
                    ParsedSection(text=text, locator=f"Notion 블록 {block['id'][:8]}")
                )

        page_text = "\n".join(section.text for section in sections).strip()
        existing_page = db.scalar(
            select(Document).where(
                Document.project_id == project_id,
                Document.source_type == "notion",
                Document.external_id == page_id,
            )
        )
        if page_text:
            _, changed = ingest_document(
                db,
                IngestionInput(
                    project_id=project_id,
                    source_type="notion",
                    external_id=page_id,
                    title=title,
                    text=page_text,
                    mime_type="text/markdown",
                    source_url=page.get("url"),
                    metadata={
                        "last_edited_time": page.get("last_edited_time"),
                        "child_page_ids": sorted(result.child_page_ids),
                    },
                    sections=sections,
                ),
            )
            result.pages += int(changed)
            result.unchanged += int(not changed)
        elif existing_page:
            db.delete(existing_page)
            db.commit()
            result.removed += 1

        active_attachment_ids: set[str] = set()
        for attachment in attachment_refs:
            filename = Path(attachment["name"]).name
            extension = (
                "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
            )
            if extension not in SUPPORTED_EXTENSIONS:
                continue
            active_attachment_ids.add(attachment["external_id"])
            try:
                content, content_type = self._download_file(attachment["url"])
                parsed = parse_file_bytes(content, filename, content_type)
                _, changed = ingest_document(
                    db,
                    IngestionInput(
                        project_id=project_id,
                        source_type="notion_attachment",
                        external_id=attachment["external_id"],
                        title=parsed.title,
                        text=parsed.text,
                        filename=filename,
                        mime_type=parsed.mime_type,
                        source_url=page.get("url"),
                        metadata={
                            "parent_page_id": page_id,
                            "last_edited_time": page.get("last_edited_time"),
                        },
                        warnings=parsed.warnings,
                        sections=parsed.sections,
                    ),
                )
                result.attachments += int(changed)
                result.unchanged += int(not changed)
            except Exception as exc:
                db.rollback()
                result.warnings.append(f"{filename}: {exc}")

        existing_attachments = db.scalars(
            select(Document).where(
                Document.project_id == project_id,
                Document.source_type == "notion_attachment",
                Document.metadata_json["parent_page_id"].as_string() == page_id,
            )
        ).all()
        for document in existing_attachments:
            if document.external_id not in active_attachment_ids:
                db.delete(document)
                result.removed += 1
        db.commit()
        return result
