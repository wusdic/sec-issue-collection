"""借鉴同类项目后新增的能力:JSON 列表接口适配器、真实性验证、记录关系、飞书推送/导出、质量评分卡、GBK 解码。"""
import json
from datetime import datetime

from app.services import capabilities, need_ctx


class _FR:
    def __init__(self, html, ok=True, final_url="https://www.example.gov.cn/x"):
        self.ok, self.html, self.final_url, self.status, self.error = ok, html, final_url, 200 if ok else 500, None


def test_json_api_adapter_pages_and_maps_fields(monkeypatch):
    from app.services import adapters, fetcher
    pages = {1: {"list": [{"topic": "甲文件", "infourl": "/a.htm", "pubtime": "2026-08-01 10:00:00"},
                          {"topic": "乙文件", "infourl": "//www.example.gov.cn/b.htm", "pubtime": "2026-08-02"}]},
             2: {"list": [{"topic": "丙文件", "infourl": "https://other.cn/c", "pubtime": ""}]},
             3: {"list": []}}
    calls = []

    def fake_fetch(url, referer=None, **kw):
        calls.append(url)
        page = int(url.split("pageno=")[1].split("&")[0])
        return _FR(json.dumps(pages.get(page, {"list": []})))
    monkeypatch.setattr(fetcher, "fetch", fake_fetch)

    class Src:
        entry_url = "https://www.example.gov.cn/cms/JsonList"
        adapter_config = {"params": {"channelCode": "A1"}, "items_path": "list",
                          "fields": {"title": "topic", "url": "infourl", "published": "pubtime"}, "max_pages": 5}
        name, kind = "t", "page"
    ad = adapters.get_adapter(Src()) if hasattr(adapters, "get_adapter") and False else adapters.JsonApiAdapter(Src())
    items = ad.discover()
    assert [i.title for i in items] == ["甲文件", "乙文件", "丙文件"]
    assert items[0].url == "https://www.example.gov.cn/a.htm" and items[1].url.startswith("https://www.example.gov.cn/b")
    assert items[0].published.startswith("2026-08-01") and "channelCode=A1" in calls[0]
    assert ad.discover_page(9) is None and "json_api" in adapters._REGISTRY


def test_decode_body_handles_meta_gbk():
    from app.services.fetcher import decode_body

    class R:
        headers = {"content-type": "text/html"}
        content = ('<html><head><meta charset="gb2312"></head><body>某市网络安全通报</body></html>').encode("gb18030")
    assert "某市网络安全通报" in decode_body(R())

    class R2:
        headers = {"content-type": "text/html; charset=utf-8"}
        content = "正文".encode("utf-8")
    assert decode_body(R2()) == "正文"


def test_verify_text_levels_and_sensitive(db):
    from app.services import verify
    c = need_ctx.get(db, "sec_events")
    v = verify.verify_text("https://www.miit.gov.cn/x", "关于某某通知", "关于某某通知的正文内容……", c)
    assert v["status"] == "verified" and v["domain_trust"] == "high" and v["title_consistent"]
    v2 = verify.verify_text("https://blog.example.com/x", "关于某某通知", "完全无关的内容", c)
    assert v2["status"] == "pending_review" and v2["domain_trust"] == "low" and not v2["title_consistent"]
    v3 = verify.verify_text("https://www.miit.gov.cn/x", "内部通知", "本文件为内部资料,不对外公开。内部通知……", c)
    assert v3["sensitive"] and v3["status"] == "pending_review"
    v4 = verify.verify_text("https://www.miit.gov.cn/x", "t", "新版本正文", c, expected_hash="deadbeef")
    assert v4["changed"] and "已变化" in "".join(v4["notes"])


def test_verify_stage_quarantines_sensitive_docs(db, need):
    from app.models import RawDocument, Source
    from app.services import dedup
    from app.services.pipeline import process_document
    src = db.query(Source).first()
    url = f"https://www.miit.gov.cn/secret-{datetime.utcnow():%H%M%S%f}"
    doc = RawDocument(need_id=need.id, source_id=src.id, url=url, url_normalized=url, final_url=url,
                      title="某单位遭勒索攻击内部通报", publisher="t", published_at=datetime.utcnow(),
                      content_text="内部资料,不对外公开。某单位遭勒索攻击,系统瘫痪。", screen_status="pending")
    db.add(doc); db.flush(); dedup.assign_cluster(db, doc)
    r = process_document(db, need, doc)
    assert r["action"] == "manual_queue" and "密级" in doc.screen_reason
    assert doc.verification["sensitive"] and doc.verification["domain_trust"] == "high"


