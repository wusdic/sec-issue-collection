"""并行文档处理:worker 线程独立会话 + 共享诊断记录器仍能留痕。"""
from concurrent.futures import ThreadPoolExecutor

from app.models import RawDocument, RunTrace
from app.services import crawl_runner, diagnostics


def _mk_doc(db, need, sid, i):
    url = f"https://conc.example.com/2026-07/{i:02d}/a.htm"
    d = RawDocument(need_id=need.id, source_id=sid, url=url, url_normalized=url,
                    title=f"某公司数据泄露事件{i}", content_text="某公司遭黑客攻击导致数据泄露，客户信息被窃取。",
                    screen_status="pending", is_primary=True)
    db.add(d); db.flush()
    return d.id


def test_parallel_processing_creates_events_and_traces(db, need):
    from app.models import Source
    src = db.query(Source).first()
    ids = [_mk_doc(db, need, src.id, i) for i in range(6)]
    db.commit()

    # 主线程开诊断会话,worker 线程绑定同一记录器
    with diagnostics.session(job_id=None) as rec:
        with ThreadPoolExecutor(max_workers=4) as ex:
            results = list(ex.map(lambda did: crawl_runner._process_one(need.id, did, rec), ids))

    actions = [r.get("action") for r in results]
    assert all(a is not None for a in actions)
    # 至少产出若干草稿事件(MockLLM 对含关键词文本判为相关并抽取)
    assert actions.count("draft_created") >= 1
    # worker 线程里的 LLM/决策留痕已落库(证明 bind 生效、跨线程可留痕)
    assert db.query(RunTrace).filter_by(kind="screen").count() >= 6


def test_process_one_isolated_session(db, need):
    """_process_one 用独立会话,异常返回 error 而非抛出。"""
    r = crawl_runner._process_one(need.id, 99999999, None)  # 不存在的 doc
    assert r["action"] == "error"
