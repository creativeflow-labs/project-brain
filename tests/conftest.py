import os
from pathlib import Path

os.environ["DATABASE_URL"] = "sqlite:///./data/test_project_brain.db"
os.environ["UPLOAD_DIR"] = "./data/test_uploads"
os.environ["OPENAI_API_KEY"] = ""


def pytest_sessionfinish(session, exitstatus):
    test_db = Path("./data/test_project_brain.db")
    if test_db.exists():
        test_db.unlink()

