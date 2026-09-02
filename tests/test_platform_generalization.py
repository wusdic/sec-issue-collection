"""通用平台验证:同一套引擎零改动、只换画像,就能跑第二个需求(文档型「政策监管动态库」);
引擎代码里不得再有行业字面量;界面/报表/日报/回访/去重全部按画像角色工作。"""
import ast
import pathlib
from datetime import datetime

import pytest

from app.db import SessionLocal
from app.services import need_ctx, profiles

POLICY = "policy_watch"
SEC = "sec_events"
ROOT = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module", autouse=True)
def _policy_need():
    """把第二个画像装进库(注册 + 词表 + 关键词 + 种子源),模块内共享。"""
    s = SessionLocal()
    try:
        r = profiles.setup_need(s, POLICY)
        s.commit()
        assert r["need_id"] == POLICY and r["dictionaries"] and r["keywords"]
    finally:
        s.close()
    need_ctx.reset_cache()
    yield


def _source(db):
    from app.models import Source
    return db.query(Source).first()


def _doc(db, need_id: str, title: str, text: str, idx: int):
    from app.models import RawDocument
    from app.services import dedup
    url = f"https://demo.local/{need_id}-{idx}-{datetime.utcnow():%H%M%S%f}"
    doc = RawDocument(need_id=need_id, source_id=_source(db).id, url=url, url_normalized=url, final_url=url,
                      title=title, publisher="test", published_at=datetime.utcnow(),
                      content_text=text, screen_status="pending")
    db.add(doc)
    db.flush()
    dedup.assign_cluster(db, doc)
    return doc


# ---------------- 画像 → 上下文 ----------------

def test_policy_profile_context(db):
    c = need_ctx.get(db, POLICY)
    assert c.id_prefix == "POL" and c.archetype == "文档型"
    assert c.role_path("subject") == "issuer" and c.role_path("subject_key") == "doc_number"
    assert c.role_path("dim1") == "topics[0]"
    assert c.dedup["type_role"] is None and c.dedup["subject_roles"][0] == "subject_key"
    assert c.confirm_allowed == ["S1"]
    assert c.tristate_fields == [] and not c.leads["enabled"]
    assert c.envelope["id_field"] == "doc_id" and c.envelope["status_field"] == ""
    assert c.role_label("dim1") == "主题" and c.ui["record_label"] == "政策"
    assert c.selftest_query == "网络安全 征求意见稿"
    # 安全库的上下文完全不同,且互不污染
    s = need_ctx.get(db, SEC)
    assert s.id_prefix == "SEC" and s.tristate_fields == ["loss_L1", "loss_L2", "loss_L3", "loss_L4", "loss_L5"]
    assert s.role_path("dim1") == "industry.level1" and s.leads["enabled"]


def test_neutral_defaults_without_profile():
    """没有画像的需求也能拿到中性缺省(不是某个行业的行为)。"""
    c = need_ctx.get(None, "nonexistent_need")
    assert c.id_prefix == "REC" and c.record_types["default"] == "单一记录"
    assert c.tristate_fields == [] and c.field_roles["dim1"] == "dim1"
    assert c.region_policy["domestic_tlds"] == []       # 不声明地域 → 不限地域


# ---------------- 提示词 / Mock 模型按画像 ----------------

def test_prompts_are_profile_driven(db):
    from app.models import NeedProfile
    from app.services.prompts import extract_prompts, screen_prompts
    pol = db.get(NeedProfile, POLICY).config
    sec = db.get(NeedProfile, SEC).config
    ps, _ = screen_prompts(pol, "t", "x")
    ss, _ = screen_prompts(sec, "t", "x")
    assert f"NEED_ID={POLICY}" in ps and "征求意见" in ps and "政策监管动态库" in ps
    assert f"NEED_ID={SEC}" in ss and "征求意见稿" not in ss
    pe, _ = extract_prompts(pol, {}, {"type": "object"}, "t", "x")
    assert "policy.schema.json" in pe and "doc_number" in pe and "『不该入库』" in pe


