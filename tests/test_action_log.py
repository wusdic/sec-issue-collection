"""动作分类分级 + 高级别优先提示。

系统自动做的事越多,越需要"它到底动了什么"一眼可见。这里验证:
1) 每个自动动作都进台账,并按 模块/级别 归类;
2) 级别按 是否碰红线 × 影响面 定,影响面大会自动升级;
3) 只有高级别未确认的才优先提示,一般动作不打扰;
4) 确认后不再顶在页面上,但日志完整保留。
"""
from datetime import datetime, timedelta

import pytest

from app.config import settings
from app.models import ActionLog, Source
from app.services import actions


@pytest.fixture(autouse=True)
def _clean(db):
    db.query(ActionLog).delete()
    db.flush()
    yield
    db.query(ActionLog).delete()
    db.flush()


# ---------------- 分级规则 ----------------

def test_redline_action_is_critical():
    """自动定级 S1 碰发布红线(S1 可支撑"已确认"金额并允许发布)→ 紧急。"""
    assert actions.grade("source.auto_promote_s1") == actions.CRITICAL
    assert actions.grade("event.money_confirmed") == actions.CRITICAL
    assert actions.grade("crawl.job_failed") == actions.CRITICAL


def test_routine_action_is_low():
    assert actions.grade("source.auto_columns") == actions.NOTICE
    assert actions.grade("crawl.job_done") == actions.INFO
    assert actions.grade("event.auto_draft") == actions.INFO


def test_impact_escalates_level():
    """一次停用 1 个源是"重要",一次停用 8 个就是"紧急"——那多半是系统出问题了。"""
    assert actions.grade("source.auto_retire", 1) == actions.HIGH
    assert actions.grade("source.auto_retire", 5) == actions.CRITICAL
    assert actions.grade("source.auto_trial", 3) == actions.HIGH      # 未达升级线
    assert actions.grade("source.auto_trial", 8) == actions.CRITICAL


def test_escalation_caps_at_critical():
    assert actions.grade("source.auto_promote_s1", 999) == actions.CRITICAL


def test_unknown_action_falls_back_quietly():
    assert actions.grade("something.new") == actions.INFO


# ---------------- 记账与查询 ----------------

def test_record_classifies_by_module(db, need):
    actions.record(db, "source.auto_retire", "「某源」自动停用", need_id=need.id)
    actions.record(db, "crawl.job_failed", "采集整批失败", need_id=need.id)
    rows = actions.feed(db, need.id)
    mods = {r["module"] for r in rows}
    assert mods == {"sources", "crawl"}
    assert all(r["level_name"] in actions.LEVEL_NAME.values() for r in rows)


def test_feed_filters_by_module_and_level(db, need):
    actions.record(db, "source.auto_columns", "定位栏目", need_id=need.id)      # 关注
    actions.record(db, "source.auto_promote_s1", "自动 S1", need_id=need.id)    # 紧急
    assert len(actions.feed(db, need.id, module="sources")) == 2
    high = actions.feed(db, need.id, min_level=actions.HIGH)
    assert len(high) == 1 and high[0]["action"] == "source.auto_promote_s1"
    assert actions.feed(db, need.id, module="crawl") == []


def test_feed_sorts_severe_first(db, need):
    actions.record(db, "source.auto_columns", "低级别", need_id=need.id)
    actions.record(db, "source.auto_promote_s1", "高级别", need_id=need.id)
    rows = actions.feed(db, need.id, min_level=1)
    assert rows[0]["level"] >= rows[-1]["level"]


def test_alerts_only_high_and_unacked(db, need):
    actions.record(db, "source.auto_columns", "定位栏目(不该提示)", need_id=need.id)
    r = actions.record(db, "source.auto_retire", "自动停用(该提示)", need_id=need.id)
    a = actions.alerts(db, need.id, "sources")
    assert a["count"] == 1 and a["items"][0]["id"] == r.id
    actions.ack(db, [r.id], user_id=None)
    assert actions.alerts(db, need.id, "sources")["count"] == 0     # 确认后不再顶在页面上
    assert len(actions.feed(db, need.id, min_level=1)) == 2         # 但日志完整保留


