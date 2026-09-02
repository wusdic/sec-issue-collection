"""通知功能组件(渠道插件)与"分类存本地"导出。"""
import json
from pathlib import Path

from app.services import capabilities, need_ctx, notify


def test_notify_channels_registry_and_send(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "smtp_host", "")
    monkeypatch.setattr(settings, "feishu_webhook", "")
    assert notify.configured_channels() == []                    # 什么都没配 → 缺省无渠道
    posted = []

    class R:
        status_code = 200
        headers = {"content-type": "application/json"}
        def json(self): return {"code": 0}
    import httpx
    monkeypatch.setattr(httpx, "post", lambda url, json=None, timeout=15: (posted.append((url, json)) or R()))
    r = notify.send("主题", "正文", channels=[{"kind": "webhook", "url": "https://hook/x", "template": "dingtalk"},
                                              "feishu", "nope"])
    assert r["webhook"]["ok"] and posted[0][1]["msgtype"] == "text" and "主题" in posted[0][1]["text"]["content"]
    assert r["feishu"]["ok"] is False and "未配置" in r["feishu"]["note"]     # feishu 未配 webhook → 跳过
    assert r["nope"]["ok"] is False and "未知渠道" in r["nope"]["note"]
    # 新渠道 = 登记一个函数
    notify.CHANNELS["memo"] = lambda s, b, cfg: (True, "memo:" + s)
    try:
        assert notify.send("t", "b", channels=["memo"])["memo"]["note"] == "memo:t"
    finally:
        notify.CHANNELS.pop("memo")


def test_profile_declares_channels(db):
    c = need_ctx.get(db, "sec_events")
    cfg = dict(c.raw)
    cfg["outputs"] = {**cfg.get("outputs", {}), "notify": {"channels": ["email", {"kind": "webhook", "url": "https://h"}]}}
    c2 = need_ctx.NeedContext("sec_events", cfg)
    assert [x["kind"] for x in notify.channels_for(c2)] == ["email", "webhook"]
    assert "notify.send" in {x["name"] for x in capabilities.list_capabilities()}


def test_local_library_export_classifies_records(db, tmp_path):
    from app.models import Event
    from app.services import exports
    c = need_ctx.get(db, "sec_events")
    cfg = dict(c.raw)
    cfg["outputs"] = {**cfg.get("outputs", {}), "exports": [
        {"name": "本地库", "kind": "local_library", "root": str(tmp_path), "path_template": "{dim1}/{grade}/{year}"}]}
    c2 = need_ctx.NeedContext("sec_events", cfg)
    ev = Event(event_id="SEC-LIB-0001", need_id="sec_events", status="published",
               payload={"title": "某银行数据泄露", "org_name": "某银行", "industry": {"level1": "金融"},
                        "severity": {"level": "重大"}, "occurred_date": "2026-05-01", "summary": "摘要文字",
                        "sources": [{"credibility": "S1", "url_or_doc_number": "https://a.gov.cn/x"}]})
    from app.services.events import _sync_columns
    _sync_columns(ev, c2)
    db.add(ev); db.flush()
    r = exports.run(db, c2)
    item = r["exports"][0]
    assert item["kind"] == "local_library" and item["written"] >= 1
    d = tmp_path / "sec_events" / "金融" / "重大" / "2026"
    files = list(d.glob("SEC-LIB-0001_*"))
    assert {f.suffix for f in files} == {".json", ".md"}
    data = json.loads(next(f for f in files if f.suffix == ".json").read_text(encoding="utf-8"))
    assert data["roles"]["subject"] == "某银行" and data["payload"]["title"] == "某银行数据泄露"
    md = next(f for f in files if f.suffix == ".md").read_text(encoding="utf-8")
    assert "# 某银行数据泄露" in md and "https://a.gov.cn/x" in md and "单位:某银行" in md
