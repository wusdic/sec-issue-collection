"""找源关键词进化机制。

核心要回答的问题是用户提的那个:「零售 数据泄露」有可能还不如只写「数据泄露」。
机制必须能用可观测的事实把这件事判出来,而不是靠人拍脑袋改配置。
"""
from datetime import datetime, timedelta

import pytest

from app.config import settings
from app.models import RawDocument, SearchQueryStat, Source, TermStat
from app.services import autopilot, discovery, prospect, query_evolution as qe


@pytest.fixture(autouse=True)
def _clean_ledger(db):
    """词表台账是全局的,别的测试跑 run_once 时也会往里写(而且会 commit)。
    这批用例断言的是精确的词表状态,所以每条用例开始前先清空。"""
    db.query(TermStat).delete()
    db.query(SearchQueryStat).delete()
    db.commit()
    yield
    db.query(TermStat).delete()
    db.query(SearchQueryStat).delete()
    db.commit()


def _run(db, need, query, *, results=0, usable=0, new=0, times=1):
    for _ in range(times):
        qe.record_run(db, need.id, query, results=results, usable=usable, new_channels=new)
    db.flush()


# ---------------- 记账 ----------------

def test_records_per_query_yield(db, need, monkeypatch):
    """每条词的产出必须单独记账,否则词表根本没有可进化的依据。"""
    class _E:
        def __init__(self, *_a, **_k): pass
        def search_page(self, q, page, tf=None):
            if page:
                return None
            n = 3 if q == "网警 处罚" else 1
            return [__import__("app.services.adapters", fromlist=["x"]).DiscoveredItem(
                url=f"https://qe-{q.replace(' ', '')}-{i}.gov.cn/a.html", title="通报")
                for i in range(n)]

    monkeypatch.setattr(prospect, "_engines", lambda: [("bing_rss", _E())])
    monkeypatch.setattr(prospect, "build_queries", lambda *a, **k: ["网警 处罚", "网信办 通报"])
    r = prospect.run_once(db, need.id)
    yields = {x["query"]: x for x in r["query_yield"]}
    assert yields["网警 处罚"]["new_channels"] == 3
    assert yields["网信办 通报"]["new_channels"] == 1
    rows = {s.query: s for s in db.query(SearchQueryStat).filter_by(need_id=need.id).all()}
    assert rows["网警 处罚"].new_channels == 3 and rows["网警 处罚"].runs == 1


def test_value_weights_admitted_highest(db, need):
    """"真的多了一个能用的源"最值钱;原始结果条数权重压到很低——必应那 900 条页脚
    已经证明原始条数最容易被灌水。"""
    _run(db, need, "甲", results=1000, usable=0, new=0)
    _run(db, need, "乙", results=5, usable=5, new=1)
    db.flush()
    rows = {s.query: s for s in db.query(SearchQueryStat).filter_by(need_id=need.id).all()}
    assert qe.value_of(rows["乙"]) > qe.value_of(rows["甲"])


def test_admitted_credited_back_to_query(db, need):
    """候选真进了源库,功劳要记回当初捞到它的那条词——这是最强的正反馈。"""
    _run(db, need, "网警 处罚", results=3, usable=3, new=1)
    discovery.record_evidence(db, "https://credit-me.gov.cn/a.html", "source_search",
                              display_name="某局", found_by_query="网警 处罚")
    db.flush()
    ev = db.query(discovery.SourceDiscoveryEvidence).filter_by(
        identity_key="credit-me.gov.cn").first()
    assert ev.found_by_query == "网警 处罚"
    discovery._credit_query(db, need.id, [ev])
    db.flush()
    row = db.query(SearchQueryStat).filter_by(need_id=need.id, query="网警 处罚").one()
    assert row.admitted == 1


# ---------------- 增益:这正是「零售 数据泄露」那个问题 ----------------

def test_useless_modifier_detected_and_demoted(db, need, monkeypatch):
    """「零售 数据泄露」不如「数据泄露」 → 增益 <1 → 零售退出组合池(但仍可单独跑)。"""
    monkeypatch.setattr(settings, "term_min_samples", 2)
    _run(db, need, "数据泄露", results=40, usable=20, new=4, times=2)      # 锚点单独:高产
    _run(db, need, "个人信息", results=30, usable=15, new=3, times=2)
    _run(db, need, "数据泄露 零售", results=3, usable=1, new=0, times=2)   # 加了限定:被 AND 压死
    _run(db, need, "个人信息 零售", results=2, usable=1, new=0, times=2)
    r = qe.compute_term_stats(db, need.id)
    db.flush()
    weak = {w["term"] for w in r["weak"]}
    assert "零售" in weak
    t = db.query(TermStat).filter_by(need_id=need.id, term="零售").one()
    assert t.lift < 1 and t.state == "weak" and "不如锚点单独搜" in (t.note or "")


