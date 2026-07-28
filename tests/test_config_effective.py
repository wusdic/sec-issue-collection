"""配置真正生效:排除词降噪、源轮转、日常采集自动停用、日报按严重度排序。"""
from datetime import datetime, timedelta

from app.config import settings
from app.models import KeywordSet, Source
from app.services import crawl_runner, pipeline
from app.services.adapters import DiscoveredItem


# ---------------- 排除词(设置页填了必须生效) ----------------

def test_hits_negative_terms():
    neg = ["招聘", "课程 培训班"]
    assert pipeline._hits_negative("某公司安全工程师招聘", neg) == "招聘"
    assert pipeline._hits_negative("网络安全课程与培训班开课", neg) == "课程 培训班"   # 多词需同时出现
    assert pipeline._hits_negative("某公司数据泄露事件", neg) is None
    assert pipeline._hits_negative(None, neg) is None
    assert pipeline._hits_negative("任意标题", []) is None


def test_negative_terms_filter_items_before_fetch(db, need, monkeypatch):
    """命中排除词的条目不该被抓取入库(省带宽与大模型开销)。"""
    ks = db.query(KeywordSet).filter_by(need_id=need.id, is_active=True).first()
    content = dict(ks.content or {})
    content["negative_terms"] = ["招聘"]
    ks.content = content
    db.flush()

    # 用"具体栏目"源:根域源已改为不抓首页(见 test_precise_sources.py),那条路径不适合验排除词
    src = Source(name="排除词测试栏目", kind="page", adapter="generic_list", credibility="S3",
                 tier="B", lifecycle="active", serves_needs=[need.id],
                 entry_url="https://neg.example.com/col/")
    db.add(src); db.flush()
    items = [DiscoveredItem(url="https://neg.example.com/a", title="某安全公司招聘工程师"),
             DiscoveredItem(url="https://neg.example.com/b", title="某公司数据泄露被攻击")]

    class _A:
        kind = "page"
        def discover_page(self, page):
            return items if page == 0 else None

    monkeypatch.setattr(pipeline, "get_adapter", lambda s: _A())
    monkeypatch.setattr(pipeline.fetcher, "fetch",
                        lambda url, **k: pipeline.fetcher.FetchResult(url, url, 200, "<p>正文</p>"))
    run = pipeline.crawl_source(db, need, src, max_pages=1, do_archive=False)
    from app.models import RawDocument
    assert db.query(RawDocument).filter_by(url="https://neg.example.com/a").first() is None  # 被排除
    assert db.query(RawDocument).filter_by(url="https://neg.example.com/b").first() is not None
    assert run.urls_new == 1


# ---------------- 选源轮转 ----------------

def test_pick_sources_rotates_least_recently_crawled(db, need):
    """限制源数时应轮转:最久没成功采过的优先,而不是永远只采 id 最小的几个。"""
    now = datetime.utcnow()
    made = []
    for i, ago in enumerate([1, 100, None, 50]):     # None = 从未采过
        s = Source(name=f"轮转源{i}", kind="page", adapter="generic_rss", credibility="S3",
                   tier="B", lifecycle="active", serves_needs=[need.id],
                   entry_url=f"https://rot{i}.example.com/",
                   last_success_at=None if ago is None else now - timedelta(hours=ago))
        db.add(s); made.append(s)
    db.flush()
    order = [s.name for s in crawl_runner._pick_sources(db, need, 0)]   # 全量,看相对顺序
    pos = {n: order.index(n) for n in [s.name for s in made]}
    # 从未采过的排在所有采过的之前
    assert pos["轮转源2"] < pos["轮转源1"] < pos["轮转源3"] < pos["轮转源0"], pos
    # 关键性质:最久没采的(100h前)排在最近刚采的(1h前)之前 → 会被轮到
    assert pos["轮转源1"] < pos["轮转源0"]


def test_pick_sources_limit_zero_means_all(db, need):
    all_src = crawl_runner._pick_sources(db, need, 0)
    some = crawl_runner._pick_sources(db, need, 1)
    assert len(all_src) >= len(some) and len(some) == 1


# ---------------- 日常采集也自动停用坏源 ----------------

def test_crawl_failure_auto_retires_source(db, need, monkeypatch):
    monkeypatch.setattr(settings, "source_auto_retire_fail_streak", 2)
    src = Source(name="坏源", kind="page", adapter="generic_rss", credibility="S3", tier="B",
                 lifecycle="active", serves_needs=[need.id], entry_url="https://bad.example.com/col",
                 fail_streak=1)
    db.add(src); db.flush()

    class _Boom:
        kind = "page"
        def discover_page(self, page):
            raise RuntimeError("站点不可达")

    monkeypatch.setattr(pipeline, "get_adapter", lambda s: _Boom())
    run = pipeline.crawl_source(db, need, src, max_pages=1, do_archive=False)
    assert run.status == "failed"
    assert src.fail_streak >= 2
    assert src.lifecycle == "retired"        # 日常采集也会自动停用(此前只有手动体检才会)
