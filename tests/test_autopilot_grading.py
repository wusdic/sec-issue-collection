"""源库自动运维 + 试运行源自动定级:把"人工按按钮/人工转正"变成系统自己做。

红线约束必须守住:S1 只给域名可客观验证的官方来源;S2(能支撑"已确认"金额)机器只建议
不自动执行;S3 自动给(不能支撑已确认金额,自动化不突破发布红线)。
"""
from datetime import datetime, timedelta

import pytest

from app.config import settings
from app.models import AutoOpsRun, RawDocument, Source, SourceProbe
from app.services import autopilot, grading


def _trial(db, need, name, url, days_ago=20, **kw):
    s = Source(name=name, kind="page", adapter="generic_list", credibility="S4", tier="B",
               lifecycle="trial", serves_needs=[need.id], entry_url=url,
               trial_started_at=datetime.utcnow() - timedelta(days=days_ago),
               discovered_from=kw.pop("discovered_from", "discovery"), **kw)
    db.add(s); db.flush()
    return s


def _docs(db, need, src, n, status="screened_in"):
    for i in range(n):
        db.add(RawDocument(need_id=need.id, source_id=src.id,
                           url=f"{src.entry_url}/d{src.id}-{status}-{i}",
                           url_normalized=f"{src.entry_url}/d{src.id}-{status}-{i}",
                           title=f"t{i}", screen_status=status))
    db.flush()


# ---------------- 自动定级 ----------------

def test_official_domain_auto_promotes_to_s1(db, need):
    """政务域名是客观可验证的事实,不是判断 → 可以自动给 S1。"""
    s = _trial(db, need, "某省网信办通报", "https://wxb.example.gov.cn/tongbao/")
    d = grading.decide(db, s)
    assert d["action"] == "promote" and d["credibility"] == "S1"
    assert grading.is_official(s) is True


def test_named_official_platform_auto_promotes(db, need):
    """名录内的官方技术机构/法定披露平台(非 .gov.cn)同样自动 S1。"""
    s = _trial(db, need, "CNVD 公告", "https://www.cnvd.org.cn/flaw/list")
    assert grading.decide(db, s)["credibility"] == "S1"


def test_media_source_auto_promotes_to_s3_only(db, need, monkeypatch):
    """普通媒体源达标只自动到 S3:S3 支撑不了"已确认"金额,自动化不会突破发布红线。"""
    s = _trial(db, need, "某安全媒体", "https://sec-media-a.cn/news/")
    _docs(db, need, s, 8, "screened_in")
    _docs(db, need, s, 4, "screened_out")
    d = grading.decide(db, s)
    assert d["action"] == "promote" and d["credibility"] == "S3"
    assert d["metrics"]["relevant_ratio"] >= 0.3


def test_noisy_source_auto_retired(db, need):
    s = _trial(db, need, "噪声源", "https://noise-a.cn/list/")
    _docs(db, need, s, 30, "screened_out")
    d = grading.decide(db, s)
    assert d["action"] == "retire" and "噪声源" in d["reason"]


def test_small_sample_extends_trial(db, need):
    """样本太少不下结论——冗余度:低频源不该因为"还没攒够篇数"被判死。"""
    s = _trial(db, need, "低频源", "https://lowfreq-a.cn/list/")
    _docs(db, need, s, 2, "screened_in")
    d = grading.decide(db, s)
    assert d["action"] == "extend" and "样本不足" in d["reason"]


def test_young_trial_not_judged_yet(db, need):
    s = _trial(db, need, "刚建的源", "https://young-a.cn/list/", days_ago=2)
    _docs(db, need, s, 40, "screened_out")
    assert grading.decide(db, s)["action"] == "extend"     # 没满试运行期,先不判


def test_borderline_goes_to_human(db, need):
    """不够格自动转正、也不够差到淘汰 → 交人工,而不是机器硬判。"""
    s = _trial(db, need, "中间地带源", "https://mid-a.cn/list/")
    _docs(db, need, s, 10, "screened_in")      # 相关 10
    _docs(db, need, s, 60, "screened_out")     # 不相关 60 → 比例 0.14,在两条线之间
    d = grading.decide(db, s)
    assert d["action"] == "suggest"