def test_helpful_modifier_kept(db, need, monkeypatch):
    """真的能带来增量的限定词不能被误杀。"""
    monkeypatch.setattr(settings, "term_min_samples", 2)
    _run(db, need, "数据泄露", results=20, usable=5, new=1, times=2)
    _run(db, need, "个人信息", results=20, usable=5, new=1, times=2)
    _run(db, need, "数据泄露 通报", results=40, usable=20, new=5, times=2)
    _run(db, need, "个人信息 通报", results=35, usable=18, new=4, times=2)
    qe.compute_term_stats(db, need.id)
    db.flush()
    t = db.query(TermStat).filter_by(need_id=need.id, term="通报").one()
    assert t.lift > 1 and t.state == "active"
    assert "通报" not in qe.weak_terms(db, need.id)


def test_no_verdict_without_enough_samples(db, need, monkeypatch):
    """样本不够不下结论——刻意的冗余,宁可晚一轮判也不误杀。"""
    monkeypatch.setattr(settings, "term_min_samples", 3)
    _run(db, need, "数据泄露", results=40, usable=20, new=4, times=2)
    _run(db, need, "数据泄露 冷门", results=1, usable=0, new=0, times=2)
    qe.compute_term_stats(db, need.id)
    db.flush()
    t = db.query(TermStat).filter_by(need_id=need.id, term="冷门").one()
    assert t.lift < 1 and t.state == "active"      # 算了增益,但还不判死


def test_no_verdict_without_baseline(db, need, monkeypatch):
    """锚点没单独跑过就算不出增益,不能瞎判。"""
    monkeypatch.setattr(settings, "term_min_samples", 1)
    _run(db, need, "数据泄露 零售", results=1, usable=0, new=0, times=3)
    qe.compute_term_stats(db, need.id)
    db.flush()
    assert db.query(TermStat).filter_by(need_id=need.id, term="零售").one_or_none() is None


def test_weak_modifier_excluded_from_next_plan(db, need, monkeypatch):
    """判定为弱之后,含它的组合不再排期;它单独跑仍然允许。"""
    monkeypatch.setattr(settings, "term_min_samples", 1)
    _run(db, need, "数据泄露", results=40, usable=20, new=4, times=2)
    _run(db, need, "数据泄露 零售", results=1, usable=0, new=0, times=2)
    qe.compute_term_stats(db, need.id)
    db.flush()
    picked = qe.plan(db, need.id, 20, seed=["数据泄露", "数据泄露 零售", "零售"])
    assert "数据泄露 零售" not in picked
    assert "零售" in picked          # 单独跑不受影响


def test_weak_modifier_can_recover(db, need, monkeypatch):
    """反爬/淡季导致的误判要能自愈:增益回到 1 以上就重新参与组合。"""
    monkeypatch.setattr(settings, "term_min_samples", 1)
    _run(db, need, "数据泄露", results=40, usable=20, new=2, times=2)
    _run(db, need, "数据泄露 零售", results=1, usable=0, new=0, times=2)
    qe.compute_term_stats(db, need.id)
    db.flush()
    assert "零售" in qe.weak_terms(db, need.id)
    _run(db, need, "数据泄露 零售", results=90, usable=60, new=12, times=2)
    r = qe.compute_term_stats(db, need.id)
    db.flush()
    assert any(x["term"] == "零售" for x in r["recovered"])
    assert "零售" not in qe.weak_terms(db, need.id)


# ---------------- 淘汰与休整:冗余度 ----------------

def test_barren_query_rests_not_retired(db, need, monkeypatch):
    """连着几轮空手只是"歇着",不是判死——没有哪个网站天天都出一堆报道。"""
    monkeypatch.setattr(settings, "query_rest_barren", 3)
    _run(db, need, "冷门 通报", results=8, usable=4, new=0, times=3)
    r = qe.prune(db, need.id)
    db.flush()
    assert "冷门 通报" in r["rested"] and "冷门 通报" not in r["retired"]
    row = db.query(SearchQueryStat).filter_by(need_id=need.id, query="冷门 通报").one()
    assert row.state == "resting" and "不是淘汰" in (row.note or "")


