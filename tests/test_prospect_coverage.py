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
    # sources_silent 只取前 50 条展示,种子源多时未必落在里面;这里直接验计数口径
    assert out["sources_producing"] < out["sources_active"]
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
                               wechat_account="测试网信号")]


def test_sogou_account_used_without_resolving(db, need, monkeypatch):
    """790 条搜狗结果此前全被当跳转链、受配额限制白丢——结果页本来就带号名。"""
    monkeypatch.setattr(prospect, "_engines", lambda: [("sogou_wechat", _SogouLike())])
    monkeypatch.setattr(prospect, "build_queries", lambda *a, **k: ["网警 处罚"])
    monkeypatch.setattr(prospect, "_resolve_redirect",
                        lambda u: pytest.fail("自带号名时不该再去还原跳转链"))
    monkeypatch.setattr(prospect, "_wechat_account",
                        lambda u: pytest.fail("自带号名时不该再抓文章解析"))
    r = prospect.run_once(db, need.id)
    assert set(r["new_keys"]) == {"mp:平安北京", "mp:测试网信号"}
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


# ---------------- ⑩ 候选池必须看得见、能操作 ----------------

def test_candidates_api_shows_name_and_blocked_reason(db, need, monkeypatch):
    """此前候选池没有任何入口,发现到的渠道等于进黑洞;而且"没入库"从不说明卡在哪。"""
    from app.api.routes import source_candidates
    from app.services import discovery
    monkeypatch.setattr(settings, "discovery_auto_trial_threshold", 99)   # 谁都达不到
    discovery.record_evidence(db, None, "source_search", display_name="平安北京",
                              wechat_account="平安北京")
    db.flush()
    rows = [r for r in source_candidates(min_score=0, db=db, _=None)
            if r["identity_key"] == "mp:平安北京"]
    assert rows, "候选没出现在候选池里"
    c = rows[0]
    assert c["name"] == "平安北京" and c["kind"] == "公众号"
    assert c["channels"] == ["source_search"] and c["hits"] >= 1
    assert "未达" in c["blocked_reason"]              # 说得出卡在哪


def test_candidate_already_a_source_is_hidden(db, need, monkeypatch):
    from app.api.routes import source_candidates
    from app.services import discovery
    discovery.record_evidence(db, "https://cand-hidden.cn/a", "source_search")
    db.add(Source(name="已建源", kind="page", adapter="generic_list", credibility="S3", tier="B",
                  lifecycle="active", serves_needs=[need.id],
                  entry_url="https://cand-hidden.cn/c/", site_key="cand-hidden.cn"))
    db.flush()
    keys = {r["identity_key"] for r in source_candidates(min_score=0, db=db, _=None)}
    assert "cand-hidden.cn" not in keys


def test_admit_candidate_creates_trial_source(db, need):
    """不等自动入库,人工一键收下。"""
    from app.api.routes import admit_candidate
    from app.models import AppUser
    from app.services import discovery
    user = db.query(AppUser).filter_by(role="admin").first()
    discovery.record_evidence(db, None, "source_search", display_name="测试收下号",
                              wechat_account="测试收下号")
    db.flush()
    out = admit_candidate("mp:测试收下号", need_id=need.id, db=db, user=user)
    src = db.get(Source, out["id"])
    assert src.lifecycle == "trial" and src.credibility == "S4"
    assert src.kind == "query" and src.adapter == "sogou_wechat"
    assert (src.adapter_config or {}).get("account") == "测试收下号"


def test_admit_baijiahao_candidate_gets_usable_entry(db, need):
    from app.api.routes import admit_candidate
    from app.models import AppUser
    from app.services import discovery
    user = db.query(AppUser).filter_by(role="admin").first()
    discovery.record_evidence(db, None, "source_search", display_name="某安全作者",
                              platform_key="bjh:2024")
    db.flush()
    out = admit_candidate("bjh:2024", need_id=need.id, db=db, user=user)
    assert out["entry_url"] == "https://author.baidu.com/home?app_id=2024"


# ---------------- ⑪ 验证页不能变成假结果 / 号名要可用 / 候选名要是渠道名 ----------------

def test_blocked_page_returns_none_not_junk():
    """搜狗被拦一次就吐出 139 条全是 sogou.com 的假结果——验证页必须当成抓取失败。"""
    from app.services import adapters
    a = adapters.SogouWechatAdapter(prospect._Shim())
    blocked = ("<html><body>搜狗搜索 返回首页 IP：1.2.3.4 VerifyCode：7f12 "
               "此验证码用于确认这些请求是您的正常行为而不是自动程序发出的"
               '<a href="https://www.sogou.com/">搜狗首页导航</a></body></html>')
    assert a.looks_blocked(blocked) is True
    import app.services.adapters as _ad
    orig = _ad.fetcher.fetch
    try:
        _ad.fetcher.fetch = lambda *x, **k: _ad.fetcher.FetchResult("u", "u", 200, blocked)
        assert a.search_page("网警 处罚", 0) is None      # 不是 []、更不是一堆导航链接
    finally:
        _ad.fetcher.fetch = orig


