"""每日简报 + 端到端诊断留痕。"""
from datetime import date, datetime

import pytest

from app.models import AppUser, Event, Lead, RunTrace
from app.services import diagnostics
from app.services import digest as digest_svc


@pytest.fixture()
def admin(db):
    u = db.query(AppUser).filter_by(role="admin").first()
    if not u:
        from app.auth import hash_password
        u = AppUser(username="admin_dg", display_name="admin_dg",
                    password_hash=hash_password("x"), role="admin")
        db.add(u); db.flush()
    return u


# ---------------- 诊断留痕 ----------------

def test_record_noop_without_session():
    # 无活跃会话:record 不报错、不写库
    diagnostics.record("llm", "should be dropped")
    assert not diagnostics.active()


def test_session_persists_traces(db):
    with diagnostics.session(job_id=None):
        diagnostics.set_ref("https://x.com/a")
        diagnostics.record("screen", "粗筛 0.80 相关", detail={"confidence": 0.8})
        diagnostics.record("llm", "LLM[extract] ok", detail={"raw_response": "{}"})
    # 会话结束后独立 DB 会话已 commit,可查到
    rows = db.query(RunTrace).filter_by(kind="screen").all()
    assert any(r.ref == "https://x.com/a" and r.summary.startswith("粗筛") for r in rows)


def test_llm_trace_captured_in_session(db, need):
    """MockLLM 调用也在会话内留痕(提示词+解析结果)。"""
    from app.services.llm import get_llm
    with diagnostics.session(job_id=None):
        get_llm().complete_json("TASK=screen", "某公司数据泄露被攻击")
    tr = db.query(RunTrace).filter_by(kind="llm").order_by(RunTrace.id.desc()).first()
    assert tr is not None and tr.detail["task"] == "screen"
    assert "parsed" in tr.detail and tr.detail["model"] == "mock"


# ---------------- 每日简报 ----------------

def _mk_event(db, need_id, sev="L4", industry="金融", org="某银行"):
    eid = f"SEC-TEST-{datetime.utcnow().timestamp()}"
    ev = Event(event_id=eid, need_id=need_id, payload={}, status="draft",
               industry_l1=industry, org_name=org, severity=sev,
               attack_types=["勒索软件"], consequences=["数据泄露"],
               confidence_overall="中", completeness_score=0.7)
    db.add(ev); db.flush()
    return ev


def test_digest_counts_today_events(db, need):
    _mk_event(db, need.id, sev="L5", industry="金融", org="A银行")
    _mk_event(db, need.id, sev="L3", industry="医疗", org="B医院")
    c = digest_svc.build_content(db, need.id, datetime.utcnow().date())
    assert c["events_total"] >= 2
    assert c["events_by_industry"].get("金融", 0) >= 1
    assert c["top_events"][0]["severity"] == "L5"  # 高严重度排前
    md = digest_svc.render_markdown(c)
    assert "安全事件日报" in md and "行业热点" in md


def test_digest_upsert_idempotent(db, need):
    day = datetime.utcnow().date()
    d1 = digest_svc.upsert(db, need.id, day)
    d2 = digest_svc.upsert(db, need.id, day)
    assert d1.id == d2.id   # 同天幂等,不重复建


def test_digest_yesterday_excludes_today_events(db, need):
    from datetime import timedelta
    _mk_event(db, need.id, org="今天的事件")
    y = datetime.utcnow().date() - timedelta(days=1)
    c = digest_svc.build_content(db, need.id, y)
    assert c["events_total"] == 0  # 昨天的简报不含今天新建的事件