def test_resting_query_gets_revived(db, need, monkeypatch):
    """歇着的词要按复活配额定期复测,否则低频好词会被永久埋掉。"""
    monkeypatch.setattr(settings, "query_rest_barren", 2)
    monkeypatch.setattr(settings, "query_share_revive", 0.5)
    _run(db, need, "低频 通报", results=8, usable=4, new=0, times=2)
    qe.prune(db, need.id)
    db.flush()
    picked = qe.plan(db, need.id, 4, seed=["低频 通报", "网警 处罚"])
    assert "低频 通报" in picked


def test_only_truly_dead_query_retired(db, need, monkeypatch):
    """真退休的门槛很高:跑满多轮且一条有效结果都没出过。"""
    monkeypatch.setattr(settings, "query_retire_runs", 4)
    _run(db, need, "彻底没用的词", results=6, usable=0, new=0, times=4)
    _run(db, need, "偶尔有用的词", results=6, usable=2, new=0, times=4)
    r = qe.prune(db, need.id)
    db.flush()
    assert "彻底没用的词" in r["retired"]
    assert "偶尔有用的词" not in r["retired"]


def test_retired_query_not_scheduled(db, need, monkeypatch):
    monkeypatch.setattr(settings, "query_retire_runs", 2)
    _run(db, need, "废词", results=4, usable=0, new=0, times=2)
    qe.prune(db, need.id)
    db.flush()
    assert "废词" not in qe.plan(db, need.id, 30, seed=["废词", "网警 处罚"])


# ---------------- 变异:词表要自己长 ----------------

def test_drop_variant_proposed_for_combo(db, need):
    """给高产组合派生"去掉限定词"的对照词——这正是检验限定词有没有用的手段。"""
    _run(db, need, "数据泄露 通报", results=40, usable=20, new=5, times=2)
    qe.mutate(db, need.id)
    db.flush()
    row = db.query(SearchQueryStat).filter_by(need_id=need.id, query="数据泄露").one()
    assert row.origin == "drop" and row.parent == "数据泄露 通报" and row.runs == 0


def test_harvest_new_terms_from_corpus(db, need):
    """词表唯一真正"跟着环境长"的入口:从已采到且判为相关的标题里挖新词。"""
    src = Source(name="语料源", kind="page", adapter="generic_list", credibility="S3",
                 tier="B", lifecycle="active", serves_needs=[need.id],
                 entry_url="https://corpus-a.cn/", site_key="corpus-a.cn")
    db.add(src); db.flush()
    others = ["某地通报三起网络违法案件", "某平台被责令整改", "某单位落实主体责任",
              "某公司被立案调查", "行业安全形势分析"]
    for i in range(40):
        # 一半语料含"数据出境",另一半是别的题材——真实语料本来就参差,
        # 全一样的标题会被"几乎每篇都有=套话"那条规则挡掉
        title = (f"某公司因违反数据出境安全评估要求被约谈第{i}号" if i % 2 == 0
                 else f"{others[i % len(others)]}第{i}号")
        db.add(RawDocument(need_id=need.id, source_id=src.id,
                           url=f"https://corpus-a.cn/n/{i}.html",
                           url_normalized=f"https://corpus-a.cn/n/{i}.html",
                           title=title,
                           screen_status="screened_in", fetched_at=datetime.utcnow()))
    db.flush()
    words = qe.harvest_terms(db, need.id, top_n=5)
    assert any("数据出境" in w or "安全评估" in w for w in words), words
    assert all(len(w) >= 3 for w in words)


def test_harvest_skips_thin_corpus(db, need, monkeypatch):
    """语料太少时挖出来的都是噪声,宁可不挖。"""
    monkeypatch.setattr(settings, "harvest_min_titles", 500)
    assert qe.harvest_terms(db, need.id) == []


def test_harvested_terms_enter_explore_queue(db, need, monkeypatch):
    monkeypatch.setattr(qe, "harvest_terms", lambda *a, **k: ["数据出境"])
    _run(db, need, "数据泄露 通报", results=40, usable=20, new=5, times=2)
    r = qe.mutate(db, need.id)
    db.flush()
    assert "数据出境" in r["harvested"]
    row = db.query(SearchQueryStat).filter_by(need_id=need.id, query="数据出境").one()
    assert row.origin == "harvest"


