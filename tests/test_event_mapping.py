"""抽取落库规约:嵌套/异形字段→标量列容错;通报情报单独归类。"""
from datetime import date

from app.services import events as ev


def test_to_date_variants():
    assert ev._to_date({"date": "2026-07-21", "precision": "日"}) == date(2026, 7, 21)
    assert ev._to_date({"value": "2022-07-01"}) == date(2022, 7, 1)     # 键名是 value 也能取
    assert ev._to_date("2026-06") == date(2026, 6, 1)                    # 只到月
    assert ev._to_date("2026") == date(2026, 1, 1)                       # 只到年
    assert ev._to_date("2026-06-01T12:00:00") == date(2026, 6, 1)        # 带时间
    assert ev._to_date({"date": None, "raw_text": "近期"}) is None       # 无年份→None,不造脏值
    assert ev._to_date("未披露") is None
    assert ev._to_date(None) is None


def test_scalar_severity():
    assert ev._scalar_severity({"level": "一般", "auto_suggested": True}) == "一般"
    assert ev._scalar_severity("重大") == "重大"
    assert ev._scalar_severity(None) is None


def test_infer_record_type():
    # 有单一受害方 → 单一事件
    assert ev._infer_record_type({"org_name": "某银行", "title": "某银行遭勒索"}) == "单一事件"
    # LLM 显式给出
    assert ev._infer_record_type({"record_type": "通报情报"}) == "通报情报"
    # 无受害方 + 通报特征 → 通报情报
    assert ev._infer_record_type({"org_name": "未披露",
                                  "title": "上半年网络安全态势通报",
                                  "consequences": ["数据泄露"]}) == "通报情报"


def test_sync_columns_tolerates_weird_shapes(db, need):
    from app.models import Event
    p = {"title": "季度网络安全态势统计通报", "org_name": "未披露",
         "occurred_date": {"value": "2026-06-30"},          # 异形键
         "severity": {"level": "未定级", "basis": "汇总通报"},
         "consequences": ["数据泄露", "服务降级"]}
    e = Event(event_id="SEC-MAP-1", need_id=need.id, payload=p, status="draft")
    ev._sync_columns(e)                                     # 不应抛异常
    assert e.occurred_date == date(2026, 6, 30)             # value 键也落库成功
    assert e.severity == "未定级"
    assert e.record_type == "通报情报"                       # 自动归类为通报情报


def test_create_draft_sets_record_type(db, need):
    from app.services.events import create_draft
    p = {"title": "某医院数据泄露", "org_name": "某三甲医院",
         "occurred_date": {"date": "2026-07-01", "precision": "日"},
         "severity": {"level": "较大"}}
    e = create_draft(db, need.id, p, doc=None, source_credibility="S1")
    assert e.record_type == "单一事件" and e.severity == "较大"
    assert str(e.occurred_date) == "2026-07-01"