def test_raw_biz_id_not_used_as_account(db, need, monkeypatch):
    """搜狗有时给 gh_81a05c27673f 这种原始 ID:不可读也没法按号名采,必须解析成真名。"""
    class _E:
        def __init__(self, *_a, **_k): pass
        def search_page(self, q, page, tf=None):
            if page:
                return None
            return [DiscoveredItem(url="https://weixin.sogou.com/link?url=AAA", title="网警通报",
                                   wechat_account="gh_81a05c27673f")]
    monkeypatch.setattr(prospect, "_engines", lambda: [("sogou_wechat", _E())])
    monkeypatch.setattr(prospect, "build_queries", lambda *a, **k: ["网警 处罚"])
    monkeypatch.setattr(prospect, "_resolve_redirect",
                        lambda u: "https://mp.weixin.qq.com/s/XYZ")
    monkeypatch.setattr(prospect, "_wechat_account", lambda u: "平安北京")
    r = prospect.run_once(db, need.id)
    assert "mp:平安北京" in r["new_keys"]
    assert not any(k.startswith("mp:gh_") for k in r["new_keys"])


def test_unresolvable_biz_id_not_admitted(db, need, monkeypatch):
    class _E:
        def __init__(self, *_a, **_k): pass
        def search_page(self, q, page, tf=None):
            if page:
                return None
            return [DiscoveredItem(url="https://mp.weixin.qq.com/s/XYZ", title="x",
                                   wechat_account="gh_aaaaaaaaaa")]
    monkeypatch.setattr(prospect, "_engines", lambda: [("sogou_wechat", _E())])
    monkeypatch.setattr(prospect, "build_queries", lambda *a, **k: ["网警 处罚"])
    monkeypatch.setattr(prospect, "_wechat_account", lambda u: "gh_aaaaaaaaaa")
    r = prospect.run_once(db, need.id)
    assert not r["new_keys"] and r["stats"]["wechat_unresolved"] == 1


def test_candidate_name_prefers_channel_not_article_title(db, need):
    """候选池里不该出现"什么是网警?-安康市公安局"这种文章标题当渠道名。"""
    from app.services import discovery
    discovery.record_evidence(db, "https://ankang-ga.cn/a", "source_search",
                              display_name="什么是 网警 ?-安康市公安局")
    db.flush()
    assert discovery.candidate_name(db, "ankang-ga.cn") == "安康市公安局"
    # 初评拿到站点标题后,优先用它
    db.add(SourceProbe(identity_key="ankang-ga.cn", relevance=0.8, ok=True,
                       site_title="安康市公安局网络安全保卫支队"))
    db.flush()
    assert discovery.candidate_name(db, "ankang-ga.cn") == "安康市公安局网络安全保卫支队"


def test_wechat_candidate_can_be_probed(db, monkeypatch):
    """公众号没有首页可抓 → 此前永远初评不了、永远进不了库。改用搜狗按号取文章标题。"""
    monkeypatch.setattr(prospect, "_wechat_titles",
                        lambda acct, n: ["网警通报某公司数据泄露", "依法查处一批违法违规App"])

    class _LLM:
        def complete_json(self, system, user, retries=2):
            return {"relevance": 0.88, "reason": "持续发执法通报"}

    monkeypatch.setattr("app.services.llm.get_screen_llm", lambda: _LLM())
    row = prospect.probe_one(db, "mp:平安北京", force=True)
    assert row.ok and row.relevance == 0.88


# ---------------- ⑫ 先验证路径再铺词 / 废词不要组 / 种子源要够 ----------------

def test_selftest_reports_per_engine(db, monkeypatch):
    """先自检再铺关键词:每个引擎只跑 1 条词,直接告诉你这条路通不通。"""
    class _Good:
        config = {"render": False}
        def __init__(self, *_a, **_k): pass
        def build_url(self, q, page, tf): return "https://good.engine/s"
        def looks_blocked(self, html): return False
        def parse(self, html): return [DiscoveredItem(url="https://real-sec.cn/a", title="某通报")]

    class _Blocked(_Good):
        def build_url(self, q, page, tf): return "https://blocked.engine/s"
        def looks_blocked(self, html): return True

    monkeypatch.setattr(prospect, "_engines",
                        lambda: [("good", _Good()), ("blocked", _Blocked())])
    monkeypatch.setattr(prospect.fetcher, "fetch",
                        lambda *a, **k: prospect.fetcher.FetchResult("u", "u", 200, "<html>x</html>"))
    r = prospect.selftest(db)
    by = {e["engine"]: e for e in r["engines"]}
    assert by["good"]["ok"] is True and by["good"]["samples"][0]["key"] == "real-sec.cn"
    assert by["blocked"]["blocked"] is True and by["blocked"]["ok"] is False
    assert r["usable"] == ["good"] and "good" in r["advice"]


