"""去重三层、URL 工具、SimHash、画像校验。"""
from app.services import url_tools
from app.services.simhash import hamming, simhash64


def test_url_normalize_strips_tracking():
    a = url_tools.normalize_url("https://Example.com/a?utm_source=x&id=5#frag")
    b = url_tools.normalize_url("http://example.com:80/a?id=5")
    assert "utm_source" not in a
    assert a.split("://")[1] == b.split("://")[1]


def test_identity_key_domain_and_wechat():
    assert url_tools.identity_key_for("https://news.freebuf.com/x") == "freebuf.com"
    assert url_tools.identity_key_for("http://a.gov.cn/x") == "a.gov.cn"
    assert url_tools.identity_key_for("", wechat_account="安全内参") == "mp:安全内参"


def test_search_redirect_detected():
    assert url_tools.is_search_redirect("https://www.baidu.com/link?url=abc")
    assert not url_tools.is_search_redirect("https://freebuf.com/news/1.html")


def test_simhash_near_duplicate():
    a = simhash64("某医院遭勒索软件攻击系统瘫痪超过36小时门诊停诊")
    b = simhash64("某医院遭勒索软件攻击导致系统瘫痪超过36小时门诊停诊了")
    c = simhash64("证监会发布新规加强上市公司信息披露监管要求")
    assert hamming(a, b) <= 6
    assert hamming(a, c) > 10


def test_doc_cluster_marks_reposts(db, need):
    from datetime import datetime, timedelta
    from app.models import RawDocument, Source
    from app.services import dedup
    src = db.query(Source).first()
    base_text = "某银行核心系统遭黑客攻击导致交易中断三小时客户无法转账"
    d1 = RawDocument(need_id=need.id, source_id=src.id, url="https://s1.com/x1",
                     url_normalized="https://s1.com/x1", content_text=base_text,
                     published_at=datetime.utcnow() - timedelta(hours=2))
    db.add(d1); db.flush()
    dedup.assign_cluster(db, d1)
    d2 = RawDocument(need_id=need.id, source_id=src.id, url="https://s2.com/x2",
                     url_normalized="https://s2.com/x2", content_text=base_text + "。",
                     published_at=datetime.utcnow())
    db.add(d2); db.flush()
    dedup.assign_cluster(db, d2)
    assert d1.cluster_id == d2.cluster_id
    assert d1.is_primary and not d2.is_primary  # 先发布者为首发


def test_profile_validation_requires_benchmark():
    from app.services.profiles import validate_profile
    bad = {"need": {"id": "x"}, "record_schemas": [{"archetype": "事件型"}],
           "dictionaries": {"file": "d"}, "sources": {"seed_file": "s"},
           "update": {"a": 1}, "quality": {"model": "事实核实型"},
           "outputs": {"reports": []}, "compliance": {"collection_boundary": "仅公开渠道"},
           "benchmark": {}}
    errors = validate_profile(bad)
    assert any("基准" in e for e in errors)
