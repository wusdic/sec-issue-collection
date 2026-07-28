"""待人工队列的出口:重跑 / 并入事件 / 判为不相干(此前只进不出)。"""
import pytest

from app.api.routes import ResolveDocIn, resolve_document
from app.models import AppUser, Event, EventSource, RawDocument, Source


@pytest.fixture()
def editor(db):
    u = db.query(AppUser).filter_by(role="editor").first()
    if not u:
        from app.auth import hash_password
        u = AppUser(username="ed_mq", display_name="ed_mq",
                    password_hash=hash_password("x"), role="editor")
        db.add(u); db.flush()
    return u


def _mk_doc(db, need, tag):
    src = db.query(Source).first()
    d = RawDocument(need_id=need.id, source_id=src.id, url=f"https://mq.example.com/{tag}",
                    url_normalized=f"https://mq.example.com/{tag}", title=f"待人工{tag}",
                    content_text="正文", screen_status="manual_queue",
                    screen_reason="疑似同事件,请人工确认")
    db.add(d); db.flush()
    return d


def test_requeue_puts_doc_back_to_pending(db, need, editor):
    d = _mk_doc(db, need, "rq")
    out = resolve_document(d.id, ResolveDocIn(action="requeue"), db, editor)
    assert out["screen_status"] == "pending"      # 下轮会重新粗筛+抽取


def test_discard_removes_from_queue(db, need, editor):
    d = _mk_doc(db, need, "dc")
    out = resolve_document(d.id, ResolveDocIn(action="discard", note="营销稿"), db, editor)
    assert out["screen_status"] == "screened_out" and "不相干" in out["reason"]


def test_attach_links_doc_as_event_source(db, need, editor):
    ev = Event(event_id="SEC-MQ-0001", need_id=need.id, payload={"title": "某银行数据泄露"},
               status="draft")
    db.add(ev); db.flush()
    d = _mk_doc(db, need, "at")
    out = resolve_document(d.id, ResolveDocIn(action="attach", event_id=ev.event_id), db, editor)
    assert out["screen_status"] == "screened_out"
    # 文档已作为补充来源挂到事件上(EventSource + payload.sources 双写)
    assert db.query(EventSource).filter_by(event_id=ev.event_id, doc_id=d.id).first() is not None
    assert any(s.get("url_or_doc_number") == d.url for s in (ev.payload.get("sources") or []))


def test_attach_rejects_unknown_event(db, need, editor):
    d = _mk_doc(db, need, "bad")
    with pytest.raises(Exception):
        resolve_document(d.id, ResolveDocIn(action="attach", event_id="NOPE-1"), db, editor)


def test_invalid_action_rejected(db, need, editor):
    d = _mk_doc(db, need, "inv")
    with pytest.raises(Exception):
        resolve_document(d.id, ResolveDocIn(action="whatever"), db, editor)