def test_relations_extract_and_link(db):
    from app.models import Event
    from app.services import profiles, relations
    profiles.setup_need(db, "policy_watch")
    need_ctx.reset_cache()
    c = need_ctx.get(db, "policy_watch")
    text = ("根据《中华人民共和国数据安全法》制定本办法。本办法自2026年1月1日起施行,《网络数据管理暂行规定》同时废止。"
            "本标准代替 GB/T 35273-2017。")
    rel = relations.extract(text, c, own_title="数据安全管理办法")
    kinds = {(r["relation"], r["title"]) for r in rel}
    assert ("references", "中华人民共和国数据安全法") in kinds
    assert ("repeals", "网络数据管理暂行规定") in kinds
    assert ("supersedes", "GB/T 35273-2017") in kinds
    old = Event(event_id="POL-REL-0001", need_id="policy_watch", payload={"title": "网络数据管理暂行规定"}, status="published")
    new = Event(event_id="POL-REL-0002", need_id="policy_watch", payload={"title": "数据安全管理办法"}, status="draft")
    db.add_all([old, new]); db.flush()
    rows = relations.link(db, new, rel, c)
    linked = {r.relation: r.target_event_id for r in rows if r.target_event_id}
    assert linked.get("repeals") == "POL-REL-0001"
    out = relations.for_event(db, "POL-REL-0001")
    assert out["incoming"] and out["incoming"][0]["source_event_id"] == "POL-REL-0002"
    r2 = capabilities.run("relations.extract", db, "policy_watch", event_id="POL-REL-0002", text=text)
    assert any(x["relation"] == "repeals" for x in r2["related_docs"])


def test_exports_dry_run_and_fake_feishu(db):
    from app.models import Event
    from app.services import exports
    c = need_ctx.get(db, "sec_events")
    cfg = dict(c.raw)
    cfg["outputs"] = {**cfg.get("outputs", {}), "exports": [
        {"name": "法规表", "kind": "feishu_bitable", "app_token": "APP", "table_id": "TBL", "key_field": "记录号",
         "field_map": {"标题": "title", "单位": "subject", "行业": "dim1", "状态": "record.status"}}]}
    c2 = need_ctx.NeedContext("sec_events", cfg)
    ev = Event(event_id="SEC-EXP-0001", need_id="sec_events", status="published",
               payload={"title": "某银行数据泄露", "org_name": "某银行", "industry": {"level1": "金融"}})
    db.add(ev); db.flush()
    r = exports.run(db, c2, dry_run=True)
    assert r["ok"] and r["exports"][0]["records"] >= 1
    row = next(x for x in r["exports"][0]["preview"] if x.get("记录号") == "SEC-EXP-0001")
    assert row["标题"] == "某银行数据泄露" and row["单位"] == "某银行" and row["行业"] == "金融" and row["状态"] == "published"

    class FakeResp:
        def __init__(self, data): self._d = data; self.headers = {"content-type": "application/json"}; self.status_code = 200
        def json(self): return self._d

    class FakeHttp:
        def __init__(self): self.posted = []
        def post(self, url, json=None, headers=None):
            if url.endswith("tenant_access_token/internal"):
                return FakeResp({"code": 0, "tenant_access_token": "T"})
            self.posted.append(json)
            return FakeResp({"code": 0, "data": {"records": json["records"]}})
        def get(self, url, headers=None, params=None):
            return FakeResp({"code": 0, "data": {"items": [{"fields": {"记录号": "SEC-EXP-0001"}}], "has_more": False}})
    http = FakeHttp()
    from app.config import settings
    settings.feishu_app_id, settings.feishu_app_secret = "id", "secret"
    r2 = exports.run(db, c2, http=http)
    item = r2["exports"][0]
    assert item["skipped"] >= 1 and item["written"] == item["records"] - item["skipped"]
    assert all("记录号" in rec["fields"] for chunk in http.posted for rec in chunk["records"])


def test_feishu_webhook_notify(monkeypatch):
    from app.config import settings
    from app.services import notify
    sent = {}

    class R:
        status_code = 200
        headers = {"content-type": "application/json"}
        def json(self): return {"code": 0}
    import httpx
    monkeypatch.setattr(httpx, "post", lambda url, json=None, timeout=15: (sent.update({"url": url, "json": json}) or R()))
    monkeypatch.setattr(settings, "feishu_webhook", "https://open.feishu.cn/hook/x")
    ok, note = notify.deliver_feishu("日报内容")
    assert ok and sent["json"]["msg_type"] == "text" and "日报内容" in sent["json"]["content"]["text"]
    monkeypatch.setattr(settings, "feishu_webhook", "")
    assert notify.deliver_feishu("x")[0] is False


def test_quality_scorecard(db, need):
    from app.services import kpi
    r = kpi.quality_scorecard(db, need.id)
    assert set(r["dimensions"]) == {"completeness", "accuracy", "consistency", "timeliness"}
    assert 0 <= r["score"] <= 100 and r["grade"] in "ABCD"
    assert abs(sum(r["weights"].values()) - 1.0) < 1e-6
    assert capabilities.run("quality.scorecard", db, need.id)["grade"] == r["grade"]


def test_new_capabilities_registered():
    names = {c["name"] for c in capabilities.list_capabilities()}
    assert {"verify", "verify.recheck", "relations.extract", "exports.run", "quality.scorecard", "notify.feishu"} <= names
