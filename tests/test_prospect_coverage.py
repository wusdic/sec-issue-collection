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
    assert any("医疗" in q for q in qs)                   # 空白行业 → 生成该方向找源词(行业名取短)
    # 词越多召回越窄,找源要的是广度:每条不超过 3 个词
    assert all(len(q.split()) <= 3 for q in qs), qs


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


# ---------------- ⑤ "0 条"必须说得出为什么 ----------------

class _BlockedEngine:
    """被反爬挡住:search_page 返回 None(抓不到)。"""
    def __init__(self, *_a, **_k): pass
    def search_page(self, query, page, time_filter=None): return None


class _RedirectEngine:
    """结果全是搜索引擎自家跳转链(百度/必应的真实形态)。"""
    def __init__(self, *_a, **_k): pass
    def search_page(self, query, page, time_filter=None):
        if page > 0:
            return None
        return [DiscoveredItem(url="https://www.baidu.com/link?url=abc123", title="某安全站"),
                DiscoveredItem(url="https://www.baidu.com/link?url=def456", title="另一个站")]


def test_blocked_engine_explains_itself(db, need, monkeypatch):
    monkeypatch.setattr(prospect, "_engines", lambda: [("baidu_search", _BlockedEngine())])
    monkeypatch.setattr(prospect, "build_queries", lambda *a, **k: ["安全周报 汇总 推荐"])
    r = prospect.run_once(db, need.id)
    assert r["hits"] == 0 and not r["new_keys"]
    assert "抓不到" in r["note"] and "403" in r["note"]       # 不再是一排没头没脑的 0
    assert r["stats"]["fetch_fail"] >= 1
    assert r["engine_detail"][0]["errors"] >= 1


def test_search_redirect_links_are_resolved(db, need, monkeypatch):
    """百度结果是 baidu.com/link?url=… ,不还原就只能得到 baidu.com → 永远 0 个新候选。"""
    monkeypatch.setattr(prospect, "_engines", lambda: [("baidu_search", _RedirectEngine())])
    monkeypatch.setattr(prospect, "build_queries", lambda *a, **k: ["安全周报 汇总 推荐"])
    seen = {}

    def fake_resolve(url):
        seen[url] = True
        return "https://real-sec-site.cn/news/1.html"

    monkeypatch.setattr(prospect, "_resolve_redirect", fake_resolve)
    r = prospect.run_once(db, need.id)
    assert len(seen) == 2 and r["stats"]["resolved"] == 2
    assert "real-sec-site.cn" in r["new_keys"]


def test_unresolvable_redirects_reported_not_silent(db, need, monkeypatch):
    monkeypatch.setattr(prospect, "_engines", lambda: [("baidu_search", _RedirectEngine())])
    monkeypatch.setattr(prospect, "build_queries", lambda *a, **k: ["安全周报 汇总 推荐"])
    monkeypatch.setattr(prospect, "_resolve_redirect", lambda u: u)   # 还原失败
    r = prospect.run_once(db, need.id)
    assert not r["new_keys"] and r["stats"]["redirect"] == 2 and r["stats"]["resolved"] == 0
    assert "跳转链" in r["note"] and "还原失败" in r["note"]


def test_resolve_budget_is_capped(db, need, monkeypatch):
    monkeypatch.setattr(settings, "prospect_resolve_max", 1)
    monkeypatch.setattr(prospect, "_engines", lambda: [("baidu_search", _RedirectEngine())])
    monkeypatch.setattr(prospect, "build_queries", lambda *a, **k: ["安全周报 汇总 推荐"])
    calls = {"n": 0}

    def fake_resolve(url):
        calls["n"] += 1
        return "https://capped-site.cn/a"

    monkeypatch.setattr(prospect, "_resolve_redirect", fake_resolve)
    prospect.run_once(db, need.id)
    assert calls["n"] == 1      # 超配额不再逐条发请求