def test_selftest_all_dead_gives_actionable_advice(db, monkeypatch):
    monkeypatch.setattr(prospect, "_engines", lambda: [])
    r = prospect.selftest(db)
    assert not r["usable"] and "铺再多关键词也没用" in r["advice"]


def test_placeholder_industry_never_becomes_query(db, need, monkeypatch):
    """"其他"是词表的兜底桶不是行业,「其他 网络安全」是废词,不能拿去搜。"""
    monkeypatch.setattr(settings, "coverage_min_events", 3)
    qs = coverage.prospect_queries(db, need.id)
    assert not any(q.startswith(("其他", "其它", "未分类")) for q in qs), qs


def test_seed_sources_are_broad_and_unique():
    """源本身要尽量全:种子清单够多、无重名、公众号/站内检索源都有各自唯一的目标键。"""
    import yaml
    from app.services import url_tools as ut
    data = yaml.safe_load(open("config/seed_sources.yaml", encoding="utf-8"))
    ss = data["sources"]
    assert len(ss) >= 80, f"种子源只有 {len(ss)} 个,太少"
    assert len({s["name"] for s in ss}) == len(ss), "种子源有重名"
    idents = [ut.source_keys(s["kind"], s.get("entry_url"), s.get("adapter_config", {}))[1]
              for s in ss]
    real = [i for i in idents if i]
    assert len(set(real)) == len(real), "种子源的采集目标键有冲突,会互相顶掉"
    kinds = {s["adapter"] for s in ss}
    assert "sogou_wechat" in kinds, "缺公众号源"
    assert sum(1 for s in ss if s["adapter"] == "sogou_wechat") >= 15


def test_subdomain_sites_are_distinct_sources():
    """各省通信管理局是 miit.gov.cn 的子域,但各自是独立发布主体,不能共用一个目标键。"""
    from app.services.url_tools import source_keys
    a = source_keys("page", "https://gdca.miit.gov.cn/")
    b = source_keys("page", "https://zjca.miit.gov.cn/")
    c = source_keys("page", "https://www.miit.gov.cn/")
    assert a[1] != b[1] != c[1] and a[1] != c[1]
    # www 与非 www 仍然合并
    assert source_keys("page", "https://www.cac.gov.cn/")[1] == \
           source_keys("page", "https://cac.gov.cn/")[1]


# ---------------- ⑦ 找源损耗:号名解析、跳转链去重、引擎熔断、跨语言噪声 ----------------

def test_sogou_account_read_from_new_markup():
    """搜狗改版后号名只在 id="..._account_0" 上;只认 a.account 会让整批结果拿不到号名,
    只能逐条还原跳转链——实测 732 条搜狗结果因此全走还原、配额一超就白丢。"""
    from app.services.adapters import SogouWechatAdapter
    html = """<ul class="news-list">
      <li><div class="txt-box"><h3><a href="/link?url=AAA">网警通报某案</a></h3>
        <div class="s-p"><a id="sogou_vr_11002401_account_0" href="/profile?src=x">江苏网警</a>
        <span class="s2">2026-01-01</span></div></div></li>
    </ul>"""
    items = SogouWechatAdapter(prospect._Shim()).parse(html)
    assert len(items) == 1 and items[0].wechat_account == "江苏网警"


def test_sogou_account_ignores_control_text():
    """s-p 里也可能先出现"更多"这类控件文案,不能当成号名。"""
    from app.services.adapters import SogouWechatAdapter
    html = """<ul class="news-list">
      <li><h3><a href="/link?url=B">某通报</a></h3>
        <div class="s-p"><a href="#">更多</a><a id="x_account_1" href="/profile">浙江网警</a></div></li>
    </ul>"""
    items = SogouWechatAdapter(prospect._Shim()).parse(html)
    assert items[0].wechat_account == "浙江网警"


