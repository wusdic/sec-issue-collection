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


def test_out_of_scope_detection():
    from app.services.pipeline import _is_out_of_scope
    # 内容治理/名单/政策 → 非安全范畴
    assert _is_out_of_scope({}, "中央网信办部署开展'清朗·未成年人网络保护'专项行动", "...")
    assert _is_out_of_scope({}, "关于发布第十八批深度合成服务算法备案信息的公告", "...")
    assert _is_out_of_scope({"record_type": "不该入库"}, "任意", "...")
    # 带明确安全要素的不算(如"清朗行动查处一起数据泄露")
    assert not _is_out_of_scope({}, "某公司数据泄露被黑客勒索", "...")
    assert not _is_out_of_scope({}, "关于BlackMoon僵尸网络的风险提示", "...")


def test_out_of_scope_doc_not_evented(db, need, monkeypatch):
    """粗筛漏网的内容治理稿(强制通过粗筛),抽取后被范畴闸门过滤,不建事件。"""
    from app.models import Event, RawDocument, Source
    from app.services import pipeline
    # 模拟粗筛漏判为相关,考验抽取后的兜底闸门
    monkeypatch.setattr(pipeline, "screen_document",
                        lambda *a, **k: {"is_candidate": True, "confidence": 0.9, "reason": "mock放行"})
    src = db.query(Source).first()
    doc = RawDocument(need_id=need.id, source_id=src.id, url="https://cac.gov.cn/x/ql.htm",
                      url_normalized="https://cac.gov.cn/x/ql.htm", is_primary=True,
                      title="中央网信办部署'清朗·整治AI应用乱象'专项行动",
                      content_text="为营造清朗网络空间,中央网信办部署专项行动整治AI应用乱象……",
                      screen_status="pending")
    db.add(doc); db.flush()
    before = db.query(Event).count()
    r = pipeline.process_document(db, need, doc)
    assert r["action"] == "screened_out"
    assert db.query(Event).count() == before          # 未建事件
    assert "非网络安全" in doc.screen_reason


def test_placeholder_source_url_sanitized(db, need):
    from app.models import RawDocument, Source
    from app.services.events import create_draft
    src = db.query(Source).first()
    doc = RawDocument(need_id=need.id, source_id=src.id, url="https://real.example.com/a.htm",
                      url_normalized="https://real.example.com/a.htm", title="某银行数据泄露",
                      content_text="...", is_primary=True)
    db.add(doc); db.flush()
    p = {"title": "某银行数据泄露", "org_name": "某银行",
         "sources": [{"url_or_doc_number": "http://www.cac.gov.cn/c_XXXXX.htm"}]}
    ev = create_draft(db, need.id, p, doc=doc, source_credibility="S1")
    assert ev.payload["sources"][0]["url_or_doc_number"] == "https://real.example.com/a.htm"
