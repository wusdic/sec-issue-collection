"""并行采集回归:短事务不再抢死 SQLite 写锁;并发建草稿不丢事件;僵尸任务可回收。"""
import threading
import time

from app.models import CrawlJob, Event, Source
from app.services import crawl_runner, events, pipeline
from app.services.adapters import DiscoveredItem


class _Adapter:
    kind = "page"

    def __init__(self, n=3):
        self.n = n

    def discover_page(self, page):
        if page > 0:
            return None
        time.sleep(0.3)                      # 模拟网络抓取耗时(此期间不得占着写锁)
        return [DiscoveredItem(url=f"https://p{id(self)}/{i}", title=f"某公司数据泄露{i}")
                for i in range(self.n)]


def test_parallel_crawl_sources_no_lock_error(db, need, monkeypatch):
    """5 个源并发抓取:此前整源一个长事务会让 4 个 worker 报 database is locked。"""
    from app.db import SessionLocal
    srcs = []
    for i in range(5):
        s = Source(name=f"并发源{i}", kind="page", adapter="generic_list", credibility="S3",
                   tier="B", lifecycle="active", serves_needs=[need.id],
                   entry_url=f"https://conc{i}.example.com/col")
        db.add(s); srcs.append(s)
    db.commit()
    ids = [s.id for s in srcs]

    monkeypatch.setattr(pipeline, "get_adapter", lambda source: _Adapter())
    monkeypatch.setattr(pipeline.fetcher, "fetch",
                        lambda url, **k: pipeline.fetcher.FetchResult(url, url, 200, "<p>正文内容</p>"))

    results = []

    def run(sid):
        wdb = SessionLocal()
        try:
            from app.models import NeedProfile
            n = wdb.get(NeedProfile, need.id)
            r = pipeline.crawl_source(wdb, n, wdb.get(Source, sid), max_pages=1, do_archive=False)
            wdb.commit()
            results.append((r.status, r.error))
        except Exception as e:  # noqa: BLE001
            results.append(("exception", f"{type(e).__name__}: {e}"))
        finally:
            wdb.close()

    ts = [threading.Thread(target=run, args=(i,)) for i in ids]
    [t.start() for t in ts]
    [t.join() for t in ts]

    failures = [r for r in results if r[0] != "ok"]
    assert not failures, f"并发采集不应失败: {failures}"
    assert len(results) == 5


def test_concurrent_create_draft_keeps_all_events(db, need, monkeypatch):
    """并发建草稿:事件号竞态此前会撞主键丢结果,现在应全部成功且号不重复。"""
    from app.db import SessionLocal
    from app.services import llm as llmmod

    class _Slow(llmmod.MockLLM):
        def embed(self, text):
            time.sleep(0.3)                  # 放大取号→插入之间的竞态窗口
            return [0.1] * 8

    monkeypatch.setattr(llmmod, "_client", _Slow())
    got = []

    def w(i):
        s = SessionLocal()
        try:
            ev = events.create_draft(s, need.id, {"title": f"并发{i}", "org_name": f"公司{i}"})
            s.commit(); got.append(ev.event_id)
        except Exception as e:  # noqa: BLE001
            got.append(f"FAIL {type(e).__name__}")
        finally:
            s.close()

    ts = [threading.Thread(target=w, args=(i,)) for i in range(5)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not [g for g in got if str(g).startswith("FAIL")], got
    assert len(set(got)) == 5, f"事件号应互不相同: {got}"


def test_reap_orphan_jobs(db, need):
    """进程重启后残留的 running 任务应被回收,否则页面永远提示已有任务在跑。"""
    j = CrawlJob(need_id=need.id, status="running", phase="抓取")
    db.add(j); db.commit()
    jid = j.id
    n = crawl_runner.reap_orphan_jobs()
    assert n >= 1
    db.expire_all()
    assert db.get(CrawlJob, jid).status == "failed"


def test_column_discovery_does_not_hold_write_lock(db, need, monkeypatch):
    """栏目发现必须"先跑完网络、再一次性写库",否则会占着 SQLite 写锁做网络 I/O,
    让其他并行 worker 全部卡住(实测表现为任务跑到一半就不动了)。"""
    from app.config import settings
    from app.services import columns
    monkeypatch.setattr(settings, "column_min_articles", 1)
    monkeypatch.setattr(settings, "column_consistency_min", 0.0)
    monkeypatch.setattr(settings, "auto_column_refresh_days", 7)

    root = Source(name="根域站", kind="page", adapter="generic_rss", credibility="S1", tier="B",
                  lifecycle="active", serves_needs=[need.id], entry_url="https://lockchk.cn/",
                  site_key="lockchk.cn", identity_key="lockchk.cn", adapter_config={})
    db.add(root); db.commit()

    order = []          # 记录"网络抓取"与"写库"的先后顺序
    root_html = ('<a href="/zhifa/a.htm">执法处罚</a><a href="/tongbao/b.htm">安全通报</a>')
    art = "".join(f'<a href="/c/2026-07/{i:02d}/x.htm">通报{i}</a>' for i in range(6))

    def fake_fetch(url, **k):
        order.append(("fetch", url))
        html = root_html if url.rstrip("/") == "https://lockchk.cn" else art
        return columns.fetcher.FetchResult(url, url, 200, html)
    monkeypatch.setattr(columns.fetcher, "fetch", fake_fetch)

    real_flush = db.flush
    def spy_flush(*a, **kw):
        order.append(("db", None))
        return real_flush(*a, **kw)
    monkeypatch.setattr(db, "flush", spy_flush)

    kids, recomputed = columns.discover_and_persist(db, root)
    assert recomputed is True and len(kids) >= 1
    # 关键断言:最后一次网络抓取必须发生在第一次写库之前
    last_fetch = max(i for i, (k, _) in enumerate(order) if k == "fetch")
    first_db = min((i for i, (k, _) in enumerate(order) if k == "db"), default=10**9)
    assert last_fetch < first_db, f"写库后仍在抓网络(会占着写锁): {order}"
