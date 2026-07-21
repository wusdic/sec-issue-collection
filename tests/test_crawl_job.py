"""后台采集任务:进度、日志、状态机(mock 网络,不联网)。"""
from app.models import CrawlJob, CrawlLog
from app.services import crawl_runner, pipeline


class _FakeRun:
    status = "ok"
    urls_found = 3
    urls_new = 3
    error = None


def test_crawl_job_lifecycle(db, need, monkeypatch):
    monkeypatch.setattr(pipeline, "crawl_source", lambda db, need, src, **k: _FakeRun())
    monkeypatch.setattr(pipeline, "process_document", lambda db, need, doc: {"action": "screened_out"})
    job = CrawlJob(need_id=need.id, status="running", limit_sources=2)
    db.add(job)
    db.commit()
    jid = job.id
    crawl_runner._run(jid)
    db.expire_all()
    j = db.get(CrawlJob, jid)
    assert j.status == "done"
    assert j.total_sources >= 1
    assert j.done_sources == j.total_sources
    logs = db.query(CrawlLog).filter_by(job_id=jid).all()
    assert any("开始采集" in l.message for l in logs)
    assert any("采集完成" in l.message for l in logs)


def test_crawl_job_records_source_error(db, need, monkeypatch):
    def boom(db, need, src, **k):
        raise RuntimeError("反爬拦截 403")
    monkeypatch.setattr(pipeline, "crawl_source", boom)
    monkeypatch.setattr(pipeline, "process_document", lambda db, need, doc: {"action": "screened_out"})
    job = CrawlJob(need_id=need.id, status="running", limit_sources=1)
    db.add(job)
    db.commit()
    jid = job.id
    crawl_runner._run(jid)
    db.expire_all()
    j = db.get(CrawlJob, jid)
    assert j.status == "done"  # 单源失败不使整任务失败
    errs = db.query(CrawlLog).filter_by(job_id=jid, level="error").all()
    assert any("反爬拦截 403" in l.message for l in errs)


def test_has_running_gate(db, need):
    j = CrawlJob(need_id=need.id, status="running", limit_sources=1)
    db.add(j)
    db.commit()
    assert crawl_runner.has_running(db, need.id) is not None
    j.status = "done"
    db.commit()
    assert crawl_runner.has_running(db, need.id) is None
