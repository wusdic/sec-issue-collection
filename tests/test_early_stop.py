"""增量翻页早停:连续遇到已采过的条目即停止翻页,不跑完整站。"""
from app.config import settings
from app.models import Source
from app.services import pipeline
from app.services.adapters import DiscoveredItem


class _PagerAdapter:
    """模拟按时间倒序的分页源:第0页全新,第1页起全是已采过(旧内容)。"""
    kind = "query"

    def __init__(self, pages):
        self._pages = pages
        self.fetched_pages = []

    def search_page(self, query, page, time_filter=None):
        self.fetched_pages.append(page)
        return self._pages[page] if page < len(self._pages) else None


def _mk_pages(new_n, old_n, old_urls):
    p0 = [DiscoveredItem(url=f"https://s/new{i}", title=f"新{i}") for i in range(new_n)]
    p1 = [DiscoveredItem(url=u, title="旧") for u in old_urls[:old_n]]
    p2 = [DiscoveredItem(url=u, title="旧2") for u in old_urls[:old_n]]
    return [p0, p1, p2]


def test_early_stop_on_consecutive_seen(db, need, monkeypatch):
    settings.crawl_stop_consecutive_seen = 5
    src = db.query(Source).filter_by(kind="query").first() or db.query(Source).first()
    src.kind = "query"
    db.flush()

    # 预置一批"已采过"的 URL(第1、2页都是这些)
    from app.models import RawDocument
    from app.services import url_tools
    old_urls = [f"https://s/old{i}" for i in range(10)]
    for u in old_urls:
        db.add(RawDocument(need_id=need.id, source_id=src.id, url=u,
                           url_normalized=url_tools.normalize_url(u), content_text="旧文"))
    db.flush()

    pages = _mk_pages(new_n=3, old_n=10, old_urls=old_urls)
    adapter = _PagerAdapter(pages)
    monkeypatch.setattr(pipeline, "get_adapter", lambda source: adapter)
    monkeypatch.setattr(pipeline.fetcher, "fetch",
                        lambda url, **k: pipeline.fetcher.FetchResult(url, url, 200, "<p>x</p>"))

    run = pipeline.crawl_source(db, need, src, queries=["测试词"], max_pages=3, do_archive=False)
    # 第0页3条新 + 第1页遇到连续5条已采过 → 停,不该翻到第2页
    assert 2 not in adapter.fetched_pages, f"不该翻到第2页: {adapter.fetched_pages}"
    assert run.urls_new == 3
    assert run.urls_skipped >= 5


def test_no_early_stop_flag(db, need, monkeypatch):
    settings.crawl_stop_consecutive_seen = 3
    from app.models import Source
    src = Source(name="持续更新页", kind="query", adapter="baidu_search", credibility="S4",
                 tier="A", lifecycle="active", serves_needs=[need.id],
                 adapter_config={"no_early_stop": True})
    db.add(src)
    db.flush()
    from app.models import RawDocument
    from app.services import url_tools
    olds = [f"https://ns/old{i}" for i in range(6)]
    for u in olds:
        db.add(RawDocument(need_id=need.id, source_id=src.id, url=u,
                           url_normalized=url_tools.normalize_url(u), content_text="旧"))
    db.flush()
    pages = [[DiscoveredItem(url=u, title="旧") for u in olds[:3]],
             [DiscoveredItem(url=u, title="旧") for u in olds[3:6]],
             [DiscoveredItem(url="https://ns/new", title="新")]]
    adapter = _PagerAdapter(pages)
    monkeypatch.setattr(pipeline, "get_adapter", lambda source: adapter)
    monkeypatch.setattr(pipeline.fetcher, "fetch",
                        lambda url, **k: pipeline.fetcher.FetchResult(url, url, 200, "<p>x</p>"))
    pipeline.crawl_source(db, need, src, queries=["词"], max_pages=3, do_archive=False)
    # no_early_stop:即使前两页全重复也翻到第2页拿到新内容
    assert 2 in adapter.fetched_pages


def test_search_engine_not_early_stopped(db, need, monkeypatch):
    """搜索引擎(相关性排序)即使前面全重复也不早停,翻满 max_pages 避免漏采。"""
    from app.services.adapters import BaiduSearchAdapter
    settings.crawl_stop_consecutive_seen = 3
    from app.models import Source
    src = Source(name="百度搜索", kind="query", adapter="baidu_search", credibility="S4",
                 tier="A", lifecycle="active", serves_needs=[need.id], adapter_config={})
    db.add(src); db.flush()
    from app.models import RawDocument
    from app.services import url_tools
    olds = [f"https://se/old{i}" for i in range(8)]
    for u in olds:
        db.add(RawDocument(need_id=need.id, source_id=src.id, url=u,
                           url_normalized=url_tools.normalize_url(u), content_text="旧"))
    db.flush()

    # 真实 BaiduSearchAdapter(SearchEngineAdapter 子类),但 search_page 用假数据
    pages = [[DiscoveredItem(url=u, title="旧") for u in olds[:4]],
             [DiscoveredItem(url=u, title="旧") for u in olds[4:8]],
             [DiscoveredItem(url="https://se/new", title="新")]]
    fetched = []
    class _FakeBaidu(BaiduSearchAdapter):
        def search_page(self, query, page, time_filter=None):
            fetched.append(page)
            return pages[page] if page < len(pages) else None
    monkeypatch.setattr(pipeline, "get_adapter", lambda source: _FakeBaidu(source))
    monkeypatch.setattr(pipeline.fetcher, "fetch",
                        lambda url, **k: pipeline.fetcher.FetchResult(url, url, 200, "<p>x</p>"))
    monkeypatch.setattr("time.sleep", lambda *a: None)
    pipeline.crawl_source(db, need, src, queries=["词"], max_pages=3, do_archive=False)
    # 搜索引擎不早停 → 翻满3页,拿到第2页的新内容
    assert 2 in fetched, f"搜索引擎应翻满不早停: {fetched}"