def test_all_platform_results_explained(db, need, monkeypatch):
    class _PlatformEngine:
        def __init__(self, *_a, **_k): pass
        def search_page(self, q, page, time_filter=None):
            return None if page else [DiscoveredItem(url="https://zhihu.com/q/1", title="知乎")]
    monkeypatch.setattr(prospect, "_engines", lambda: [("bing_search", _PlatformEngine())])
    monkeypatch.setattr(prospect, "build_queries", lambda *a, **k: ["安全周报 汇总 推荐"])
    r = prospect.run_once(db, need.id)
    assert not r["new_keys"] and r["stats"]["platform"] == 1
    assert "通用大平台" in r["note"]


def test_no_engine_configured_explained(db, need, monkeypatch):
    monkeypatch.setattr(prospect, "_engines", lambda: [])
    monkeypatch.setattr(prospect, "build_queries", lambda *a, **k: ["x"])
    assert "没有可用的搜索引擎" in prospect.run_once(db, need.id)["note"]


# ---------------- ⑥ 引擎选择器失配不该静默 0 条 ----------------

_BAIDU_LIKE = """<html><body>
  <div class="result"><div class="c-title-new"><a href="http://www.baidu.com/link?url=AAA">
    某安全媒体 - 数据泄露事件盘点</a></div></div>
  <div class="result"><div class="c-title-new"><a href="http://www.baidu.com/link?url=BBB">
    另一个渠道 - 勒索攻击追踪</a></div></div>
  <a href="https://www.baidu.com/">百度首页</a>
  <a href="https://www.baidu.com/s?wd=x&pn=10">下一页</a>
</body></html>"""


def test_generic_fallback_when_selector_misses():
    """百度改版后 h3 a 取不到 → 通用抽链兜底,不再静默返回 0 条。"""
    from app.services.adapters import BaiduSearchAdapter
    a = BaiduSearchAdapter(prospect._Shim())
    assert a.parse('<div><h3><a href="https://x.cn/1">标题够长的结果一</a></h3></div>')   # 选择器命中时照旧
    items = a.parse(_BAIDU_LIKE)
    urls = [i.url for i in items]
    assert len(items) == 2                                   # 两条结果都抽到了
    assert all("link?url=" in u for u in urls)                # 保留跳转链(后续会还原)
    assert "https://www.baidu.com/" not in urls               # 引擎自身导航被滤掉


def test_generic_fallback_keeps_external_links():
    from app.services.adapters import BingSearchAdapter
    a = BingSearchAdapter(prospect._Shim())
    html = ('<div><a href="https://real-site.cn/a">某站的一篇很长的标题</a></div>'
            '<a href="https://cn.bing.com/search?q=x">下一页</a><a href="https://y.cn/b">短</a>')
    urls = [i.url for i in a.parse(html)]
    assert urls == ["https://real-site.cn/a"]                # 外链留下,引擎导航与超短文本滤掉


def test_blocked_page_detected():
    from app.services.adapters import BaiduSearchAdapter
    a = BaiduSearchAdapter(prospect._Shim())
    assert a.looks_blocked("<html><title>百度安全验证</title>请完成安全验证</html>") is True
    assert a.looks_blocked(_BAIDU_LIKE) is False


def test_empty_page_reports_blocked_reason(db, need, monkeypatch):
    class _EmptyEngine:
        config = {"render": False}
        def __init__(self, *_a, **_k): pass
        def search_page(self, q, page, tf=None): return [] if page == 0 else None
        def build_url(self, q, page, tf): return "https://engine.example/s"
        def looks_blocked(self, html): return True
    monkeypatch.setattr(prospect, "_engines", lambda: [("baidu_search", _EmptyEngine())])
    monkeypatch.setattr(prospect, "build_queries", lambda *a, **k: ["找源词"])
    monkeypatch.setattr(prospect.fetcher, "fetch",
                        lambda *a, **k: prospect.fetcher.FetchResult("u", "u", 200, "百度安全验证"))
    r = prospect.run_once(db, need.id)
    assert r["stats"]["blocked_pages"] >= 1
    assert "验证页" in r["note"] and "浏览器渲染" in r["note"]


# ---------------- ⑦ 关键词策略:短词组合,别把召回压死 ----------------

