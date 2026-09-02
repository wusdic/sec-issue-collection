"""场景适用性验证:医疗政策 / 招标中标 / 企业定向监测 / 省市地域文件 —— 四个新画像零代码接入,
关键词自动生成、范围限定闸门、轻量抽取、普通数值金额汇总、可组合阶段与能力独立调用。"""
from datetime import datetime

import pytest

from app.db import SessionLocal
from app.services import capabilities, keywords, need_ctx, profiles

NEEDS = ["med_policy", "tender_watch", "company_watch", "nanjing_docs"]


@pytest.fixture(scope="module", autouse=True)
def _setup_needs():
    s = SessionLocal()
    try:
        for nid in NEEDS:
            r = profiles.setup_need(s, nid)
            assert r["keywords"], r          # 没给关键词文件 → 自动生成
        s.commit()
    finally:
        s.close()
    need_ctx.reset_cache()
    yield


def _doc(db, need_id, title, text, idx=0):
    from app.models import RawDocument, Source
    from app.services import dedup
    src = db.query(Source).first()
    url = f"https://demo.local/{need_id}/{idx}-{datetime.utcnow():%H%M%S%f}"
    doc = RawDocument(need_id=need_id, source_id=src.id, url=url, url_normalized=url, final_url=url,
                      title=title, publisher="test", published_at=datetime.utcnow(),
                      content_text=text, screen_status="pending")
    db.add(doc)
    db.flush()
    dedup.assign_cluster(db, doc)
    return doc


def _run(db, need_id, sample_idx):
    from app.models import Event, NeedProfile
    from app.services.pipeline import process_document
    need = db.get(NeedProfile, need_id)
    smp = need_ctx.for_need(need).demo_samples[sample_idx]
    r = process_document(db, need, _doc(db, need_id, smp["title"], smp["text"], sample_idx))
    return r, (db.get(Event, r["event_id"]) if r.get("event_id") else None)


# ---------------- 关键词自动生成 ----------------

def test_keywords_generated_from_scope(db):
    from app.models import KeywordSet
    for nid in NEEDS:
        ks = db.query(KeywordSet).filter_by(need_id=nid, is_active=True).first()
        assert ks and ks.content.get("generated_by") == "keywords.generate", nid
        qs = keywords.expand_queries(ks.content)
        assert qs, nid
    qs = keywords.expand_queries(db.query(KeywordSet).filter_by(need_id="company_watch", is_active=True).first().content)
    assert "星河智算 融资" in qs and "蓝海数科 中标" in qs and "融资" not in qs      # 主体×主题,不单独搜"融资"
    qs = keywords.expand_queries(db.query(KeywordSet).filter_by(need_id="tender_watch", is_active=True).first().content)
    assert "江苏省 网络安全 招标公告" in qs and "网络安全 中标公告" in qs                 # 画像自定义三元配方
    qs = keywords.expand_queries(db.query(KeywordSet).filter_by(need_id="med_policy", is_active=True).first().content)
    assert "医疗 互联网诊疗" in qs and "医院 通知" in qs and "医疗卫生" not in qs           # 行业锚点×主题/文种,不单独搜行业


def test_watch_targets_feed_keywords(db):
    from app.models import WatchTarget
    db.add(WatchTarget(need_id="company_watch", kind="org", value="云脉半导体", aliases=["云脉"], reason="测试"))
    db.flush()
    g = keywords.term_groups(need_ctx.get(db, "company_watch"), db)
    assert "云脉半导体" in g["entity_terms"] and "云脉" in g["entity_terms"]
    assert "云脉 融资" in keywords.compose(g, None)


def test_discovery_recipes_generated_without_file(db):
    c = need_ctx.get(db, "company_watch")
    assert c.discovery_file is None and c.source_search_queries == []        # 契约层只读文件
    qs = keywords.search_queries_for(c)                                        # 能力层按 scope 生成
    assert qs and any("星河智算" in q for q in qs)
    assert "星河智算科技有限公司" in keywords.recipes_for(c)["subject_terms"]
    from app.services import prospect
    assert prospect.base_queries(c) == qs


