"""主线闭环:发布即落本地资料库、再核查接回访、紧急动作走通知组件、对标漏报率、JSON 接口 POST、验证信息可见。"""
import json
from datetime import date, datetime, timedelta

from app.services import capabilities, need_ctx


def _event(db, need_id, eid, title, status="draft", **extra):
    from app.models import Event
    from app.services.events import _sync_columns
    c = need_ctx.get(db, need_id)
    ev = Event(event_id=eid, need_id=need_id, status=status, payload={"title": title, **extra})
    _sync_columns(ev, c)
    db.add(ev); db.flush()
    return ev


def test_publish_writes_local_library(db, need, tmp_path, monkeypatch):
    """复核发布 → 画像声明的 local_library 目标立即落一份 JSON+MD。"""
    from app.models import AppUser, ReviewTask
    from app.services import review
    from app.services.events import create_draft
    from app.services.llm import MockLLM
    c = need_ctx.get(db, need.id)
    cfg = dict(need.config)
    cfg["outputs"] = {**cfg["outputs"], "exports": [{"name": "本地资料库", "kind": "local_library", "root": str(tmp_path),
                                                     "path_template": "{dim1}/{year}"}]}
    need.config = cfg
    need_ctx.reset_cache()
    p = MockLLM()._mock_extract("某市第三人民医院遭勒索软件攻击,系统瘫痪")
    p["sources"] = [{"ref_id": "SRC-001", "url_or_doc_number": "https://example.com/a", "publisher": "t",
                     "published_date": "2026-07-10", "credibility": "S1"}]
    ev = create_draft(db, need.id, p, source_credibility="S1")
    r1 = db.query(AppUser).filter_by(username="reviewer1").one()
    review.approve(db, ev.event_id, r1.id, c.record_schema())
    assert ev.status == "published"
    files = list((tmp_path / need.id).rglob(f"{ev.event_id}_*"))
    assert {f.suffix for f in files} == {".json", ".md"}


def test_recheck_due_flags_changed_sources(db, need, monkeypatch):
    from app.models import ActionLog, EventSource, FollowupTask, RawDocument, Source
    from app.services import fetcher, followup, verify
    src = db.query(Source).first()
    url = f"https://www.miit.gov.cn/doc-{datetime.utcnow():%H%M%S%f}"
    doc = RawDocument(need_id=need.id, source_id=src.id, url=url, url_normalized=url, final_url=url,
                      title="某通知", content_text="旧版本正文", screen_status="screened_in",
                      verification={"content_hash": verify.content_hash("旧版本正文"), "status": "verified"})
    db.add(doc); db.flush()
    ev = _event(db, need.id, "SEC-RCK-0001", "某通知", status="monitoring")
    db.add(EventSource(event_id=ev.event_id, ref_id="SRC-001", doc_id=doc.id, credibility="S1", supports_fields=["*"]))
    db.add(FollowupTask(event_id=ev.event_id, kind="T+30", due_date=date.today() - timedelta(days=1),
                        status="open", reason="状态未落定"))
    db.flush()

    class FR:
        ok, html, final_url, status, error = True, "<html><body>新版本正文:已正式生效</body></html>", url, 200, None
    monkeypatch.setattr(fetcher, "fetch", lambda u, render="auto", **k: FR())
    r = followup.recheck_due(db, need.id)
    assert r["checked"] == 1 and r["changed"] == ["SEC-RCK-0001"]
    t = db.query(FollowupTask).filter_by(event_id=ev.event_id).first()
    assert t.reason.startswith("[内容已变化")
    assert db.query(ActionLog).filter_by(action="record.source_changed", target=ev.event_id).first()
    assert doc.verification.get("changed_at")
    # 再跑一次:哈希已更新,不再报变化
    assert followup.recheck_due(db, need.id)["changed"] == []
    assert capabilities.run("followup.recheck", db, need.id)["tasks"] >= 1


