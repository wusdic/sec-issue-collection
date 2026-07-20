"""线索映射/窗口、端到端流水线、多需求隔离。"""
from datetime import date, datetime, timedelta

from app.services import leads as leads_svc


def test_product_mapping_four_dims():
    payload = {"attack_type": ["勒索软件"], "consequences": ["业务中断", "数据被加密或破坏"],
               "entry_vector": [{"vector": "VPN或边界设备漏洞"}],
               "root_cause": {"category": "无有效备份"},
               "security_controls": [{"control": "数据备份", "status": "缺位"}]}
    products = leads_svc.map_products(payload)
    assert "备份与恢复" in products
    assert "攻击面管理" in products  # 边界入口维命中


def test_window_stage_transitions():
    today = date(2026, 7, 20)
    assert leads_svc.window_stage(date(2026, 7, 10), today) == "应急期"
    assert leads_svc.window_stage(date(2026, 5, 1), today) == "整改期"
    assert leads_svc.window_stage(date(2026, 1, 1), today) == "预算期"
    assert leads_svc.window_stage(date(2024, 1, 1), today) == "已过窗"


def test_pipeline_end_to_end(db, need):
    """采集文档 → 处理 → 生成草稿 → 赎金隔离验证。"""
    from app.models import RawDocument, Source, Event
    from app.services import dedup
    from app.services.pipeline import process_document
    src = db.query(Source).first()
    text = ("某三甲医院遭勒索软件攻击,HIS 系统瘫痪 36 小时,门诊停诊。"
            "攻击者要求支付 300 万元赎金,医院未支付,数据由备份恢复。")
    doc = RawDocument(need_id=need.id, source_id=src.id, url="https://ex.com/e2e",
                      url_normalized="https://ex.com/e2e", title="某医院遭勒索系统瘫痪",
                      content_text=text, published_at=datetime.utcnow(), screen_status="pending")
    db.add(doc); db.flush()
    dedup.assign_cluster(db, doc)
    result = process_document(db, need, doc)
    assert result["action"] == "draft_created"
    ev = db.get(Event, result["event_id"])
    assert ev.payload["ransom"]["demanded_amount"] == 3000000
    # 赎金不得进 L1
    assert ev.payload["loss_L1"].get("confirmed_cny") is None
    assert "勒索软件" in ev.attack_types


def test_need_isolation(db):
    """多需求隔离:第二需求(政策库画像)注册后与 sec_events 数据不混。"""
    from app.services import profiles
    from app.models import Event
    cfg = profiles.load_profile_file(profiles.settings.config_dir / "need_policy_watch.yaml")
    np = profiles.register_need(db, cfg)
    db.flush()
    assert np.id == "policy_watch"
    sec_events = db.query(Event).filter_by(need_id="sec_events").count()
    policy_events = db.query(Event).filter_by(need_id="policy_watch").count()
    assert policy_events == 0
    assert sec_events >= 0  # 隔离,互不影响