# ---------------- 场景①:医疗行业政策(文档型,复用政策 Schema,行业限定) ----------------

def test_med_policy_pipeline(db):
    r, ev = _run(db, "med_policy", 0)
    assert r["action"] == "draft_created", r
    assert ev.event_id.startswith("MED-") and ev.org_name == "国家卫生健康委"
    assert ev.org_uscc == "国卫办医发〔2026〕5号" and ev.industry_l1 == "互联网诊疗" and ev.severity == "规范性文件"
    # 未提及医疗行业的文件 → 范围外
    r2 = capabilities.run("scope_gate", db, "med_policy", payload={}, title="关于印发交通运输条例的通知", text="交通运输部印发条例。")
    assert r2["out_of_scope"] and "行业" in r2["reason"]


# ---------------- 场景②:招标/中标(对象型,地域+行业限定,普通数值金额) ----------------

def test_tender_pipeline_and_plain_amount(db):
    from app.services import kpi
    r, ev = _run(db, "tender_watch", 0)
    assert r["action"] == "draft_created", r
    assert ev.event_id.startswith("TND-")
    assert ev.org_name == "南京市某区大数据管理局" and ev.org_uscc == "JSZC-2026-0812"
    assert ev.province == "江苏省" and ev.severity == "中标公告" and ev.industry_l1 == "网络安全"
    assert ev.payload["winner"] == "江苏某某信息技术有限公司" and ev.payload["amount"]["value"] == 2865000
    r2, ev2 = _run(db, "tender_watch", 1)
    assert r2["action"] == "draft_created" and ev2.severity == "招标公告" and not ev2.payload.get("winner")
    # 非江苏项目 → 范围外(require_mention: regions)
    r3 = capabilities.run("scope_gate", db, "tender_watch", payload={"title": "x"},
                          title="杭州市某单位网络安全设备采购招标公告", text="采购人:杭州市某局。")
    assert r3["out_of_scope"] and "地域" in r3["reason"]
    # 普通数值金额按地区汇总(只统计已发布口径)
    ev.status = "published"
    db.flush()
    a = kpi.amount_stats(db, "tender_watch")
    assert a["enabled"] and a["scope"] == "plain" and a["by_group"]["江苏省"] == 2865000
    # 回访触发:招标阶段尚未公布中标人
    from app.services.followup import schedule_followups
    tasks = schedule_followups(db, ev2)
    assert tasks and "尚未公布中标人" in tasks[0].reason
    assert kpi.missing_field(db, "tender_watch")["field"] == "winner"


# ---------------- 场景③:企业定向监测(观测型,轻量抽取,主体名单+别名) ----------------

def test_company_watch_light_extraction(db):
    c = need_ctx.get(db, "company_watch")
    assert c.extract_mode == "light" and c.schema_file is None
    assert set(c.record_schema()["properties"]) >= {"title", "summary", "subject", "category", "sentiment"}
    assert c.role_path("dim1") == "category" and c.role_path("subject") == "subject"
    r, ev = _run(db, "company_watch", 0)
    assert r["action"] == "draft_created", r
    assert ev.event_id.startswith("CMP-") and ev.org_name == "星河智算科技有限公司" and ev.industry_l1 == "融资"
    assert ev.payload["summary"].startswith("星河智算科技有限公司宣布") and ev.payload["sentiment"] == "正面"
    # 不点名名单企业的综述 → 不入库
    r2, _ = _run(db, "company_watch", 1)
    assert r2["action"] == "screened_out"
    g = capabilities.run("scope_gate", db, "company_watch", payload={"title": "行业算力榜单"},
                         title="某机构发布行业算力榜单", text="盘点十家厂商。")
    assert g["out_of_scope"] and "主体" in g["reason"]
    # 提示词里带范围说明与别名
    from app.services.prompts import screen_prompts
    sysm, _ = screen_prompts(c.raw, "t", "x", ctx=c)
    assert "星河智算科技有限公司(含 星河智算/XinghePower)" in sysm and "主体" in sysm


