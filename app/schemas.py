from datetime import datetime

from pydantic import BaseModel, Field


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=1000)


class ProjectOut(BaseModel):
    id: str
    name: str
    description: str
    created_at: datetime

    model_config = {"from_attributes": True}


class DocumentOut(BaseModel):
    id: str
    source_type: str
    title: str
    filename: str | None
    mime_type: str
    source_url: str | None
    warnings_json: list
    created_at: datetime

    model_config = {"from_attributes": True}


class AskRequest(BaseModel):
    project_id: str
    question: str = Field(min_length=2, max_length=2000)
    top_k: int = Field(default=6, ge=1, le=12)


class Citation(BaseModel):
    document_id: str
    title: str
    source_type: str
    locator: str | None
    source_url: str | None
    excerpt: str
    score: float


class AskResponse(BaseModel):
    answer: str
    mode: str
    citations: list[Citation]


class NotionImportRequest(BaseModel):
    project_id: str
    integration_token: str = Field(min_length=10)
    page_ids: list[str] = Field(min_length=1, max_length=20)


class GmailSyncRequest(BaseModel):
    project_id: str
    query: str = Field(default="newer_than:30d", max_length=500)
    max_results: int = Field(default=30, ge=1, le=100)

