"""时效窗口 + URL日期 + 根域键合并 + 栏目自动发现 + 候选名校验 + 删除不 500。"""
from datetime import date

import pytest

from app.config import settings
from app.services import columns, url_tools


# ---------------- URL 日期 / 根域键 ----------------

def test_date_from_url_variants():
    assert url_tools.date_from_url("https://www.cac.gov.cn/2026-07/17/c_178.htm") == date(2026, 7, 17)
    assert url_tools.date_from_url("http://x.cn/2019/03/05/a.html") == date(2019, 3, 5)
    assert url_tools.date_from_url("https://x.cn/art_20200722xyz.html") == date(2020, 7, 22)
    assert url_tools.date_from_url("https://x.cn/news/abc.html") is None


def test_root_only_keys_collapse_www():
    # www 与非 www 的根目录 → 同一采集目标键(=站点键),不再被当成两个"栏目"
    sk1, ik1 = url_tools.source_keys("page", "https://www.cac.gov.cn/")
    sk2, ik2 = url_tools.source_keys("page", "https://cac.gov.cn/")
    assert sk1 == sk2 == "cac.gov.cn"
    assert ik1 == ik2 == "cac.gov.cn"
    # 有栏目路径 → 目标键是归一化 URL(与站点键不同)
    sk3, ik3 = url_tools.source_keys("page", "https://www.cac.gov.cn/zhifa/index.htm")
    assert sk3 == "cac.gov.cn" and ik3 != "cac.gov.cn"


# ---------------- 时效窗口 ----------------

def test_recency_skips_old_content(db, need, monkeypatch):
    from app.models import RawDocument, Source
    from app.services import pipeline
    from app.services.adapters import DiscoveredItem
    monkeypatch.setattr(settings, "collect_recency_days", 1825)  # 近5年
    src = db.query(Source).first()
    # URL 里带 2015 年 → 早于5年窗口
    old = DiscoveredItem(url="https://old.example.com/2015-01/02/a.htm", title="很旧的通报")
    monkeypatch.setattr(pipeline.fetcher, "fetch",
                        lambda *a, **k: pipeline.fetcher.FetchResult("u", "u", 200, "<p>x</p>"))
    stats = {"new": 0, "skipped": 0, "failed": 0, "blacklist": 0, "too_old": 0}
    r = pipeline.ingest_item(db, need, src, old, None, do_archive=False, stats=stats)
    assert r is None and stats["too_old"] == 1
    doc = db.query(RawDocument).filter_by(url="https://old.example.com/2015-01/02/a.htm").one()
    assert doc.screen_status == "screened_out" and "时效" in doc.screen_reason


def test_recency_keeps_recent_content(db, need, monkeypatch):
    from app.models import Source
    from app.services import pipeline
    from app.services.adapters import DiscoveredItem
    monkeypatch.setattr(settings, "collect_recency_days", 1825)
    src = db.query(Source).first()
    recent = DiscoveredItem(url="https://new.example.com/2026-07/20/a.htm", title="近期安全通报")
    monkeypatch.setattr(pipeline.fetcher, "fetch",
                        lambda *a, **k: pipeline.fetcher.FetchResult(
                            "https://new.example.com/2026-07/20/a.htm",
                            "https://new.example.com/2026-07/20/a.htm", 200, "<p>正文内容</p>"))
    stats = {"new": 0, "skipped": 0, "failed": 0, "blacklist": 0, "too_old": 0}
    r = pipeline.ingest_item(db, need, src, recent, None, do_archive=False, stats=stats)
    assert r is not None and stats["too_old"] == 0


# ---------------- 栏目自动发现 ----------------

def test_find_columns_picks_relevant():
    html = """<html><body><nav>
      <a href="/zhifa/index.htm">执法处罚</a>
      <a href="/wangan/tongbao.htm">网络安全通报</a>
      <a href="/about.htm">关于我们</a>
      <a href="/zhaopin.htm">招聘</a>
      <a href="https://other-site.com/x">外站链接</a>
      <a href="/">首页</a>
    </body></html>"""
    cols = columns.find_columns(html, "https://www.gov-demo.cn/")
    urls = [c["url"] for c in cols]
    assert any("zhifa" in u for u in urls)
    assert any("tongbao" in u for u in urls)
    assert not any("about" in u or "zhaopin" in u for u in urls)     # 无关/停用词排除
    assert not any("other-site.com" in u for u in urls)              # 只要本站
    assert not any(url_tools.registered_domain(u.split('/')[2]) == "" for u in urls)


def test_is_root_only():
    assert columns.is_root_only("https://www.cac.gov.cn/")
    assert columns.is_root_only("https://cac.gov.cn")
    assert not columns.is_root_only("https://www.cac.gov.cn/zhifa/index.htm")


# ---------------- 候选名校验(F5:日期不当源) ----------------

def test_candidate_rejects_date_like(db, need):
    from app.services import discovery
    # 纯日期公众号名 → 不登记
    assert discovery.record_evidence(db, None, "wechat_reference",
                                     display_name="2026-07-22", wechat_account="2026-07-22") is None
    # 正常主体名 → 登记
    assert discovery.record_evidence(db, None, "wechat_reference",
                                     display_name="安全内参", wechat_account="安全内参") == "mp:安全内参"


def test_valid_subject_helper():
    from app.services.discovery import _valid_subject
    assert not _valid_subject("2026-07-22")
    assert not _valid_subject("123")
    assert not _valid_subject("  ")
    assert _valid_subject("FreeBuf")
    assert _valid_subject("网信中国")


# ---------------- 删除不 500(有采集记录无文档) ----------------

def test_delete_source_with_crawlrun_no_docs(db, admin_user):
    from app.api.routes import SourceIn, create_source, delete_source
    from app.models import CrawlRun, Source
    r = create_source(SourceIn(name="有采集记录的源", entry_url="https://delcr.example.com/col",
                               kind="page"), db, admin_user)
    sid = r["id"]
    db.add(CrawlRun(source_id=sid, status="ok")); db.flush()   # 有 CrawlRun、无 RawDocument
    out = delete_source(sid, db, admin_user)
    assert out["action"] == "deleted"          # 不再 500,记账行一并清掉
    assert db.get(Source, sid) is None


@pytest.fixture()
def admin_user(db):
    from app.models import AppUser
    u = db.query(AppUser).filter_by(role="admin").first()
    if not u:
        from app.auth import hash_password
        u = AppUser(username="admin_rc", display_name="admin_rc",
                    password_hash=hash_password("x"), role="admin")
        db.add(u); db.flush()
    return u
