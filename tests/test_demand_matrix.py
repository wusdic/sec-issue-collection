"""需求泛化验证:不手写画像,按"需求空间"的正交轴组合**生成**画像,逐个装载并跑通全链路。

轴(见 design/platform/05):记录原型 × 主范围维度(地域/行业/主题/主体)× 是否必须提及 × 抽取模式 ×
是否有普通数值金额。每个组合都要:关键词自动生成、正样本建记录且角色列落库、负样本按范围闸门处理、
日报/看板/覆盖度/界面定义可用。这证明平台对"没见过的需求"也成立,而不只对手写的几个例子成立。"""
import itertools
from datetime import datetime

import pytest

from app.services import capabilities, keywords, need_ctx, profiles

ARCHETYPES = ["事件型", "文档型", "对象型", "观测型"]
SCOPE_KINDS = ["regions", "industries", "topics", "entities"]
VALUE = {"regions": ("苏州市", "苏州"), "industries": ("能源行业", "能源"),
         "topics": ("碳排放", "碳市场"), "entities": ("恒远集团", "恒远")}
ROLE_FOR = {"regions": "region", "industries": "dim2", "topics": "dim1", "entities": "subject"}
PATH_FOR = {"regions": "region", "industries": "industry", "topics": "category", "entities": "subject"}


def make_profile(idx: int, archetype: str, kind: str, require: bool, amount: bool) -> dict:
    v, alias = VALUE[kind]
    scope = {kind: [{"value": v, "terms": [alias]}], "require_mention": [kind] if require else []}
    if kind != "topics":
        scope["topics"] = ["主题甲", "主题乙"]
    rules = [{"path": PATH_FOR[kind], "when_contains": [alias, v], "value": v},
             {"path": "summary", "regex": "正文[::]\\s*([^\\n]{6,200})"}]
    if kind != "topics":
        rules.append({"path": "category", "when_contains": ["主题甲"], "value": "主题甲"})
    if amount:
        rules.append({"path": "amount", "regex": "金额\\s*(\\d+(?:\\.\\d+)?)\\s*万", "transform": "wan"})
    cfg = {
        "need": {"id": f"gen_{idx}", "name": f"生成需求{idx}", "owner": "t", "priority": "B",
                 "timeliness_sla": "日级", "primary_behaviors": ["G1"], "languages": ["zh"], "regions": ["中国大陆"]},
        "compliance": {"data_categories": ["公开信息"], "personal_info": False, "collection_boundary": "仅公开渠道",
                       "retention": "1年", "usage_limit": "内部"},
        "record_schemas": [{"archetype": archetype, "min_publish_fields": ["title"]}],
        "record": {"id_prefix": f"G{idx}", "envelope": {"id_field": "", "status_field": ""},
                   # 行业维度:轻量 Schema 追加 industry 字段并把 dim2 角色指过去(其余角色用轻量缺省)
                   **({"field_roles": {"dim2": "industry"}} if kind == "industries" else {}),
                   "record_types": {"values": ["记录", "汇总", "不该入库"], "default": "记录", "out_of_scope": "不该入库",
                                    "advisory": {"value": "汇总", "hints": ["盘点"], "subject_blank_values": ["", "未披露"]}},
                   "dedup": {"subject_roles": ["subject+title"], "type_role": None, "window_days": 30}},
        "dictionaries": {"relevance_term_fields": ["topic_terms"]},
        "scope": scope,
        "pipeline": {"extract_mode": "light",
                     "light_fields": ([{"name": "amount", "type": "number", "desc": "金额(元)"}] if amount else [])
                     + ([{"name": "industry", "type": "string", "desc": "行业"}] if kind == "industries" else [])},
        "sources": {"credibility_levels": {"confirm_allowed": ["S1"], "levels": {"S1": "官方"}}},
        "quality": {"model": "事实核实型", "screen": {"goal": f"与{v}相关的主题甲/主题乙信息"}},
        "update": {"followup_schedule": [30]},
        "outputs": {"reports": [{"name": "周报", "cycle": "周", "audience": "t"}],
                    "reports_engine": {"crosstab": {"row_role": ROLE_FOR[kind], "col_roles": ["dim1"]},
                                       "amount_sum": ({"kind": "plain", "fields": ["amount"], "group_role": ROLE_FOR[kind]} if amount else None),
                                       "status_count": None, "missing_field": None},
                    "leads_engine": {"enabled": False},
                    "digest": {"title": f"生成需求{idx}日报", "group_roles": [ROLE_FOR[kind]], "rank_role": "dim1"}},
        "ui": {"record_label": "条目", "tabs": {"leads": {"label": "线索", "enabled": False}}},
        "benchmark": {"baselines": [{"name": "抽样", "type": "人工抽样", "cycle": "月"}], "target_recall": 0.9},
        "mock": {"screen_keywords": [v, alias, "主题甲", "主题乙", "发布"], "extract_rules": rules},
    }
    return cfg