# ---------------- 场景④:省市地域文件(文档型,轻量抽取,地域限定) ----------------

def test_regional_docs_pipeline(db):
    r, ev = _run(db, "nanjing_docs", 0)
    assert r["action"] == "draft_created", r
    assert ev.event_id.startswith("NJD-") and ev.province == "南京市" and ev.industry_l1 == "数据要素"
    assert ev.org_name == "南京市人民政府办公厅"
    r2, _ = _run(db, "nanjing_docs", 1)                       # 杭州 → 范围外
    assert r2["action"] == "screened_out"
    from app.services import coverage
    cov = {x["industry"] for x in coverage.industry_coverage(db, "nanjing_docs")}
    assert {"数据要素", "政务服务", "数字政府", "公共数据开放"} <= cov     # 覆盖维度来自 scope.topics


# ---------------- 阶段可组合 / 能力可独立调用 ----------------

def test_pipeline_stages_are_composable(db):
    from app.models import NeedProfile
    from app.services.pipeline import process_document
    need = db.get(NeedProfile, "company_watch")
    cfg = dict(need.config)
    cfg["pipeline"] = {**cfg.get("pipeline", {}), "stages": ["screen", "draft"]}   # 去掉抽取/闸门/去重
    need.config = cfg
    c = need_ctx.for_need(need)
    assert c.pipeline_stages == ["screen", "draft"]
    smp = c.demo_samples[0]
    r = process_document(db, need, _doc(db, "company_watch", smp["title"], smp["text"], 7))
    assert r["action"] == "draft_created"
    from app.models import Event
    ev = db.get(Event, r["event_id"])
    assert ev.payload["title"] == smp["title"] and "summary" in ev.payload and "category" not in ev.payload


def test_capabilities_registry_and_standalone_calls(db):
    names = {c["name"] for c in capabilities.list_capabilities()}
    assert {"screen", "extract", "guard", "scope_gate", "dedup.record", "keywords.generate",
            "keywords.expand", "prospect.queries", "coverage.summary", "digest.build"} <= names
    s = capabilities.run("screen", db, "tender_watch", title="南京网络安全等保项目中标公告", text="中标金额 100 万元")
    assert s["is_candidate"]
    e = capabilities.run("extract", db, "tender_watch", title="南京网络安全等保项目中标公告",
                         text="项目编号:JSZC-2026-0001。采购人:南京市某局。中标供应商:某某公司,中标金额:12万元。")
    assert e["payload"]["amount"]["value"] == 120000 and e["payload"]["buyer"] == "南京市某局"
    k = capabilities.run("keywords.expand", db, "tender_watch")
    assert k["count"] > 0
    q = capabilities.run("prospect.queries", db, "med_policy")
    assert q["queries"]
    with pytest.raises(KeyError):
        capabilities.run("no.such.capability", db, "med_policy")


def test_cli_lists_capabilities():
    from typer.testing import CliRunner
    from app.cli import cli
    out = CliRunner().invoke(cli, ["cap-list"]).output
    assert "keywords.generate" in out and "[处理] screen" in out


def test_needs_ui_exposes_scope_and_mode(db):
    from app.api import routes
    from app.models import AppUser
    user = db.query(AppUser).first()
    ui = routes.need_ui("tender_watch", db=db, _=user)
    assert ui["extract_mode"] == "schema" and any(x.startswith("地域:江苏省") for x in ui["scope"])
    ui2 = routes.need_ui("company_watch", db=db, _=user)
    assert ui2["extract_mode"] == "light" and ui2["tabs"]["leads"]["enabled"] is False
    assert {n["id"] for n in routes.list_needs(db=db, _=user)} >= set(NEEDS)