def test_critical_action_goes_through_notify(db, need, monkeypatch):
    from app.services import actions, notify
    sent = []
    monkeypatch.setattr(notify, "send", lambda subject, body, channels=None, ctx=None: sent.append(subject) or {})
    # 找一个 CRITICAL 级动作;没有就临时登记一个
    crit = next((k for k, v in actions.CATALOG.items() if v.level >= actions.CRITICAL), None)
    if crit is None:
        actions.CATALOG["test.critical"] = actions.Spec("test", actions.CRITICAL, "测试", 0, "")
        crit = "test.critical"
    actions.record(db, crit, "紧急:测试动作", need_id=need.id)
    assert sent and sent[0].startswith("[紧急]")
    sent.clear()
    actions.record(db, "source.seeds_loaded", "一般动作", need_id=need.id, count=1)
    assert not sent


def test_benchmark_recall_and_scorecard_blend(db, need):
    from app.models import EventSource, RawDocument, Source
    from app.services import benchmark, kpi
    src = db.query(Source).first()
    e1 = _event(db, need.id, "SEC-BM-0001", "某银行数据泄露事件通报", status="published")
    u = f"https://www.example.gov.cn/bm-{datetime.utcnow():%H%M%S%f}"
    d = RawDocument(need_id=need.id, source_id=src.id, url=u, url_normalized=u, final_url=u,
                    title="某医院遭勒索攻击", content_text="x", screen_status="screened_out")
    db.add(d); db.flush()
    items = [{"title": "某银行数据泄露事件通报", "url": ""},                 # 标题命中记录
             {"title": "某医院遭勒索攻击", "url": u},                          # 采到但没成记录
             {"title": "完全没采到的一条事件", "url": "https://nowhere.cn/x"}]  # 没采到
    r = benchmark.run(db, need_ctx.get(db, need.id), "测试基准", items, period="2026-09")
    assert r["total"] == 3 and r["matched"] == 1 and abs(r["recall"] - 1 / 3) < 1e-3
    assert r["by_reason"] == {"doc_only": 1, "not_found": 1}
    assert r["items"][0]["matched_event_id"] == e1.event_id
    lat = benchmark.latest(db, need.id)
    assert lat["batch_id"] == r["batch_id"] and abs(lat["recall"] - 1 / 3) < 1e-3
    sc = kpi.quality_scorecard(db, need.id)
    assert sc["benchmark"]["batch_id"] == r["batch_id"] and "verification" in sc
    assert capabilities.run("benchmark.run", db, need.id, batch_name="b2", items=items[:1])["recall"] == 1.0
    dash = kpi.dashboard(db, need.id)
    assert dash["quality"] and dash["quality"]["grade"] in "ABCD"


def test_json_api_post_uses_post_json(monkeypatch):
    from app.services import adapters, fetcher
    seen = {}

    class FR:
        ok, status, error, final_url = True, 200, None, "https://x/api"
        html = json.dumps({"data": {"items": [{"title": "t1", "url": "/p/1"}]}})
    monkeypatch.setattr(fetcher, "post_json", lambda url, data=None, headers=None, as_form=False, **k: (seen.update({"url": url, "data": data, "form": as_form}) or FR()))

    class Src:
        entry_url = "https://x/api"
        adapter_config = {"method": "POST", "params": {"channel": "A"}, "items_path": "data.items",
                          "fields": {"title": "title", "url": "url"}, "page_param": "page", "page_start": 1}
        name, kind = "t", "page"
    items = adapters.JsonApiAdapter(Src()).discover_page(0)
    assert items and items[0].url == "https://x/p/1" and seen["data"] == {"channel": "A", "page": 1}


def test_documents_api_exposes_verification(db, need):
    from app.api import routes
    from app.models import AppUser, RawDocument, Source
    src = db.query(Source).first()
    u = f"https://www.miit.gov.cn/v-{datetime.utcnow():%H%M%S%f}"
    db.add(RawDocument(need_id=need.id, source_id=src.id, url=u, url_normalized=u, final_url=u, title="t",
                       content_text="x", screen_status="screened_in", verification={"status": "verified", "domain_trust": "high"}))
    db.flush()
    rows = routes.list_documents(need_id=need.id, relevant=True, limit=50, db=db, _=db.query(AppUser).first())
    assert any((r.get("verification") or {}).get("status") == "verified" for r in rows)


def test_autopilot_has_recheck_and_exports_tasks():
    from app.services import autopilot
    keys = [t[0] for t in autopilot.TASKS]
    assert "recheck" in keys and "exports" in keys
    assert set(keys) <= set(autopilot._ACTIONS)
