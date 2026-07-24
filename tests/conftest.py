import os
from pathlib import Path

from cryptography.fernet import Fernet

os.environ["DATABASE_URL"] = "sqlite:///./data/test_project_brain.db"
os.environ["UPLOAD_DIR"] = "./data/test_uploads"
os.environ["OPENAI_API_KEY"] = ""
os.environ["TOKEN_ENCRYPTION_KEY"] = Fernet.generate_key().decode("utf-8")
os.environ["NOTION_SCHEDULER_POLL_SECONDS"] = "3600"


def pytest_sessionfinish(session, exitstatus):
    test_db = Path("./data/test_project_brain.db")
    if test_db.exists():
        test_db.unlink()
