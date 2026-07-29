"""源发现卫生:正文套话不得变成源;0 产出计入源健康。

实测一轮采集:75 源里 48 个是 [候选] 垃圾源(占 64% 采集名额,全部 0 产出),
名字形如 "[候选]请注明出处""[候选]于原作者或互联网共享平台""[候选]https://…",
且因全角冒号漏配产生 17 对 "：新华社/新华社" 重复源。
"""
import pytest

from app.config import settings
from app.models import Source
from app.services import discovery, pipeline
from app.services.pipeline import CITATION_RE, _is_subject_like


def _capture(text):
    m = CITATION_RE.search(text)
    return (m.group(1) or "").strip(" \t:：、,，。;；「」『』\"'()（）") if m else None


def test_fullwidth_colon_not_captured():
    """全角冒号必须是分隔符,否则「：新华社」与「新华社」成为两个不同的源。"""
    assert _capture("文章来源：新华社") == "新华社"
    assert _capture("来源:新华社") == "新华社"


def test_boilerplate_not_treated_as_subject():
    for junk in ["请注明出处", "于原作者或互联网共享平台", "如若转载", "本文编辑",
                 "https://cybersecuritynews.com/", "点击可疑链接"]:
        assert not _is_subject_like(junk), junk


def test_real_subject_names_accepted():
    for ok in ["新华社", "安全内参", "FreeBuf", "装备工业一司", "CNCERT"]:
        assert _is_subject_like(ok), ok


def test_boilerplate_rejected_at_evidence_layer(db, need):
    assert discovery.record_evidence(db, None, "wechat_reference",
                                     display_name="请注明出处", wechat_account="请注明出处") is None
    assert discovery.record_evidence(db, None, "wechat_reference",
                                     display_name="安全内参", wechat_account="安全内参") == "mp:安全内参"


def test_single_channel_candidate_not_auto_registered(db, need, monkeypatch):
    """孤证(单通道)即便分数够也不自动入库,需≥2 通道或曾为同稿首发。"""
    from app.models import SourceDiscoveryEvidence
    monkeypatch.setattr(settings, "discovery_auto_trial_threshold", 1.0)  # 故意调到极低
    db.add(SourceDiscoveryEvidence(identity_key="lonely.example.com", display_name="孤证站",
                                   kind_guess="website", channel="citation",
                                   evidence_url="https://lonely.example.com/a"))
    db.flush()
    res = discovery.evaluate_candidates(db, need.id)
    hit = [c for c in res if c["identity_key"] == "lonely.example.com"]
    assert hit and hit[0]["auto_trial"] is False       # 分数够但通道不够 → 不入库


def test_multi_channel_candidate_can_register(db, need, monkeypatch):
    from app.models import SourceDiscoveryEvidence
    monkeypatch.setattr(settings, "discovery_auto_trial_threshold", 1.0)
    for ch in ("citation", "event_search"):
        db.add(SourceDiscoveryEvidence(identity_key="multi.example.com", display_name="多通道站",
                                       kind_guess="website", channel=ch,
                                       evidence_url="https://multi.example.com/a"))
    db.flush()
    res = discovery.evaluate_candidates(db, need.id)
    hit = [c for c in res if c["identity_key"] == "multi.example.com"]
    assert hit and hit[0]["auto_trial"] is True


def test_zero_yield_counts_as_unhealthy(db, need, monkeypatch):
    """解析出 0 条不再算成功:此前无条件 ok 并清零 fail_streak,坏源永不停用。"""
    monkeypatch.setattr(settings, "source_auto_retire_fail_streak", 2)
    monkeypatch.setattr(settings, "source_quiet_tolerance_days", 30)
    from datetime import datetime as _dt, timedelta as _td
    src = Source(name="空产出源", kind="page", adapter="generic_list", credibility="S3", tier="B",
                 lifecycle="active", serves_needs=[need.id], entry_url="https://empty.example.com/c",
                 fail_streak=1, last_success_at=None,
                 created_at=_dt.utcnow() - _td(days=90))   # 建源已久且从未成功 → 超出容忍期
    db.add(src); db.flush()

    class _Empty:
        kind = "page"
        def discover_page(self, page):
            return []                      # 抓到了页面但一条都没解析出来

    monkeypatch.setattr(pipeline, "get_adapter", lambda s: _Empty())
    run = pipeline.crawl_source(db, need, src, max_pages=1, do_archive=False)
    assert run.urls_found == 0
    assert src.fail_streak >= 2
    assert src.lifecycle == "retired"       # 连续无产出 → 自动停用
    assert src.last_success_at is None      # 不再被刷新成"刚成功过"