def test_manual_source_never_auto_retired(db, need):
    """人工添加的源只升不降,绝不自动淘汰。"""
    s = _trial(db, need, "人工加的源", "https://manual-a.cn/list/", discovered_from="manual")
    _docs(db, need, s, 30, "screened_out")
    r = grading.auto_grade(db, need.id)
    mine = [x for x in r["results"] if x["id"] == s.id][0]
    assert mine["action"] == "suggest" and s.lifecycle == "trial"


def test_auto_grade_executes_and_records(db, need):
    ok = _trial(db, need, "达标源", "https://good-a.cn/news/")
    _docs(db, need, ok, 10, "screened_in")
    bad = _trial(db, need, "该淘汰源", "https://bad-a.cn/news/")
    _docs(db, need, bad, 25, "screened_out")
    r = grading.auto_grade(db, need.id)
    assert r["promoted"] >= 1 and r["retired"] >= 1
    assert db.get(Source, ok.id).lifecycle == "active"
    assert db.get(Source, ok.id).credibility == "S3"
    assert db.get(Source, bad.id).lifecycle == "retired"
    assert "自动定级" in (db.get(Source, ok.id).note or "")


def test_dry_run_changes_nothing(db, need):
    s = _trial(db, need, "预览源", "https://dry-a.cn/news/")
    _docs(db, need, s, 10, "screened_in")
    grading.auto_grade(db, need.id, dry_run=True)
    assert db.get(Source, s.id).lifecycle == "trial"      # 预览不落库


def test_pending_human_lists_suggestions(db, need):
    s = _trial(db, need, "待确认源", "https://ask-a.cn/news/")
    _docs(db, need, s, 10, "screened_in")
    _docs(db, need, s, 60, "screened_out")
    grading.auto_grade(db, need.id)
    todo = [x for x in grading.pending_human(db, need.id) if x["id"] == s.id]
    assert todo and todo[0]["suggest"] == "S3"


# ---------------- 自动运维调度 ----------------

def test_due_tasks_respect_cadence(db, need, monkeypatch):
    monkeypatch.setattr(settings, "autopilot_grade_days", 1)
    assert "grade" in [t[0] for t in autopilot.due_tasks(db, need.id)]   # 从没跑过 → 到期
    db.add(AutoOpsRun(need_id=need.id, task="grade", status="done",
                      started_at=datetime.utcnow()))
    db.flush()
    assert "grade" not in [t[0] for t in autopilot.due_tasks(db, need.id)]   # 刚跑过 → 不重复
    assert "grade" in [t[0] for t in autopilot.due_tasks(db, need.id, force=True)]


def test_due_again_after_cadence(db, need, monkeypatch):
    monkeypatch.setattr(settings, "autopilot_dedup_days", 7)
    db.add(AutoOpsRun(need_id=need.id, task="dedup", status="done",
                      started_at=datetime.utcnow() - timedelta(days=8)))
    db.flush()
    assert "dedup" in [t[0] for t in autopilot.due_tasks(db, need.id)]


def test_plan_shows_next_run(db, need, monkeypatch):
    monkeypatch.setattr(settings, "autopilot_health_days", 3)
    db.add(AutoOpsRun(need_id=need.id, task="health", status="done",
                      started_at=datetime.utcnow() - timedelta(days=1)))
    db.flush()
    row = [p for p in autopilot.plan(db, need.id) if p["task"] == "health"][0]
    assert row["every_days"] == 3 and row["due"] is False and row["next_run"] != "尽快"


def test_run_due_records_each_step(db, need, monkeypatch):
    """每步都落 AutoOpsRun:自动化但不黑箱,人能核对系统做了什么。"""
    monkeypatch.setattr(autopilot, "TASKS", [("grade", "试运行源自动定级/淘汰",
                                              "autopilot_grade_days", 1)])
    r = autopilot.run_due(need.id, force=True)
    assert r["ran"] == 1 and r["tasks"][0]["status"] in ("done", "skipped")
    rows = db.query(AutoOpsRun).filter_by(need_id=need.id, task="grade").all()
    assert any(x.status in ("done", "skipped") and x.finished_at for x in rows)


