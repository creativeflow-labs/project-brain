from __future__ import annotations

import re
from dataclasses import dataclass

import httpx
from sqlalchemy.orm import Session

from app.config import get_settings
from app.services.ingestion import IngestionInput, ingest_document
from app.services.parser import ParsedSection, SUPPORTED_EXTENSIONS, parse_file_bytes

settings = get_settings()


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
    if block_type in {"to_do"}:
        return f"[{'x' if value.get('checked') else ' '}] {text}"
    if block_type in {"bulleted_list_item", "numbered_list_item"}:
        return f"- {text}"
    return text


@dataclass
class NotionSyncResult:
    pages: int = 0
    attachments: int = 0
    unchanged: int = 0
    errors: list[str] | None = None


class NotionConnector:
    def __init__(self, token: str) -> None:
        self.client = httpx.Client(
            timeout=30,
            headers={
                "Authorization": f"Bearer {token}",
                "Notion-Version": settings.notion_version,
                "Content-Type": "application/json",
            },
        )

    def _get(self, path_or_url: str) -> dict:
        url = (
            path_or_url
            if path_or_url.startswith("http")
            else f"https://api.notion.com/v1{path_or_url}"
        )
        response = self.client.get(url)
        response.raise_for_status()
        return response.json()

    def _all_blocks(self, block_id: str, depth: int = 0) -> list[dict]:
        if depth > 8:
            return []
        results: list[dict] = []
        cursor = None
        while True:
            suffix = f"?page_size=100&start_cursor={cursor}" if cursor else "?page_size=100"
            payload = self._get(f"/blocks/{block_id}/children{suffix}")
            for block in payload.get("results", []):
                results.append(block)
                if block.get("has_children"):
                    results.extend(self._all_blocks(block["id"], depth + 1))
            if not payload.get("has_more"):
                break
            cursor = payload.get("next_cursor")
        return results

    def sync_page(self, db: Session, project_id: str, page_value: str) -> NotionSyncResult:
        result = NotionSyncResult(errors=[])
        page_id = normalize_notion_id(page_value)
        page = self._get(f"/pages/{page_id}")
        title = "Notion 페이지"
        for prop in page.get("properties", {}).values():
            if prop.get("type") == "title":
                title = _rich_text(prop.get("title")) or title
                break

        blocks = self._all_blocks(page_id)
        sections: list[ParsedSection] = []
        attachment_blocks: list[dict] = []
        for block in blocks:
            block_type = block.get("type")
            if block_type in {"file", "pdf"}:
                attachment_blocks.append(block)
                continue
            text = _block_text(block).strip()
            if text:
                sections.append(
                    ParsedSection(text=text, locator=f"Notion 블록 {block['id'][:8]}")
                )

        page_text = "\n".join(section.text for section in sections).strip()
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
                    metadata={"last_edited_time": page.get("last_edited_time")},
                    sections=sections,
                ),
            )
            result.pages += int(changed)
            result.unchanged += int(not changed)

        for block in attachment_blocks:
            block_type = block["type"]
            payload = block[block_type]
            caption = _rich_text(payload.get("caption"))
            file_object = payload.get("file") or payload.get("external") or {}
            url = file_object.get("url")
            filename = caption or url.split("?")[0].split("/")[-1] if url else ""
            extension = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
            if not url or extension not in SUPPORTED_EXTENSIONS:
                continue
            try:
                download = httpx.get(url, timeout=60, follow_redirects=True)
                download.raise_for_status()
                parsed = parse_file_bytes(
                    download.content, filename, download.headers.get("content-type")
                )
                _, changed = ingest_document(
                    db,
                    IngestionInput(
                        project_id=project_id,
                        source_type="notion_attachment",
                        external_id=block["id"],
                        title=parsed.title,
                        text=parsed.text,
                        filename=filename,
                        mime_type=parsed.mime_type,
                        source_url=page.get("url"),
                        metadata={"parent_page_id": page_id},
                        warnings=parsed.warnings,
                        sections=parsed.sections,
                    ),
                )
                result.attachments += int(changed)
                result.unchanged += int(not changed)
            except Exception as exc:
                result.errors.append(f"{filename or block['id']}: {exc}")
        return result

