"""红线用例:发布校验(confirmed 无 S1/S2 来源拒发)、双签、口径回归。"""
import pytest

from app.services.events import PublishError, create_draft, publish
from app.services.llm import MockLLM
from app.services.review import ReviewError, approve
from app.services import kpi


def _minimal_payload(credibility="S3", confirmed=False):
    p = MockLLM()._mock_extract("某市第三人民医院遭勒索软件攻击,系统瘫痪")
    p["sources"] = [{
        "ref_id": "SRC-001", "url_or_doc_number": "https://example.com/a",
        "publisher": "测试来源", "published_date": "2026-07-10", "credibility": credibility,
    }]
    if confirmed:
        p["loss_L4"] = {"confirmed_cny": {"point": 800000}, "status": "已确认",
                        "case_numbers": ["某网信罚〔2026〕1号"]}
    return p


def test_publish_blocked_without_s1(db, need, record_schema):
    """confirmed 金额 + 仅 S3 来源 → 拒绝发布。"""
    ev = create_draft(db, need.id, _minimal_payload("S3", confirmed=True), source_credibility="S3")
    with pytest.raises(PublishError, match="红线"):
        publish(db, ev, record_schema)


def test_publish_ok_with_s1(db, need, record_schema):
    """confirmed 金额 + S1 来源 + 双人复核 → 发布成功。"""
    from app.models import AppUser
    ev = create_draft(db, need.id, _minimal_payload("S1", confirmed=True), source_credibility="S1")
    r1 = db.query(AppUser).filter_by(username="reviewer1").one()
    r2 = db.query(AppUser).filter_by(username="reviewer2").one()
    t = approve(db, ev.event_id, r1.id, record_schema)   # 一审 → 转二审
    assert t.stage == "second_review"
    t = approve(db, ev.event_id, r2.id, record_schema)   # 二审 → 发布
    assert t.stage == "published"
    assert ev.status == "published"
    assert not ev.payload["loss_L4"].get("pending_human")


def test_double_sign_same_person_blocked(db, need, record_schema):
    """双签红线:一审二审同一人 → 拒绝。"""
    from app.models import AppUser
    ev = create_draft(db, need.id, _minimal_payload("S1", confirmed=True), source_credibility="S1")
    r1 = db.query(AppUser).filter_by(username="reviewer1").one()
    approve(db, ev.event_id, r1.id, record_schema)
    with pytest.raises(ReviewError, match="双签"):
        approve(db, ev.event_id, r1.id, record_schema)


def test_no_confirmed_single_review_publishes(db, need, record_schema):
    """无确认金额 → 单人复核即发布。"""
    from app.models import AppUser
    ev = create_draft(db, need.id, _minimal_payload("S3", confirmed=False), source_credibility="S3")
    r1 = db.query(AppUser).filter_by(username="reviewer1").one()
    t = approve(db, ev.event_id, r1.id, record_schema)
    assert t.stage == "published"


def test_traceability_gate(db, need):
    """口径回归:库内全部 confirmed 均可回溯 → 报表放行。"""
    check = kpi.traceability_check(db, need.id)
    assert check["ok"], check
