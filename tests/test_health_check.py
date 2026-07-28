"""源体检后台任务 + 停用源恢复。"""
import time

import pytest

from app.models import AppUser, Source
from app.services import health


@pytest.fixture()
def admin(db):
    u = db.query(AppUser).filter_by(role="admin").first()
    if not u:
        from app.auth import hash_password
        u = AppUser(username="admin_hc", display_name="admin_hc",
                    password_hash=hash_password("x"), role="admin")
        db.add(u); db.flush()
    return u


# ---------------- 恢复被误停用的源 ----------------

def test_restore_reenables_and_clears_failures(db, need):
    src = Source(name="被误停的源", kind="page", adapter="generic_rss", credibility="S3",
                 tier="B", lifecycle="retired", serves_needs=[need.id],
                 entry_url="https://restore.example.com/c", fail_streak=5,
                 adapter_config={"auto_merged": True},
                 note="某某 [自动查重:并入同采集目标的源]")
    db.add(src); db.flush()
    out = health.restore(db, src.id)
    assert out.lifecycle == "active"
    assert out.fail_streak == 0                       # 清零,避免刚恢复又被历史计数停用
    assert "auto_merged" not in (out.adapter_config or {})   # 不再被启动查重自动并掉
    assert out.adapter_config.get("manually_restored") is True
    assert "[自动查重" not in (out.note or "")


def test_restore_missing_source_returns_none(db):
    assert health.restore(db, 99999999) is None


def test_restore_endpoint(db, need, admin):
    from app.api.routes import restore_source
    src = Source(name="待恢复", kind="page", adapter="generic_rss", credibility="S3", tier="B",
                 lifecycle="retired", serves_needs=[need.id], fail_streak=3,
                 entry_url="https://re2.example.com/c")
    db.add(src); db.flush()
    out = restore_source(src.id, db, admin)
    assert out["lifecycle"] == "active" and out["fail_streak"] == 0


# ---------------- 体检后台任务与进度 ----------------

def test_health_status_shape_when_idle():
    st = health.status()
    assert isinstance(st, dict) and "running" in st


def test_health_check_runs_and_reports_progress(db, need, monkeypatch):
    """体检在后台跑,进度可查(此前在浏览器同步跑,源多就像"没反应")。"""
    from app.api import routes

    def _fake(source_id, q=None, mark=False, db=None, _=None):
        return {"ok": True, "count": 3, "hint": "能抓到内容", "retired": False}

    monkeypatch.setattr(routes, "test_fetch_source", _fake)
    for i in range(3):
        db.add(Source(name=f"体检源{i}", kind="page", adapter="generic_rss", credibility="S3",
                      tier="B", lifecycle="active", serves_needs=[need.id],
                      entry_url=f"https://hc{i}.example.com/c"))
    db.commit()

    st = health.start(need.id)
    assert st["running"] is True
    for _ in range(100):                     # 等后台跑完(最多 ~10s)
        if not health.status().get("running"):
            break
        time.sleep(0.1)
    fin = health.status()
    assert fin["running"] is False and fin["finished_at"]
    assert fin["total"] >= 3 and fin["done"] == fin["total"]
    assert fin["ok"] >= 3
    assert len(fin["results"]) == fin["total"]


def test_health_check_is_idempotent_while_running(monkeypatch):
    """已有体检在跑时再点不会起第二个。"""
    health._state.clear(); health._state["running"] = True
    try:
        st = health.start("sec_events")
        assert st["running"] is True
    finally:
        health._state.clear(); health._state["running"] = False