def test_step_failure_does_not_stop_others(db, need, monkeypatch):
    monkeypatch.setattr(autopilot, "TASKS",
                        [("dedup", "整理", "autopilot_dedup_days", 7),
                         ("grade", "定级", "autopilot_grade_days", 1)])

    def boom(_db, _need):
        raise RuntimeError("整理炸了")

    monkeypatch.setitem(autopilot._ACTIONS, "dedup", boom)
    r = autopilot.run_due(need.id, force=True)
    st = {t["task"]: t["status"] for t in r["tasks"]}
    assert st["dedup"] == "failed" and st["grade"] in ("done", "skipped")   # 后一步照跑
    row = (db.query(AutoOpsRun).filter_by(need_id=need.id, task="dedup", status="failed")
           .order_by(AutoOpsRun.id.desc()).first())
    assert row and "整理炸了" in (row.note or "")


def test_human_todo_only_the_undecidable(db, need):
    s = _trial(db, need, "只能人判的源", "https://human-a.cn/news/")
    _docs(db, need, s, 10, "screened_in")
    _docs(db, need, s, 60, "screened_out")
    grading.auto_grade(db, need.id)
    todo = autopilot.human_todo(db, need.id)
    assert todo["total"] >= 1
    assert any(x["id"] == s.id for x in todo["suggest_credibility"])


@pytest.fixture(autouse=True)
def _clean(db):
    yield
    db.query(SourceProbe).delete()
    db.flush()


# ---------------- 候选池也必须自动处理(不是等人来点) ----------------

def test_candidates_task_is_daily(db, need, monkeypatch):
    monkeypatch.setattr(settings, "autopilot_candidates_days", 1)
    assert "candidates" in [t[0] for t in autopilot.due_tasks(db, need.id)]
    row = [p for p in autopilot.plan(db, need.id) if p["task"] == "candidates"][0]
    assert row["every_days"] == 1


def test_candidates_task_admits_and_prunes(db, need, monkeypatch):
    """补初评 → 达标自动入库 → 清掉明确无关的,全程不需要人点。"""
    from app.models import SourceDiscoveryEvidence
    from app.services import discovery, prospect
    monkeypatch.setattr(settings, "discovery_auto_trial_threshold", 0.1)
    monkeypatch.setattr(settings, "discovery_probe_pass", 0.7)
    monkeypatch.setattr(settings, "candidate_prune_relevance", 0.2)
    monkeypatch.setattr(settings, "candidate_prune_days", 30)
    monkeypatch.setattr(prospect, "probe_pending", lambda *a, **k: {"probed": 2})

    discovery.record_evidence(db, "https://auto-good.cn/a", "source_search")
    discovery.record_evidence(db, "https://auto-bad.cn/a", "source_search")
    db.add(SourceProbe(identity_key="auto-good.cn", relevance=0.9, ok=True))
    db.add(SourceProbe(identity_key="auto-bad.cn", relevance=0.05, ok=True))
    db.flush()
    # 差的那个很久没再出现 → 该被清理
    for e in db.query(SourceDiscoveryEvidence).filter_by(identity_key="auto-bad.cn").all():
        e.last_seen = datetime.utcnow() - timedelta(days=90)
    db.flush()

    r = autopilot._do_candidates(db, need.id)
    assert r["auto_trial"] >= 1 and r["pruned"] >= 1
    assert db.query(Source).filter_by(site_key="auto-good.cn").first() is not None
    assert db.query(SourceDiscoveryEvidence).filter_by(identity_key="auto-bad.cn").first() is None


def test_prune_keeps_unprobed_and_recent(db, need, monkeypatch):
    """拿不准的一律留着:没初评的、最近还在出现的,都不清。"""
    from app.models import SourceDiscoveryEvidence
    from app.services import discovery
    monkeypatch.setattr(settings, "candidate_prune_relevance", 0.2)
    monkeypatch.setattr(settings, "candidate_prune_days", 30)
    discovery.record_evidence(db, "https://keep-unprobed.cn/a", "citation")   # 没初评
    discovery.record_evidence(db, "https://keep-recent.cn/a", "citation")     # 初评低但最近还在出现
    db.add(SourceProbe(identity_key="keep-recent.cn", relevance=0.05, ok=True))
    db.flush()
    discovery.prune_candidates(db, need.id)
    left = {e.identity_key for e in db.query(SourceDiscoveryEvidence).all()}
    assert "keep-unprobed.cn" in left and "keep-recent.cn" in left


def test_prune_disabled_by_config(db, need, monkeypatch):
    from app.services import discovery
    monkeypatch.setattr(settings, "candidate_prune_relevance", 0)
    assert discovery.prune_candidates(db, need.id)["pruned"] == 0