def test_combo_queries_are_two_words():
    """「网信办 处罚 解读 公众号」几乎搜不到,「网警 处罚」能捞到一批号——找源词必须短。"""
    qs = prospect.combo_queries()
    assert qs, "配方没生成任何组合词"
    assert all(len(q.split()) <= 2 for q in qs), [q for q in qs if len(q.split()) > 2]
    assert "网警 处罚" in qs and "网信办 通报" in qs


def test_combo_queries_spread_both_dimensions():
    """截断时主体和动作两个维度都要均匀,不能整轮都配同一个动作。"""
    qs = [q for q in prospect.combo_queries()[:20] if len(q.split()) == 2]
    subjects = {q.split()[0] for q in qs}
    verbs = {q.split()[1] for q in qs}
    assert len(subjects) >= 8 and len(verbs) >= 8, (subjects, verbs)


def test_coverage_queries_rank_before_generic_combos(db, need, monkeypatch):
    """cap 截断时,"缺哪块补哪块"的方向词优先级高于通用组合词。"""
    monkeypatch.setattr(settings, "prospect_query_cap", 12)
    monkeypatch.setattr(coverage, "prospect_queries", lambda *a, **k: ["医疗 数据泄露"])
    qs = prospect.build_queries(db, need.id)
    assert "医疗 数据泄露" in qs and len(qs) == 12


# ---------------- ⑧ 平台号(公众号/百家号/微博)不该被当成大平台丢掉 ----------------

class _PlatformEngine:
    def __init__(self, *_a, **_k): pass
    def search_page(self, q, page, tf=None):
        if page > 0:
            return None
        return [DiscoveredItem(url="https://mp.weixin.qq.com/s/5XUyLv7701cgN0qfLxhEdQ?click_id=1",
                               title="网警通报某公司数据泄露"),
                DiscoveredItem(url="https://author.baidu.com/home?from=bjh_article&app_id=1807",
                               title="某安全作者"),
                DiscoveredItem(url="https://zhihu.com/q/1", title="知乎回答不要")]


def test_wechat_and_baijiahao_become_account_candidates(db, need, monkeypatch):
    """公众号文章按注册域会退化成 qq.com 被丢掉——而这正是最该收的一类源。"""
    from app.models import SourceDiscoveryEvidence
    monkeypatch.setattr(prospect, "_engines", lambda: [("sogou_wechat", _PlatformEngine())])
    monkeypatch.setattr(prospect, "build_queries", lambda *a, **k: ["网警 处罚"])
    monkeypatch.setattr(prospect, "_wechat_account", lambda url: "网安宣传")
    r = prospect.run_once(db, need.id)
    assert "mp:网安宣传" in r["new_keys"]          # 识别成"某个号"
    assert "bjh:1807" in r["new_keys"]             # 百家号作者页同理
    assert r["stats"]["new_wechat"] == 1 and r["stats"]["new_platform_account"] == 1
    assert r["stats"]["platform"] == 1             # 只有知乎被当大平台丢掉
    keys = {e.identity_key for e in db.query(SourceDiscoveryEvidence)
            .filter_by(channel="source_search").all()}
    assert "qq.com" not in keys and "baidu.com" not in keys


def test_wechat_resolve_budget(db, need, monkeypatch):
    monkeypatch.setattr(settings, "prospect_wechat_resolve_max", 0)   # 0=不解析
    monkeypatch.setattr(prospect, "_engines", lambda: [("sogou_wechat", _PlatformEngine())])
    monkeypatch.setattr(prospect, "build_queries", lambda *a, **k: ["网警 处罚"])
    calls = {"n": 0}
    monkeypatch.setattr(prospect, "_wechat_account",
                        lambda url: (calls.__setitem__("n", calls["n"] + 1), "x")[1])
    r = prospect.run_once(db, need.id)
    assert calls["n"] == 1        # cap=0 表示不限,仍会解析(0 视为无上限)
    assert "bjh:1807" in r["new_keys"]


