"""生命周期回访(通用平台 · 行为 G4):发布时按画像 followup_schedule 生成 T+N 任务与一键检索包。

回访"什么时候该回访"(触发条件)和"回访去哪查"(检索包)原来写死为安全事件库的金额/立案/
采购/赎金;现在全部由画像 `update.followup_triggers` / `update.followup_search` 声明。
"""
from datetime import date, timedelta
from urllib.parse import quote

from sqlalchemy.orm import Session

from app.models import Event, FollowupTask
from app.services import need_ctx
from app.services.need_ctx import dget


def _ctx_for(db, ev: Event, ctx=None):
    return ctx or need_ctx.get(db, ev.need_id)


def _trigger_hit(payload: dict, t: dict) -> bool:
    """一条回访触发规则是否命中(规则种类见 design/platform/02 E2)。"""
    field_ = str(t.get("field") or "")
    when = t.get("when")
    val = dget(payload, field_) if field_ else None
    if when == "status_in":
        return isinstance(val, dict) and val.get("status") in set(t.get("values") or [])
    if when == "value_in":
        return val in set(t.get("values") or [])
    if when == "value_not_in":
        return val not in set(t.get("values") or [])
    if when == "empty":
        return val in (None, "", [], {})
    if when == "list_item_missing":
        items = val if isinstance(val, list) else []
        match = t.get("item_match") or {}
        for it in items:
            if not isinstance(it, dict):
                continue
            if all(str(needle) in str(it.get(k, "")) for k, needle in match.items()) and not it.get(t.get("item_key")):
                return True
        return False
    if when == "flag_unknown":
        obj = val if isinstance(val, dict) else {}
        if not obj.get(t.get("flag_key")):
            return False
        unknown = t.get("unknown_values") or [None, "未披露"]
        return obj.get(t.get("unknown_key")) in unknown
    return False


def _open_reasons(payload: dict, ctx=None) -> list[str]:
    """回访触发原因:逐条对照画像的触发规则。"""
    c = ctx or need_ctx.get(None, need_ctx.default_need_id())
    reasons = []
    for t in c.followup_triggers:
        if _trigger_hit(payload or {}, t):
            reasons.append(str(t.get("reason") or t.get("field") or "待回访"))
    return reasons


def build_search_pack(payload: dict, ctx=None) -> dict:
    """一键检索包:主体 × 后缀词 的定向查询 + 画像声明的站内检索链接。"""
    c = ctx or need_ctx.get(None, need_ctx.default_need_id())
    fs = c.followup_search
    subj = c.get_role(payload or {}, fs.get("subject_role") or "subject")
    subj = str(subj or "").strip()
    if not subj or subj in ("未披露", "未知"):
        return {"note": f"{c.role_label(fs.get('subject_role') or 'subject')}未披露,回访时先补齐"}
    q = quote(subj)
    return {
        "queries": [f"{subj} {kw}" for kw in (fs.get("query_suffixes") or [])],
        "links": {name: str(tpl).format(q=q) for name, tpl in (fs.get("link_templates") or {}).items()},
    }


def schedule_followups(db: Session, ev: Event, schedule_days: list[int] | None = None,
                       ctx=None) -> list[FollowupTask]:
    c = _ctx_for(db, ev, ctx)
    schedule_days = schedule_days or c.followup_schedule
    reasons = _open_reasons(ev.payload or {}, c)
    if not reasons or not schedule_days:
        return []
    base = ev.first_published_at.date() if ev.first_published_at else date.today()
    pack = build_search_pack(ev.payload or {}, c)
    tasks = []
    existing = {t.kind for t in db.query(FollowupTask).filter_by(event_id=ev.event_id).all()}
    for d in schedule_days:
        kind = f"T{d}"
        if kind in existing:
            continue
        t = FollowupTask(event_id=ev.event_id, kind=kind, due_date=base + timedelta(days=int(d)),
                         reason="; ".join(reasons), search_pack=pack)
        db.add(t)
        tasks.append(t)
    if tasks:
        ev.status = "monitoring"
    db.flush()
    return tasks


def due_tasks(db: Session, on: date | None = None) -> list[FollowupTask]:
    on = on or date.today()
    return (
        db.query(FollowupTask)
        .filter(FollowupTask.status == "open", FollowupTask.due_date <= on)
        .order_by(FollowupTask.due_date)
        .all()
    )


def complete_task(db: Session, task_id: int, user_id: int, findings: str = "") -> FollowupTask:
    from datetime import datetime
    t = db.get(FollowupTask, task_id)
    t.status = "done"
    t.findings = findings
    t.done_by = user_id
    t.done_at = datetime.utcnow()
    db.flush()
    return t


def recheck_due(db: Session, need_id: str, ctx=None, on: date | None = None, limit: int = 50) -> dict:
    """再核查接回访:对到期回访任务对应记录的来源文档重抓比对哈希;内容变了就把信号写进任务并记动作。

    文档型记录(法规/政策/公告)的"状态跃迁"(征求意见→发布→生效→废止)往往体现在原页面内容变化上,
    这一步把它变成回访台上看得见的提示,而不是靠人再去翻。
    """
    from app.models import Event, EventSource, RawDocument
    from app.services import actions, need_ctx, verify
    c = ctx or need_ctx.get(db, need_id)
    on = on or date.today()
    checked, changed, errors = 0, [], 0
    tasks = [t for t in due_tasks(db, on) if (db.get(Event, t.event_id) or Event(need_id="")).need_id == need_id]
    seen_docs: dict[int, dict] = {}
    for t in tasks[:limit]:
        for es in db.query(EventSource).filter_by(event_id=t.event_id).all():
            if not es.doc_id:
                continue
            doc = db.get(RawDocument, es.doc_id)
            if doc is None:
                continue
            if doc.id not in seen_docs:
                try:
                    seen_docs[doc.id] = verify.recheck(doc, c)
                except Exception as e:  # noqa: BLE001
                    seen_docs[doc.id] = {"ok": False, "error": str(e)}
                checked += 1
            r = seen_docs[doc.id]
            if not r.get("ok"):
                errors += 1
                continue
            if r.get("changed"):
                tag = f"[内容已变化 {on.isoformat()}]"
                if tag not in (t.reason or ""):
                    t.reason = f"{tag} {t.reason or ''}".strip()
                doc.verification = {**(doc.verification or {}), "content_hash": r["content_hash"],
                                    "changed_at": on.isoformat()}
                changed.append(t.event_id)
                actions.record(db, "record.source_changed",
                               f"记录 {t.event_id} 的来源页面内容已变化,请在回访时核对状态",
                               need_id=need_id, target=t.event_id,
                               detail={"doc_id": doc.id, "url": doc.final_url or doc.url})
    db.flush()
    return {"tasks": len(tasks), "checked": checked, "changed": sorted(set(changed)), "errors": errors}
