"""源库"越来越全、越来越准"的三块能力 + 冗余度(不轻易判死源)。

1) 主动找源(D5):用找源专用检索词去搜索引擎捞新渠道,而不是只等已采文章引用;
2) 候选源 LLM 相关度初评:候选评分不再只看"被提到几次",也看"提的是不是这行的内容";
3) 覆盖度盘点:哪些行业近 N 天一条事件都没有 → 自动生成该方向的找源词,形成闭环;
4) 冗余度:没有哪个站天天出稿,连续没产出不等于源坏了,不能说停就停。
"""
from datetime import datetime, timedelta

import pytest

from app.config import settings
from app.models import Event, Source, SourceProbe
from app.services import coverage, health, prospect
from app.services.adapters import DiscoveredItem


# ---------------- ① 主动找源 ----------------

class _FakeEngine:
    """假搜索引擎:每个词返回固定结果,含应被跳过的大平台链接。"""

    def __init__(self, *_a, **_k):
        pass

    def search_page(self, query, page, time_filter=None):
        if page > 0:
            return None
        return [DiscoveredItem(url="https://newsec-media.cn/a/1.html", title=f"{query} 相关渠道"),
                DiscoveredItem(url="https://zhihu.com/q/1", title="知乎回答"),
                DiscoveredItem(url="https://baidu.com/s?wd=x", title="百度搜索"),
                DiscoveredItem(url="https://another-sec.cn/list.html", title="另一个安全站")]


def test_prospect_registers_new_candidates(db, need, monkeypatch):
    from app.models import SourceDiscoveryEvidence
    monkeypatch.setattr(prospect, "_engines", lambda: [("fake", _FakeEngine())])
    monkeypatch.setattr(prospect, "build_queries", lambda *a, **k: ["安全周报 汇总 推荐"])
    r = prospect.run_once(db, need.id)
    assert r["queries"] == 1
    keys = {e.identity_key for e in db.query(SourceDiscoveryEvidence)
            .filter_by(channel="source_search").all()}
    assert "newsec-media.cn" in keys and "another-sec.cn" in keys
    assert "zhihu.com" not in keys and "baidu.com" not in keys   # 通用大平台/搜索引擎自身跳过


def test_prospect_queries_include_coverage_gaps(db, need, monkeypatch):
    """找源词 = 人工维护的基础词 + 覆盖空白自动生成的方向词(缺哪块找哪块)。"""
    monkeypatch.setattr(prospect, "base_queries", lambda: ["安全周报 汇总 推荐"])
    monkeypatch.setattr(coverage, "prospect_queries", lambda *a, **k: ["医疗卫生 网络安全 事件 通报 公众号"])
    qs = prospect.build_queries(db, need.id)
    assert "安全周报 汇总 推荐" in qs and "医疗卫生 网络安全 事件 通报 公众号" in qs


def test_prospect_query_cap(db, need, monkeypatch):
    monkeypatch.setattr(settings, "prospect_query_cap", 3)
    monkeypatch.setattr(prospect, "base_queries", lambda: [f"词{i}" for i in range(20)])
    monkeypatch.setattr(coverage, "prospect_queries", lambda *a, **k: [])
    assert len(prospect.build_queries(db, need.id)) == 3


# ---------------- ② 候选源 LLM 相关度初评 ----------------

def test_probe_scores_candidate(db, monkeypatch):
    monkeypatch.setattr(prospect, "_sample_titles",
                        lambda k, n: ["某公司数据泄露事件通报", "勒索病毒攻击某医院"])

    class _LLM:
        def complete_json(self, system, user, retries=2):
            return {"relevance": 0.85, "reason": "持续产出国内安全事件内容"}

    monkeypatch.setattr("app.services.llm.get_screen_llm", lambda: _LLM())
    row = prospect.probe_one(db, "probe-site.cn", force=True)
    assert row.ok and row.relevance == 0.85
    assert prospect.llm_scores(db).get("probe-site.cn") == 0.85


def test_probe_unreachable_site_marked_not_ok(db, monkeypatch):
    monkeypatch.setattr(prospect, "_sample_titles", lambda k, n: [])
    row = prospect.probe_one(db, "dead-site.cn", force=True)
    assert row.ok is False and row.relevance == 0.0
    assert "dead-site.cn" not in prospect.llm_scores(db)   # 未初评的不当 0 分拉低,只是不计入


def test_llm_relevance_lifts_candidate_score(db, monkeypatch):
    """初评分要真的进评分公式——此前 llm_scores 从没人传,这一项恒为 0。"""
    from app.services import discovery
    discovery.record_evidence(db, "https://scored-site.cn/a", "citation")
    discovery.record_evidence(db, "https://scored-site.cn/b", "event_search")
    base = discovery.candidate_score(db, "scored-site.cn", 0.0)
    lifted = discovery.candidate_score(db, "scored-site.cn", 1.0)
    assert lifted > base                      # 权重 weight_llm_relevance 生效
    db.add(SourceProbe(identity_key="scored-site.cn", relevance=1.0, ok=True))
    db.flush()
    assert prospect.llm_scores(db)["scored-site.cn"] == 1.0


# ---------------- ③ 覆盖度盘点 ----------------

def test_coverage_marks_empty_industries(db, need, monkeypatch):
    monkeypatch.setattr(settings, "coverage_window_days", 90)
    monkeypatch.setattr(settings, "coverage_min_events", 3)
    for i in range(4):
        db.add(Event(event_id=f"COV-{i}", need_id=need.id, payload={}, status="draft",
                     industry_l1="金融", created_at=datetime.utcnow()))
    db.flush()
    rows = coverage.industry_coverage(db, need.id)
    by = {r["industry"]: r for r in rows}
    assert by["金融"]["events"] >= 4 and by["金融"]["gap"] is False
    assert by["医疗卫生"]["events"] == 0 and by["医疗卫生"]["level"] == "空白"
    assert rows[0]["events"] <= rows[-1]["events"]        # 最缺的排最前