def test_platform_account_becomes_usable_source(db, need, monkeypatch):
    """百家号候选达标入库后,入口应是该号主页(可直接采),而不是 https://bjh:1807/。"""
    from app.services import discovery
    monkeypatch.setattr(settings, "discovery_auto_trial_threshold", 0.1)
    discovery.record_evidence(db, None, "source_search", display_name="某安全作者",
                              platform_key="bjh:9911")
    discovery.record_evidence(db, None, "citation", display_name="某安全作者",
                              platform_key="bjh:9911")
    discovery.evaluate_candidates(db, need.id)
    s = db.query(Source).filter_by(site_key="bjh:9911").one()
    assert s.entry_url == "https://author.baidu.com/home?app_id=9911"
    assert s.kind == "page" and s.adapter == "generic_list"
    assert s.identity_key == "bjh:9911"


def test_drop_reasons_are_itemised(db, need, monkeypatch):
    """"已是现有源"和"已拉黑"是两回事,混成一个数字等于没说。"""
    from app.models import SourceBlacklist
    db.add(Source(name="已有源", kind="page", adapter="generic_list", credibility="S3", tier="B",
                  lifecycle="active", serves_needs=[need.id], entry_url="https://known-x.cn/c/",
                  site_key="known-x.cn"))
    db.add(SourceBlacklist(identity_key="banned-x.cn", reason="测试"))
    db.flush()

    class _E:
        def __init__(self, *_a, **_k): pass
        def search_page(self, q, page, tf=None):
            if page:
                return None
            return [DiscoveredItem(url="https://known-x.cn/a", title="已有源的文章"),
                    DiscoveredItem(url="https://banned-x.cn/a", title="拉黑站的文章")]

    monkeypatch.setattr(prospect, "_engines", lambda: [("bing_search", _E())])
    monkeypatch.setattr(prospect, "build_queries", lambda *a, **k: ["网警 处罚"])
    r = prospect.run_once(db, need.id)
    assert r["stats"]["already_source"] == 1 and r["stats"]["blacklisted"] == 1
    assert {d["key"] for d in r["dropped_top"]} == {"known-x.cn", "banned-x.cn"}
    assert "已有的源" in r["note"] and "已拉黑" in r["note"]


# ---------------- ⑨ 搜狗结果自带号名 / 已有源站点反推栏目 / 单通道也能入库 ----------------

class _SogouLike:
    """搜狗微信:结果页自带公众号名(不必逐条还原临时链)。"""
    def __init__(self, *_a, **_k): pass
    def search_page(self, q, page, tf=None):
        if page:
            return None
        return [DiscoveredItem(url="https://weixin.sogou.com/link?url=AAA", title="网警通报",
                               wechat_account="平安北京"),
                DiscoveredItem(url="https://weixin.sogou.com/link?url=BBB", title="处罚案例",
                               wechat_account="网信中国")]


def test_sogou_account_used_without_resolving(db, need, monkeypatch):
    """790 条搜狗结果此前全被当跳转链、受配额限制白丢——结果页本来就带号名。"""
    monkeypatch.setattr(prospect, "_engines", lambda: [("sogou_wechat", _SogouLike())])
    monkeypatch.setattr(prospect, "build_queries", lambda *a, **k: ["网警 处罚"])
    monkeypatch.setattr(prospect, "_resolve_redirect",
                        lambda u: pytest.fail("自带号名时不该再去还原跳转链"))
    monkeypatch.setattr(prospect, "_wechat_account",
                        lambda u: pytest.fail("自带号名时不该再抓文章解析"))
    r = prospect.run_once(db, need.id)
    assert set(r["new_keys"]) == {"mp:平安北京", "mp:网信中国"}
    assert r["stats"]["new_wechat"] == 2 and r["stats"]["redirect"] == 0


