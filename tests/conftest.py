import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_tmp = tempfile.mkdtemp(prefix="sec-test-")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/test.db"
os.environ["ARCHIVE_ROOT"] = f"{_tmp}/archive"
os.environ["LLM_PROVIDER"] = "mock"

import pytest  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _init():
    init_db()
    from app.auth import hash_password
    from app.models import AppUser
    from app.services import profiles
    db = SessionLocal()
    paths = profiles.default_sec_events_paths()
    cfg = profiles.load_profile_file(paths["profile"])
    np = profiles.register_need(db, cfg)
    profiles.load_dictionaries(db, np.id, paths["dictionaries"])
    profiles.load_keyword_set(db, np.id, paths["keywords"])
    profiles.load_seed_sources(db, np.id, paths["sources"])
    for uname, role in [("editor1", "editor"), ("reviewer1", "reviewer"), ("reviewer2", "reviewer")]:
        db.add(AppUser(username=uname, display_name=uname,
                       password_hash=hash_password("x"), role=role))
    db.commit()
    db.close()


@pytest.fixture()
def db():
    s = SessionLocal()
    yield s
    s.rollback()
    s.close()


@pytest.fixture()
def need(db):
    from app.models import NeedProfile
    return db.get(NeedProfile, "sec_events")


@pytest.fixture()
def record_schema():
    from app.config import settings
    from app.services.extraction import load_record_schema
    return load_record_schema(settings.schema_dir / "event.schema.json")