def test_coverage_generates_prospect_queries(db, need, monkeypatch):
    monkeypatch.setattr(settings, "coverage_min_events", 3)
    qs = coverage.prospect_queries(db, need.id)
    assert any("医疗卫生" in q for q in qs)               # 空白行业 → 生成该方向找源词
    assert all("公众号" in q or "网站" in q or "公告" in q for q in qs)   # 找的是渠道不是事件


def test_coverage_summary_lists_silent_sources(db, need):
    s = Source(name="从没产出的源", kind="page", adapter="generic_list", credibility="S3",
               tier="B", lifecycle="active", serves_needs=[need.id],
               entry_url="https://silent-x.cn/col/")
    db.add(s); db.flush()
    out = coverage.summary(db, need.id)
    assert out["sources_active"] >= 1
    assert "从没产出的源" in out["sources_silent"]
    assert out["gap_count"] >= 1 and out["prospect_queries"]


# ---------------- ④ 冗余度:不轻易判死源 ----------------

def _src(db, need, **kw):
    base = dict(name="容忍度测试源", kind="page", adapter="generic_list", credibility="S3",
                tier="B", lifecycle="active", serves_needs=[need.id],
                entry_url="https://tol-x.cn/col/")
    base.update(kw)
    s = Source(**base)
    db.add(s); db.flush()
    return s


def test_recent_source_only_watched_not_retired(db, need, monkeypatch):
    """刚刚还成功过的源,连续几轮没新内容只标『观察中』,绝不停用。"""
    monkeypatch.setattr(settings, "source_auto_retire_fail_streak", 2)
    monkeypatch.setattr(settings, "source_quiet_tolerance_days", 30)
    s = _src(db, need, last_success_at=datetime.utcnow() - timedelta(days=3), fail_streak=1)
    v = health.register_failure(db, s)
    assert v["retired"] is False and v["watching"] is True
    assert s.lifecycle == "active"
    assert (s.adapter_config or {}).get("watch_since")
    assert "观察中" in v["note"]


def test_long_quiet_source_is_retired(db, need, monkeypatch):
    monkeypatch.setattr(settings, "source_auto_retire_fail_streak", 2)
    monkeypatch.setattr(settings, "source_quiet_tolerance_days", 30)
    s = _src(db, need, entry_url="https://tol-y.cn/col/",
             last_success_at=datetime.utcnow() - timedelta(days=99), fail_streak=1)
    v = health.register_failure(db, s)
    assert v["retired"] is True and s.lifecycle == "retired"
    assert (s.adapter_config or {}).get("auto_retired_at")


def test_official_source_never_auto_retired(db, need, monkeypatch):
    """S1/S2 官方源低频但不可替代,误杀代价远大于留着空跑——无论多久都不自动停用。"""
    monkeypatch.setattr(settings, "source_auto_retire_fail_streak", 2)
    monkeypatch.setattr(settings, "source_quiet_tolerance_days", 30)
    monkeypatch.setattr(settings, "auto_retire_protect_credibility", "S1,S2")
    s = _src(db, need, entry_url="https://gov-tol.cn/col/", credibility="S1",
             last_success_at=datetime.utcnow() - timedelta(days=400), fail_streak=9)
    v = health.register_failure(db, s)
    assert v["retired"] is False and v["watching"] is True and s.lifecycle == "active"


def test_success_clears_watch(db, need, monkeypatch):
    monkeypatch.setattr(settings, "source_auto_retire_fail_streak", 1)
    monkeypatch.setattr(settings, "source_quiet_tolerance_days", 30)
    s = _src(db, need, entry_url="https://tol-z.cn/col/",
             last_success_at=datetime.utcnow() - timedelta(days=2))
    health.register_failure(db, s)
    assert (s.adapter_config or {}).get("watch_since")
    health.register_success(db, s)
    assert s.fail_streak == 0 and not (s.adapter_config or {}).get("watch_since")


def test_auto_retired_source_is_rechecked(db, need, monkeypatch):
    """误杀自愈:自动停用的源到期会进体检名单;人工停的不动。"""
    monkeypatch.setattr(settings, "retired_recheck_days", 14)
    old = (datetime.utcnow() - timedelta(days=30)).isoformat(timespec="seconds")
    auto = _src(db, need, entry_url="https://revive-a.cn/col/", lifecycle="retired",
                adapter_config={"auto_retired_at": old})
    manual = _src(db, need, entry_url="https://revive-b.cn/col/", lifecycle="retired",
                  adapter_config={"manually_retired": True, "auto_retired_at": old})
    fresh = _src(db, need, entry_url="https://revive-c.cn/col/", lifecycle="retired",
                 adapter_config={"auto_retired_at":
                                 (datetime.utcnow() - timedelta(days=1)).isoformat(timespec="seconds")})
    ids = [s.id for s in health.recheck_due(db, need.id)]
    assert auto.id in ids
    assert manual.id not in ids and fresh.id not in ids


def test_column_min_articles_default_is_lenient():
    """低频栏目(一年几条的执法通报)整页也就三五条,门槛不能卡在 5。"""
    assert settings.column_min_articles <= 3


@pytest.fixture(autouse=True)
def _clean_probes(db):
    yield
    db.query(SourceProbe).delete()
    db.flush()