def test_redirect_budget_skips_are_counted(db, need, monkeypatch):
    """因配额跳过的跳转链必须计数并写进结论:此前 754 条静默丢弃,页面一个字没提。"""
    monkeypatch.setattr(settings, "prospect_resolve_max", 1)

    class _E:
        def __init__(self, *_a, **_k): pass
        def search_page(self, q, page, tf=None):
            if page:
                return None
            return [DiscoveredItem(url=f"https://www.baidu.com/link?url=X{i}", title=f"结果{i}")
                    for i in range(5)]

    monkeypatch.setattr(prospect, "_engines", lambda: [("baidu_search", _E())])
    monkeypatch.setattr(prospect, "build_queries", lambda *a, **k: ["网警 处罚"])
    monkeypatch.setattr(prospect, "_resolve_redirect", lambda u: "https://real-a.cn/x")
    r = prospect.run_once(db, need.id)
    assert r["stats"]["redirect"] == 5 and r["stats"]["resolved"] == 1
    assert r["stats"]["redirect_over_budget"] == 4
    assert "还原配额" in r["note"]


def test_hits_on_existing_site_become_columns(db, need, monkeypatch):
    """站点已有源 ≠ 该栏目已在采:miit.gov.cn 命中 162 条,应反推出漏采栏目而不是白丢。"""
    from app.services import columns
    parent = Source(name="工信部", kind="page", adapter="generic_rss", credibility="S1", tier="B",
                    lifecycle="active", serves_needs=[need.id], entry_url="https://miit-x.gov.cn/",
                    site_key="miit-x.gov.cn", identity_key="miit-x.gov.cn")
    db.add(parent); db.flush()

    class _E:
        def __init__(self, *_a, **_k): pass
        def search_page(self, q, page, tf=None):
            if page:
                return None
            return [DiscoveredItem(url=f"https://miit-x.gov.cn/tongbao/2026-07/{i}.html",
                                   title=f"通报{i}") for i in range(3)]

    monkeypatch.setattr(prospect, "_engines", lambda: [("bing_search", _E())])
    monkeypatch.setattr(prospect, "build_queries", lambda *a, **k: ["工信部 通报"])
    monkeypatch.setattr(columns, "validate_column",
                        lambda url, *a, **k: {"url": url, "valid": True, "article_count": 6,
                                              "consistency": 0.9, "relevance": 0.8})
    r = prospect.run_once(db, need.id)
    assert r["stats"]["already_source"] == 3
    assert r["stats"]["column_hints"] == 1 and r["new_columns"]
    kid = db.query(Source).filter_by(site_key="miit-x.gov.cn",
                                     discovered_from="column_auto").one()
    assert kid.entry_url.endswith("/tongbao/2026-07/")
    assert (kid.adapter_config or {}).get("parent_site_id") == parent.id
    assert "漏采栏目" in r["note"] or "补到" in r["note"]


def test_probe_verified_single_channel_can_register(db, need, monkeypatch):
    """主动找源是单通道,过不了多通道闸门 → 实测一轮 4 个候选入库 0 个。
    LLM 初评判定确实相关时,应视为第二重证据放行。"""
    from app.services import discovery
    monkeypatch.setattr(settings, "discovery_auto_trial_threshold", 0.1)
    monkeypatch.setattr(settings, "discovery_probe_pass", 0.7)
    discovery.record_evidence(db, "https://probed-new.cn/a", "source_search")
    # 没有初评分 → 单通道,仍不入库
    discovery.evaluate_candidates(db, need.id, {})
    assert db.query(Source).filter_by(site_key="probed-new.cn").first() is None
    # 初评分达标 → 放行
    discovery.evaluate_candidates(db, need.id, {"probed-new.cn": 0.9})
    assert db.query(Source).filter_by(site_key="probed-new.cn").first() is not None


def test_probe_pass_disabled_keeps_strict_gate(db, need, monkeypatch):
    from app.services import discovery
    monkeypatch.setattr(settings, "discovery_auto_trial_threshold", 0.1)
    monkeypatch.setattr(settings, "discovery_probe_pass", 0)      # 关掉这条通路
    discovery.record_evidence(db, "https://probed-off.cn/a", "source_search")
    discovery.evaluate_candidates(db, need.id, {"probed-off.cn": 1.0})
    assert db.query(Source).filter_by(site_key="probed-off.cn").first() is None
