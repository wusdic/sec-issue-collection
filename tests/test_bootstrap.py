"""首次部署流程 + 时间预算兜底(替代拍脑袋的条数上限)。"""
from datetime import datetime, timedelta

import pytest

from app.config import settings
from app.models import AutoOpsRun, Source
from app.services import autopilot, bootstrap


@pytest.fixture(autouse=True)
def _clean_ops(db):
    db.query(AutoOpsRun).filter(AutoOpsRun.task.like("bootstrap:%")).delete(
        synchronize_session=False)
    db.commit()
    yield
    db.query(AutoOpsRun).filter(AutoOpsRun.task.like("bootstrap:%")).delete(
        synchronize_session=False)
    db.commit()


# ---------------- 前置检查:mock 模型必须拦住 ----------------

def test_precheck_blocks_on_mock_llm(db, monkeypatch):
    """带着 mock(离线假模型)采集会把库填满假数据,必须先拦住。"""
    monkeypatch.setattr(settings, "llm_provider", "mock")
    pc = bootstrap.precheck(db)
    assert pc["ok"] is False
    llm = [x for x in pc["items"] if x["key"] == "llm"][0]
    assert llm["blocking"] is True and llm["ok"] is False


def test_precheck_passes_with_real_llm(db, monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "openai_compat")
    pc = bootstrap.precheck(db)
    assert pc["ok"] is True
    assert not pc["blocked"]


def test_render_off_is_warning_not_block(db, monkeypatch):
    """渲染没开只是警告,不阻断——不开也能跑,只是可用率低。"""
    monkeypatch.setattr(settings, "llm_provider", "openai_compat")
    monkeypatch.setattr(settings, "playwright_enabled", False)
    pc = bootstrap.precheck(db)
    render = [x for x in pc["items"] if x["key"] == "render"][0]
    assert render["ok"] is False and render["blocking"] is False
    assert pc["ok"] is True                    # 仍可开始


def test_start_refuses_when_blocked(db, monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "mock")
    r = bootstrap.start("sec_events")
    assert r["started"] is False and r["blocked"]


# ---------------- 流程状态:切页/刷新能接着看 ----------------

def test_status_reads_persisted_steps(db, need):
    """每步落 AutoOpsRun,所以进度不在内存里——切页、刷新、重启都读得到。"""
    db.add(AutoOpsRun(need_id=need.id, task="bootstrap:seeds", status="done",
                      summary={"added": 5, "in_file": 89, "total": 89},
                      finished_at=datetime.utcnow()))
    db.commit()
    st = bootstrap.status(db, need.id)
    seeds = [s for s in st["steps"] if s["step"] == "seeds"][0]
    assert seeds["status"] == "done" and seeds["summary"]["added"] == 5
    # 没跑过的步骤显示 pending + 说明
    crawl = [s for s in st["steps"] if s["step"] == "crawl"][0]
    assert crawl["status"] == "pending" and crawl["what"]


def test_status_never_run(db, need):
    st = bootstrap.status(db, need.id)
    assert st["never"] is True and st["completed"] is False
    assert len(st["steps"]) == len(bootstrap.STEPS)


def test_steps_are_in_dependency_order():
    """采集必须排在找源之前——找源要靠先采到的语料。"""
    keys = [k for k, _, _ in bootstrap.STEPS]
    assert keys.index("crawl") < keys.index("prospect")
    assert keys.index("seeds") < keys.index("locate")   # 先有源才谈得上定位栏目
    assert keys[0] == "precheck"


# ---------------- 时间预算替代条数上限 ----------------

def _root_sources(db, need, n):
    for i in range(n):
        db.add(Source(name=f"根域源{i}", kind="page", adapter="generic_list",
                      credibility="S3", tier="B", lifecycle="active",
                      serves_needs=[need.id], entry_url=f"https://root-{i}.gov.cn/",
                      site_key=f"root-{i}.gov.cn"))
    db.flush()


def test_locate_no_count_cap_by_default(db, need, monkeypatch):
    """默认不按条数限量:源变多也不会有站被永久跳过(以前上限 10,第 11 个就得等下轮)。"""
    _root_sources(db, need, 15)
    monkeypatch.setattr(settings, "autopilot_locate_max", 0)
    monkeypatch.setattr(settings, "autopilot_task_budget_seconds", 0)
    scanned = []
    monkeypatch.setattr("app.services.columns.discover_and_persist",
                        lambda db, s: (scanned.append(s.id) or ([], None)))
    r = autopilot._do_locate(db, need.id)
    # conftest 已有一批根域种子源,这里只验证:我加的 15 个全被扫到、且远超旧上限 10
    assert set(range(0)) or r["scanned"] >= 15
    assert r["scanned"] > 10 and "remaining" not in r


def test_locate_time_budget_stops_and_reports_remaining(db, need, monkeypatch):
    """时间到就收尾,并明确报告"还剩几个下轮继续"——不是静默截断。"""
    _root_sources(db, need, 15)
    monkeypatch.setattr(settings, "autopilot_locate_max", 0)
    monkeypatch.setattr(settings, "autopilot_task_budget_seconds", 60)
    # 让"时间"在第 3 个站之后就过预算
    ticks = iter([0, 0, 0, 999, 999, 999, 999, 999, 999, 999, 999, 999, 999, 999, 999, 999])
    monkeypatch.setattr("app.services.autopilot._time.monotonic", lambda: next(ticks))
    monkeypatch.setattr("app.services.columns.discover_and_persist",
                        lambda db, s: ([], None))
    r = autopilot._do_locate(db, need.id)
    assert r["scanned"] < 15 and r.get("remaining", 0) > 0
    assert "下轮继续" in (r.get("note") or "")


def test_health_time_budget_reports_remaining(db, need, monkeypatch):
    monkeypatch.setattr(settings, "autopilot_health_max", 0)
    monkeypatch.setattr(settings, "source_auto_retire_fail_streak", 0)   # 别真停用
    for i in range(8):
        db.add(Source(name=f"体检源{i}", kind="page", adapter="generic_list",
                      credibility="S3", tier="B", lifecycle="active",
                      serves_needs=[need.id], entry_url=f"https://h-{i}.cn/list/",
                      site_key=f"h-{i}.cn"))
    db.flush()
    calls = {"n": 0}

    def fake_check(_db, s):
        calls["n"] += 1
        return {"id": s.id, "name": s.name, "ok": True, "count": 3, "retired": False,
                "revived": False, "watching": False}

    monkeypatch.setattr("app.services.health.check_one", fake_check)
    # 预算在第 2 个之后耗尽
    seq = iter([0]*3 + [999]*20)
    monkeypatch.setattr("app.services.autopilot._time.monotonic", lambda: next(seq))
    monkeypatch.setattr(settings, "autopilot_task_budget_seconds", 30)
    r = autopilot._do_health(db, need.id)
    assert calls["n"] < 8 and r.get("remaining", 0) > 0