def test_mutations_never_exceed_two_words(db, need, monkeypatch):
    """词堆多了搜索引擎按 AND 收紧,召回会被压死——变异出来的词也必须守住 2 词上限。"""
    monkeypatch.setattr(qe, "harvest_terms", lambda *a, **k: ["数据出境"])
    _run(db, need, "数据泄露 通报", results=40, usable=20, new=5, times=2)
    qe.mutate(db, need.id)
    db.flush()
    for s in db.query(SearchQueryStat).filter_by(need_id=need.id).all():
        assert len(s.query.split()) <= 2, s.query


# ---------------- 排期 ----------------

def test_plan_reserves_baseline_explore_revive(db, need, monkeypatch):
    """配额必须留出基线和探索:没基线算不出增益,没探索词表永远长不出新东西。"""
    monkeypatch.setattr(settings, "query_share_baseline", 0.2)
    monkeypatch.setattr(settings, "query_share_explore", 0.25)
    _run(db, need, "数据泄露 通报", results=40, usable=20, new=5, times=2)
    qe.get_or_create(db, need.id, "数据泄露", origin="drop")
    qe.get_or_create(db, need.id, "全新的词", origin="harvest")
    db.flush()
    picked = qe.plan(db, need.id, 10, seed=["数据泄露 通报"])
    assert "数据泄露" in picked          # 基线
    assert "全新的词" in picked          # 探索
    assert "数据泄露 通报" in picked     # 利用


def test_plan_keeps_coverage_priority(db, need, monkeypatch):
    """cap 截断时,"缺哪块补哪块"的方向词仍要排在通用组合前面。"""
    seed = ["医疗 数据泄露"] + [f"通用{i} 处罚" for i in range(50)]
    picked = qe.plan(db, need.id, 12, seed=seed)
    assert "医疗 数据泄露" in picked and len(picked) == 12


def test_plan_respects_cap(db, need):
    picked = qe.plan(db, need.id, 7, seed=[f"词{i} 处罚" for i in range(40)])
    assert len(picked) == 7 and len(set(picked)) == 7


def test_build_queries_falls_back_when_evolution_off(db, need, monkeypatch):
    """关掉进化就回到静态清单,不能因此把找源整个搞挂。"""
    monkeypatch.setattr(settings, "query_evolution_enabled", False)
    monkeypatch.setattr(settings, "prospect_query_cap", 5)
    qs = prospect.build_queries(db, need.id)
    assert len(qs) == 5 and qs == prospect.seed_queries(db, need.id)[:5]


def test_evolution_failure_does_not_break_prospecting(db, need, monkeypatch):
    """进化机制出问题也不该让找源停摆。"""
    monkeypatch.setattr(settings, "prospect_query_cap", 6)
    monkeypatch.setattr(qe, "plan", lambda *a, **k: 1 / 0)
    qs = prospect.build_queries(db, need.id)
    assert len(qs) == 6


# ---------------- 接线 ----------------

def test_queries_is_an_autopilot_task_before_prospect(db, need):
    """进化排在主动找源之前,当轮就能用上新词表。"""
    names = [t[0] for t in autopilot.TASKS]
    assert "queries" in names and names.index("queries") < names.index("prospect")
    assert "queries" in autopilot._ACTIONS


def test_evolve_records_action(db, need, monkeypatch):
    """词表被自动改动过必须留痕,否则又是一个黑箱。"""
    from app.models import ActionLog
    monkeypatch.setattr(settings, "term_min_samples", 1)
    _run(db, need, "数据泄露", results=40, usable=20, new=4, times=2)
    _run(db, need, "数据泄露 零售", results=1, usable=0, new=0, times=2)
    db.commit()
    autopilot._do_queries(db, need.id)
    row = (db.query(ActionLog).filter_by(action="source.queries_evolved")
           .order_by(ActionLog.id.desc()).first())
    assert row and "零售" in (row.title or "")


def test_report_shows_top_and_weak(db, need, monkeypatch):
    monkeypatch.setattr(settings, "term_min_samples", 1)
    _run(db, need, "数据泄露", results=40, usable=20, new=4, times=2)
    _run(db, need, "数据泄露 零售", results=1, usable=0, new=0, times=2)
    qe.compute_term_stats(db, need.id)
    db.flush()
    rep = qe.report(db, need.id)
    assert rep["top"][0]["query"] == "数据泄露"
    assert any(t["term"] == "零售" for t in rep["weak_terms"])
    assert rep["by_state"]["active"] >= 1
