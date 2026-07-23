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


# ---------------- 栏目验证 + 持久化 ----------------

_COL_HTML = """<html><body>
  <a href="/zhifa/2026-07/01/a.htm">处罚公告一</a>
  <a href="/zhifa/2026-07/02/b.htm">处罚公告二</a>
  <a href="/zhifa/2026-06/15/c.htm">处罚公告三</a>
  <a href="/zhifa/2026-06/10/d.htm">处罚公告四</a>
  <a href="/zhifa/2026-05/09/e.htm">处罚公告五</a>
  <a href="/about">关于</a>
</body></html>"""


def test_validate_column_consistency(monkeypatch):
    from app.services import columns
    monkeypatch.setattr(settings, "column_min_articles", 5)
    monkeypatch.setattr(settings, "column_consistency_min", 0.5)
    monkeypatch.setattr(columns.fetcher, "fetch",
                        lambda *a, **k: columns.fetcher.FetchResult(
                            "https://g.cn/zhifa/", "https://g.cn/zhifa/", 200, _COL_HTML))
    v = columns.validate_column("https://g.cn/zhifa/")
    assert v["valid"] and v["article_count"] >= 5 and v["consistency"] >= 0.5


def test_validate_column_rejects_sparse(monkeypatch):
    from app.services import columns
    monkeypatch.setattr(settings, "column_min_articles", 5)
    html = '<a href="/x/1.htm">一</a><a href="/y/2.htm">二</a>'
    monkeypatch.setattr(columns.fetcher, "fetch",
                        lambda *a, **k: columns.fetcher.FetchResult("https://g.cn/nav/", "https://g.cn/nav/", 200, html))
    assert columns.validate_column("https://g.cn/nav/")["valid"] is False


def test_discover_and_persist_records_and_reuses(db, need, monkeypatch):
    from app.models import Source
    from app.services import columns
    monkeypatch.setattr(settings, "column_min_articles", 5)
    monkeypatch.setattr(settings, "column_consistency_min", 0.5)
    monkeypatch.setattr(settings, "auto_column_refresh_days", 7)
    root = Source(name="某政务站", kind="page", adapter="generic_rss", credibility="S1", tier="B",
                  lifecycle="active", serves_needs=[need.id], entry_url="https://gov-x.cn/",
                  site_key="gov-x.cn", identity_key="gov-x.cn", adapter_config={})
    db.add(root); db.flush()
    root_html = '<a href="/zhifa/index.htm">执法处罚</a><a href="/about">关于</a>'

    def fake_fetch(url, **k):
        html = root_html if url.rstrip("/") == "https://gov-x.cn" else _COL_HTML
        return columns.fetcher.FetchResult(url, url, 200, html)
    monkeypatch.setattr(columns.fetcher, "fetch", fake_fetch)

    kids1, recomputed1 = columns.discover_and_persist(db, root)
    assert recomputed1 is True and len(kids1) == 1
    child = kids1[0]
    assert child.discovered_from == "column_auto"
    assert child.adapter_config["parent_site_id"] == root.id
    assert root.adapter_config.get("columns_discovered_at")   # 已记录时间戳

    # 再次调用 → TTL 内直接复用,不重算(不新增子源)
    kids2, recomputed2 = columns.discover_and_persist(db, root)
    assert recomputed2 is False and len(kids2) == 1 and kids2[0].id == child.id
    assert db.query(Source).filter_by(discovered_from="column_auto").count() == 1


def test_child_columns_excluded_from_scheduling(db, need, monkeypatch):
    from app.models import Source
    from app.services.crawl_runner import _pick_sources
    child = Source(name="子栏目", kind="page", adapter="generic_list", credibility="S1", tier="B",
                   lifecycle="active", serves_needs=[need.id], entry_url="https://p.cn/col/",
                   adapter_config={"parent_site_id": 999})
    db.add(child); db.flush()
    picked = _pick_sources(db, need, 999)
    assert child.id not in [s.id for s in picked]   # 子栏目不独立占名额
