from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Project Brain"
    database_url: str = "sqlite:///./data/project_brain.db"
    upload_dir: Path = Path("./data/uploads")
    max_upload_mb: int = 20

    openai_api_key: str = ""
    openai_chat_model: str = "gpt-5.6-terra"
    openai_embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = 384
    search_candidate_limit: int = 80
    search_rerank_limit: int = 36
    search_evidence_limit: int = 12
    search_collection_evidence_limit: int = 24
    search_parent_max_chars: int = 12000
    search_history_messages: int = 8
    search_max_retries: int = 1

    notion_version: str = "2026-03-11"
    notion_max_pages_per_sync: int = 2000
    notion_scheduler_poll_seconds: int = 60
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = "http://localhost:8000/api/connectors/gmail/callback"
    token_encryption_key: str = ""
    token_key_file: Path = Path("./data/secrets/token-encryption.key")

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @property
    def is_postgres(self) -> bool:
        return self.database_url.startswith("postgresql")


@lru_cache
def get_settings() -> Settings:
    return Settings()
