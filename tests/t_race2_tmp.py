import threading
from concurrent.futures import ThreadPoolExecutor
from app.db import SessionLocal
from app.models import Source, NeedProfile, CrawlRun
from app.services import pipeline
from app.services.adapters import DiscoveredItem
import app.services.fetcher as fetcher

def _fake_fetch(url, referer=None, timeout=None, render=False):
    return fetcher.FetchResult(url=url, final_url=url, status=200,
                               html="<html><body>"+"某公司遭黑客攻击导致数据泄露。"*30+"</body></html>")

def test_same_url_two_sources(db, need, monkeypatch):
    monkeypatch.setattr(fetcher, "fetch", _fake_fetch)
    srcs = db.query(Source).limit(2).all(); sid1, sid2 = srcs[0].id, srcs[1].id
    db.commit()
    URL = "https://dup2.example.com/2026-07/01/same.htm"
    bar = threading.Barrier(2)

    def work(sid):
        w = SessionLocal()
        try:
            n = w.get(NeedProfile, need.id); s = w.get(Source, sid)
            run = CrawlRun(source_id=s.id); w.add(run); w.flush()
            from app.services import dedup
            dedup.find_existing_url(w, URL)   # 两边都先查重(都查不到)
            bar.wait()                        # 同步后再各自插入
            stats = {"new":0,"skipped":0,"failed":0,"blacklist":0,"too_old":0}
            try:
                pipeline.ingest_item(w, n, s, DiscoveredItem(url=URL, title="同一篇文章"),
                                     run.id, do_archive=False, stats=stats)
                # 模拟 crawl_source 异常后仍继续写 run 统计
                run.urls_new = stats["new"]; w.flush(); w.commit(); return "ok"
            except Exception as e:
                first = f"{type(e).__name__}: {str(e)[:60]}"
                try:
                    run.status="failed"; run.urls_new=0; w.flush()   # crawl_source 里 except 之后的 flush
                    return f"insert失败({first}) 但收尾flush成功"
                except Exception as e2:
                    return f"insert失败({first}) → 收尾flush也炸 {type(e2).__name__}: {str(e2)[:70]}"
        finally:
            try: w.rollback()
            except Exception: pass
            w.close()

    with ThreadPoolExecutor(max_workers=2) as ex:
        res=[f.result() for f in [ex.submit(work,sid1), ex.submit(work,sid2)]]
    print("RESULTS:", res)