def _combos():
    allc = list(itertools.product(ARCHETYPES, SCOPE_KINDS, [True, False], [True, False]))
    return allc[::5]          # 抽 13 个组合,覆盖每个轴的每个取值


COMBOS = _combos()


def _doc(db, need_id, title, text, idx):
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


@pytest.mark.parametrize("i,archetype,kind,require,amount", [(i, *c) for i, c in enumerate(COMBOS)])
def test_generated_profile_runs_end_to_end(db, i, archetype, kind, require, amount):
    from app.models import Event, NeedProfile
    from app.services import coverage, digest, kpi
    from app.services.pipeline import process_document
    v, alias = VALUE[kind]
    cfg = make_profile(i, archetype, kind, require, amount)
    need = profiles.register_need(db, cfg)              # 六要素校验通过
    need_ctx.reset_cache()
    c = need_ctx.for_need(need)
    assert c.archetype == archetype and c.extract_mode == "light" and c.scope_values(kind) == [v]

    # 关键词自动生成:范围词 × 主题词
    content, ks = keywords.generate(db, c)
    qs = keywords.expand_queries(content)
    assert qs, content
    if kind == "topics":
        assert v in qs
    else:
        assert f"{v} 主题甲" in qs or f"{alias} 主题甲" in qs

    # 正样本:提到范围词 + 主题词 → 建记录,角色列落库
    pos = _doc(db, need.id, f"{alias}发布主题甲相关信息", f"{alias}今日发布主题甲文件,金额 12 万元。", 1)
    r = process_document(db, need, pos)
    assert r["action"] == "draft_created", r
    ev = db.get(Event, r["event_id"])
    assert ev.event_id.startswith(f"G{i}-")
    col = need_ctx.ROLE_COLUMNS[ROLE_FOR[kind]]
    assert getattr(ev, col) == v, (col, getattr(ev, col))
    if kind != "topics":
        assert ev.industry_l1 == "主题甲"
    assert ev.payload["summary"].startswith(f"{alias}今日发布")
    if amount:
        assert ev.payload["amount"] == 120000

    # 负样本:不提范围词 → require_mention 时范畴外;否则照常入库
    neg = _doc(db, need.id, "别处发布主题甲相关信息", "某地今日发布主题甲文件。", 2)
    r2 = process_document(db, need, neg)
    assert r2["action"] == ("screened_out" if require else "draft_created"), r2

    # 输出与界面全部可用
    ev.status = "published"
    db.flush()
    d = digest.build_content(db, need.id, datetime.utcnow().date(), ctx=c)
    assert d["title"] == f"生成需求{i}日报" and d["events_total"] >= 1
    assert "生成需求" in digest.render_markdown(d)
    dash = kpi.dashboard(db, need.id, ctx=c)
    assert dash["events_published"] >= 1 and dash["traceability"]["ok"]
    a = kpi.amount_stats(db, need.id, ctx=c)
    assert a["enabled"] is amount
    if amount:
        assert a["by_group"][v] == 120000
    cov = {x["industry"] for x in coverage.industry_coverage(db, need.id, ctx=c)}
    assert v in cov or "主题甲" in cov
    ui = c.to_ui()
    assert ui["extract_mode"] == "light" and ui["tabs"]["leads"]["enabled"] is False
    assert capabilities.run("scope_gate", db, need.id, ctx=c, payload={}, title="别处", text="无关")["out_of_scope"] is require