def test_same_redirect_resolved_once(db, need, monkeypatch):
    """同一条跳转链在多个词下重复出现,只该发一次请求——否则配额被重复链吃光。"""
    monkeypatch.setattr(settings, "prospect_resolve_max", 10)

    class _E:
        def __init__(self, *_a, **_k): pass
        def search_page(self, q, page, tf=None):
            return None if page else [
                DiscoveredItem(url="https://www.baidu.com/link?url=SAME", title="同一条结果")]

    calls = {"n": 0}

    def fake_resolve(url):
        calls["n"] += 1
        return "https://dedup-site.cn/a"

    monkeypatch.setattr(prospect, "_engines", lambda: [("baidu_search", _E())])
    monkeypatch.setattr(prospect, "build_queries", lambda *a, **k: ["网警 处罚", "网信办 通报", "公安 处罚"])
    monkeypatch.setattr(prospect, "_resolve_redirect", fake_resolve)
    r = prospect.run_once(db, need.id)
    assert calls["n"] == 1, "重复的跳转链应命中缓存"
    assert r["stats"]["redirect"] == 3 and r["stats"]["redirect_cached"] == 2
    assert r["stats"]["redirect_over_budget"] == 0


def test_engine_stops_after_failure_streak(db, need, monkeypatch):
    """被反爬打死的引擎不该把剩下的词全撞一遍(实测百度连错 148 次仍每词都试)。"""
    monkeypatch.setattr(settings, "prospect_engine_fail_streak", 3)

    class _Dead:
        def __init__(self, *_a, **_k): pass
        def search_page(self, q, page, tf=None):
            _Dead.tries = getattr(_Dead, "tries", 0) + 1
            return None

    monkeypatch.setattr(prospect, "_engines", lambda: [("baidu_search", _Dead())])
    monkeypatch.setattr(prospect, "build_queries", lambda *a, **k: [f"词{i}" for i in range(20)])
    r = prospect.run_once(db, need.id)
    assert _Dead.tries == 3, f"连错 3 次后应停用,实际试了 {_Dead.tries} 次"
    detail = {e["engine"]: e for e in r["engine_detail"]}
    assert "停用" in detail["baidu_search"].get("stopped", "")


def test_engine_streak_resets_on_success(db, need, monkeypatch):
    """偶发失败不该把引擎熔断掉:成功一次就清零。"""
    monkeypatch.setattr(settings, "prospect_engine_fail_streak", 2)

    class _Flaky:
        n = 0
        def __init__(self, *_a, **_k): pass
        def search_page(self, q, page, tf=None):
            if page:
                return None
            _Flaky.n += 1
            if _Flaky.n % 2:
                return None                      # 一次失败
            return [DiscoveredItem(url=f"https://flaky-{_Flaky.n}.cn/a", title="安全通报")]

    monkeypatch.setattr(prospect, "_engines", lambda: [("bing_search", _Flaky())])
    monkeypatch.setattr(prospect, "build_queries", lambda *a, **k: [f"词{i}" for i in range(8)])
    r = prospect.run_once(db, need.id)
    detail = {e["engine"]: e for e in r["engine_detail"]}
    assert "stopped" not in detail["bing_search"]
    assert r["stats"]["raw_items"] == 4


def test_english_only_results_dropped(db, need, monkeypatch):
    """中文找源词却搜出 Thesaurus.com 这类纯英文站,是引擎的跨语言噪声,不该占候选池名额。"""
    class _E:
        def __init__(self, *_a, **_k): pass
        def search_page(self, q, page, tf=None):
            return None if page else [
                DiscoveredItem(url="https://www.thesaurus.com/browse/police", title="Thesaurus.com"),
                DiscoveredItem(url="https://ankang-ga.gov.cn/wj/1.html", title="安康市公安局 网警通报")]

    monkeypatch.setattr(prospect, "_engines", lambda: [("bing_search", _E())])
    monkeypatch.setattr(prospect, "build_queries", lambda *a, **k: ["网警 处罚"])
    r = prospect.run_once(db, need.id)
    assert r["stats"]["non_chinese"] == 1
    assert all("thesaurus" not in k for k in r["new_keys"])
    assert any("ankang" in k for k in r["new_keys"])


def test_cn_domain_kept_even_with_english_title(db, need, monkeypatch):
    """.cn 系域名即使标题被引擎截成英文也要留下——语言判定只挡明显噪声,不能误伤政务站。"""
    class _E:
        def __init__(self, *_a, **_k): pass
        def search_page(self, q, page, tf=None):
            return None if page else [
                DiscoveredItem(url="https://cyberpolice.hn.gov.cn/list.html", title="Report Center")]

    monkeypatch.setattr(prospect, "_engines", lambda: [("bing_search", _E())])
    monkeypatch.setattr(prospect, "build_queries", lambda *a, **k: ["网警 处罚"])
    r = prospect.run_once(db, need.id)
    assert r["stats"]["non_chinese"] == 0 and r["new_keys"]