def test_mock_llm_follows_profile(db):
    from app.services.llm import MockLLM
    m = MockLLM()
    sys_screen = f"TASK=screen\nNEED_ID={POLICY}\n"
    assert m.complete_json(sys_screen, "关于公开征求《条例(征求意见稿)》意见的通知")["is_candidate"]
    assert not m.complete_json(sys_screen, "今天天气不错,适合出游")["is_candidate"]
    c = need_ctx.get(db, POLICY)
    out = m.complete_json(f"TASK=extract\nNEED_ID={POLICY}\nSCHEMA_FILE={c.schema_file}\n",
                          "标题:工信部发布指南\n正文:\n工业和信息化部印发《指南》,文号工信厅信发〔2026〕12号,自发布之日起施行。")
    assert out["doc_number"] == "工信厅信发〔2026〕12号" and out["issuer"] == "工业和信息化部"
    assert out["record_type"] == "正式文件" and out["title"] == "工信部发布指南"
    assert "loss_L1" not in out                        # 不会把安全库的字段塞进政策记录


# ---------------- 端到端:政策库全链路 ----------------

def test_policy_pipeline_end_to_end(db):
    from app.models import Event, NeedProfile
    from app.services import coverage, digest, kpi
    from app.services.events import validate_publish
    from app.services.extraction import load_record_schema
    from app.services.followup import build_search_pack, schedule_followups
    from app.services.leads import generate_leads
    from app.services.pipeline import process_document

    need = db.get(NeedProfile, POLICY)
    c = need_ctx.for_need(need)
    samples = c.demo_samples
    assert len(samples) >= 2

    # ① 正式文件(带文号)→ 草稿:记录号前缀、主体=发布机关、去重键=文号、维度=主题、等级=文种
    doc = _doc(db, POLICY, samples[1]["title"], samples[1]["text"], 1)
    r = process_document(db, need, doc)
    assert r["action"] == "draft_created", r
    ev = db.get(Event, r["event_id"])
    assert ev.event_id.startswith("POL-")
    assert ev.org_name == "工业和信息化部" and ev.org_uscc == "工信厅信发〔2026〕12号"
    assert ev.industry_l1 == "数据安全" and ev.severity == "规范性文件"
    assert ev.record_type == "正式文件"
    assert ev.payload.get("doc_number") == "工信厅信发〔2026〕12号"

    # ② 同文号再来一篇 → 指纹命中(文号唯一),不建第二条
    doc2 = _doc(db, POLICY, samples[1]["title"] + "(转载)", samples[1]["text"], 2)
    r2 = process_document(db, need, doc2)
    assert r2["action"] == "merge_suggested" and r2["event_id"] == ev.event_id

    # ③ 征求意见稿 → record_type=征求意见(画像 advisory 值)
    doc3 = _doc(db, POLICY, samples[0]["title"], samples[0]["text"], 3)
    r3 = process_document(db, need, doc3)
    assert r3["action"] == "draft_created"
    ev3 = db.get(Event, r3["event_id"])
    assert ev3.record_type == "征求意见"

    # ④ 回访触发器按画像:状态未落定 + 生效日期未披露;检索包用文号
    tasks = schedule_followups(db, ev)
    assert tasks and "生效日期未披露" in tasks[0].reason and "状态未落定" in tasks[0].reason
    pack = build_search_pack(ev.payload, c)
    assert pack["queries"] and all("工信厅信发〔2026〕12号" in q for q in pack["queries"])
    assert "国务院政策文件库" in pack["links"]

    # ⑤ 发布校验:无三态字段 → 无金额红线;信封按画像(doc_id 注入、业务 status 不被系统状态覆盖)
    errors = validate_publish(db, ev, load_record_schema(c.schema_file))
    assert errors == [], errors

    # ⑥ 线索引擎关闭 → 不产线索
    assert generate_leads(db, ev) == []

    # ⑦ 报表/日报/覆盖度全按画像口径
    assert kpi.amount_stats(db, POLICY)["enabled"] is False
    assert kpi.status_count(db, POLICY)["enabled"] is False
    hm = kpi.heatmap(db, POLICY)
    assert hm["row_label"] == "主题" and "grade" in hm["col_roles"]
    assert kpi.dashboard(db, POLICY)["tiles"][0]["label"] == "政策总数"
    content = digest.build_content(db, POLICY, datetime.utcnow().date())
    md = digest.render_markdown(content)
    assert content["title"] == "政策动态日报" and "主题热点" in md and "政策" in md
    assert content["labels"]["leads_enabled"] is False
    cov = coverage.industry_coverage(db, POLICY)
    assert {x["industry"] for x in cov} >= {"网络安全", "数据安全", "个人信息保护"}
    qs = coverage.prospect_queries(db, POLICY)
    assert qs and all("征求意见稿" in q or "管理办法" in q for q in qs)

    # ⑧ 数据隔离:安全库里没有 POL 记录
    assert not [e for e in db.query(Event).filter_by(need_id=SEC).all() if e.event_id.startswith("POL-")]