# ---------------- 把"我让你手动做的两步"也自动化 ----------------

def test_engines_and_seeds_are_autopilot_tasks(db, need):
    """自检引擎、载入内置源——这两件事不该要人手动点。"""
    tasks = [t[0] for t in autopilot.TASKS]
    assert "engines" in tasks and "seeds" in tasks
    # 顺序:先补源、再挑引擎,后面的找源才不会白跑
    assert tasks.index("seeds") < tasks.index("engines") < tasks.index("prospect")


def test_autotune_keeps_only_usable_engines(db, monkeypatch):
    """被反爬的引擎自动踢出,不用人去设置页改;可用的写回配置并留痕。"""
    from app.services import prospect
    monkeypatch.setattr(settings, "prospect_engines", "bing_search,baidu_search")
    monkeypatch.setattr(settings, "prospect_engines_all", "bing_search,baidu_search")
    monkeypatch.setattr(prospect, "selftest", lambda _db, q="x": {
        "query": q, "usable": ["bing_search"], "advice": "",
        "engines": [{"engine": "bing_search", "ok": True, "blocked": False, "hint": ""},
                    {"engine": "baidu_search", "ok": False, "blocked": True, "hint": "验证页"}]})
    saved = {}
    monkeypatch.setattr("app.services.settings_service.save",
                        lambda _db, upd: saved.update(upd) or list(upd))
    r = prospect.autotune_engines(db)
    assert r["changed"] is True and r["usable"] == ["bing_search"]
    assert saved["prospect_engines"] == "bing_search"
    assert "停用 baidu_search" in r["note"]
    from app.services import actions
    assert any(x["action"] == "source.engines_tuned"
               for x in actions.feed(db, module="sources", min_level=1))


def test_autotune_readds_recovered_engine(db, monkeypatch):
    """之前被踢掉的引擎恢复了要能自动加回来——反爬是临时的,不能一棒子打死。"""
    from app.services import prospect
    monkeypatch.setattr(settings, "prospect_engines", "bing_search")
    monkeypatch.setattr(settings, "prospect_engines_all", "bing_search,sogou_wechat")
    monkeypatch.setattr(prospect, "selftest", lambda _db, q="x": {
        "query": q, "usable": ["bing_search", "sogou_wechat"], "advice": "",
        "engines": [{"engine": "bing_search", "ok": True, "blocked": False, "hint": ""},
                    {"engine": "sogou_wechat", "ok": True, "blocked": False, "hint": ""}]})
    saved = {}
    monkeypatch.setattr("app.services.settings_service.save",
                        lambda _db, upd: saved.update(upd) or list(upd))
    r = prospect.autotune_engines(db)
    assert "加回 sogou_wechat" in r["note"] and "sogou_wechat" in saved["prospect_engines"]


def test_autotune_never_empties_engine_list(db, monkeypatch):
    """一个都不可用时保持原配置——宁可空跑也不要把找源彻底关掉。"""
    from app.services import prospect
    monkeypatch.setattr(settings, "prospect_engines", "bing_search,baidu_search")
    monkeypatch.setattr(settings, "prospect_engines_all", "bing_search,baidu_search")
    monkeypatch.setattr(prospect, "selftest", lambda _db, q="x": {
        "query": q, "usable": [], "advice": "",
        "engines": [{"engine": "bing_search", "ok": False, "blocked": True, "hint": "验证页"}]})
    monkeypatch.setattr("app.services.settings_service.save",
                        lambda _db, upd: pytest.fail("全不可用时不该改配置"))
    r = prospect.autotune_engines(db)
    assert r["changed"] is False and "保持原配置" in r["note"]
    assert settings.prospect_engines == "bing_search,baidu_search"   # 未被改动


def test_seeds_task_is_idempotent(db, need):
    """载入内置源是幂等的:第二次不该再新增。"""
    r1 = autopilot._do_seeds(db, need.id)
    r2 = autopilot._do_seeds(db, need.id)
    # 幂等:不管第一次补了几个,第二次一定不再新增
    assert r2["added"] == 0, r2
    # 清单本身要够宽(内置源不能只有几十个),且库里已覆盖清单里的全部源
    assert r1["in_file"] >= 80, r1
    assert r1["total"] >= r1["in_file"], r1