def test_summary_groups_by_module(db, need):
    actions.record(db, "source.auto_retire", "停用", need_id=need.id)
    actions.record(db, "source.auto_trial", "新源入库", need_id=need.id)
    actions.record(db, "crawl.job_failed", "整批失败", need_id=need.id)
    s = actions.summary(db, need.id)
    assert s["total"] == 3 and s["critical"] >= 1
    by = {m["module"]: m["count"] for m in s["by_module"]}
    assert by == {"sources": 2, "crawl": 1}
    assert s["by_module"][0]["module"] == "sources"      # 最多的排前面


def test_ack_all_by_module(db, need):
    actions.record(db, "source.auto_retire", "A", need_id=need.id)
    actions.record(db, "crawl.job_failed", "B", need_id=need.id)
    assert actions.ack_all(db, need.id, "sources") == 1
    assert actions.summary(db, need.id)["total"] == 1   # 只剩采集那条


def test_record_carries_revert_hint(db, need):
    r = actions.record(db, "source.auto_retire", "自动停用", need_id=need.id)
    assert "恢复启用" in (r.reversible or "")            # 每条高级别动作都告诉人怎么撤销


def test_old_actions_drop_out_of_window(db, need):
    r = actions.record(db, "source.auto_retire", "很久以前", need_id=need.id)
    r.at = datetime.utcnow() - timedelta(days=60)
    db.flush()
    assert actions.feed(db, need.id, days=30) == []
    assert len(actions.feed(db, need.id, days=90)) == 1


# ---------------- 真实动作确实落台账 ----------------

def test_auto_retire_writes_action_log(db, need, monkeypatch):
    from app.services import health
    monkeypatch.setattr(settings, "source_auto_retire_fail_streak", 1)
    monkeypatch.setattr(settings, "source_quiet_tolerance_days", 30)
    s = Source(name="要被停用的源", kind="page", adapter="generic_list", credibility="S3",
               tier="B", lifecycle="active", serves_needs=[need.id],
               entry_url="https://actlog-a.cn/col/",
               last_success_at=datetime.utcnow() - timedelta(days=99))
    db.add(s); db.flush()
    health.register_failure(db, s, "试抓无结果")
    rows = actions.feed(db, need.id, module="sources", min_level=actions.HIGH)
    assert any(r["action"] == "source.auto_retire" and "要被停用的源" in r["title"] for r in rows)


def test_auto_grade_writes_action_log(db, need):
    from app.models import RawDocument
    from app.services import grading
    s = Source(name="政务通报源", kind="page", adapter="generic_list", credibility="S4",
               tier="B", lifecycle="trial", serves_needs=[need.id],
               entry_url="https://actlog.example.gov.cn/tongbao/",
               trial_started_at=datetime.utcnow() - timedelta(days=30))
    db.add(s); db.flush()
    db.add(RawDocument(need_id=need.id, source_id=s.id, url="https://actlog.example.gov.cn/x",
                       url_normalized="https://actlog.example.gov.cn/x", title="t",
                       screen_status="screened_in"))
    db.flush()
    grading.auto_grade(db, need.id)
    rows = actions.feed(db, need.id, module="sources", min_level=actions.CRITICAL)
    assert any(r["action"] == "source.auto_promote_s1" for r in rows)   # 碰红线 → 紧急提示


def test_auto_trial_registration_writes_action_log(db, need, monkeypatch):
    from app.services import discovery
    monkeypatch.setattr(settings, "discovery_auto_trial_threshold", 0.1)
    discovery.record_evidence(db, "https://actlog-new.cn/a", "citation")
    discovery.record_evidence(db, "https://actlog-new.cn/b", "event_search")
    discovery.evaluate_candidates(db, need.id)
    rows = actions.feed(db, need.id, module="sources", min_level=actions.HIGH)
    assert any(r["action"] == "source.auto_trial" for r in rows)