def test_needs_api_and_ui(db):
    from app.api import routes
    from app.models import AppUser
    user = db.query(AppUser).first()
    needs = routes.list_needs(db=db, _=user)
    ids = {n["id"] for n in needs}
    assert {SEC, POLICY} <= ids
    ui = routes.need_ui(POLICY, db=db, _=user)
    assert ui["tabs"]["leads"]["enabled"] is False and ui["record_label"] == "政策"
    assert ui["list_columns"][2]["label"] == "发布机关"
    assert ui["envelope"]["id_field"] == "doc_id"
    ui_sec = routes.need_ui(SEC, db=db, _=user)
    assert ui_sec["tristate_fields"] == ["loss_L1", "loss_L2", "loss_L3", "loss_L4", "loss_L5"]
    # 列表按角色键返回(本用例自己造一条,函数级 db 会回滚,不依赖上一个用例)
    from app.models import NeedProfile
    from app.services.pipeline import process_document
    need = db.get(NeedProfile, POLICY)
    sample = need_ctx.for_need(need).demo_samples[1]
    assert process_document(db, need, _doc(db, POLICY, sample["title"], sample["text"], 9))["action"] == "draft_created"
    rows = routes.list_events(need_id=POLICY, limit=50, db=db, _=user)
    assert rows and all(r["event_id"].startswith("POL-") for r in rows)
    assert "subject" in rows[0] and "dim1" in rows[0]


def test_default_need_is_a_setting(monkeypatch):
    from app.config import settings
    from app.services import money_guard
    monkeypatch.setattr(settings, "default_need_id", POLICY)
    assert need_ctx.default_need_id() == POLICY
    assert money_guard.tristate_fields() == []
    monkeypatch.setattr(settings, "default_need_id", SEC)
    assert money_guard.tristate_fields() == ["loss_L1", "loss_L2", "loss_L3", "loss_L4", "loss_L5"]


# ---------------- 引擎代码不得含行业字面量 ----------------

BANNED = ["网络安全", "数据泄露", "勒索", "loss_L", "sellable_mapping", "security_controls", "通报情报",
          "单一事件", "不该入库", "医疗卫生", "attack_type", "sec_events", "网警", "org_name", "industry_l1",
          "severity", "consequences", "org_uscc", "province", "policy_watch"]
ALLOW_FILES = {"need_ctx.py"}      # 角色→物理列映射只允许出现在这里


def _string_literals(path: pathlib.Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docs = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body and isinstance(body[0], ast.Expr) \
                and isinstance(getattr(body[0], "value", None), ast.Constant) and isinstance(body[0].value.value, str):
            docs.add(id(body[0].value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
            yield node.lineno, node.value


def test_engine_code_has_no_domain_literals():
    files = [*(ROOT / "app" / "services").glob("*.py"), *(ROOT / "app" / "api").glob("*.py"),
             ROOT / "app" / "cli.py", ROOT / "app" / "main.py"]
    hits = []
    for f in files:
        if f.name in ALLOW_FILES:
            continue
        for lineno, val in _string_literals(f):
            bad = next((b for b in BANNED if b in val), None)
            if bad:
                hits.append(f"{f.relative_to(ROOT)}:{lineno} 含『{bad}』: {val[:60]!r}")
    assert not hits, "\n".join(hits)
