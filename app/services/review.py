"""复核状态机(M5):extracted → first_review → [second_review] → published/rejected。

金额双签:payload 含 confirmed 金额 ⇒ needs_double,一审二审必须不同人。
"""
from sqlalchemy.orm import Session

from app.models import AuditLog, Event, ReviewTask
from app.services.events import PublishError, publish
from app.services.money_guard import confirmed_fields


class ReviewError(ValueError):
    pass


def get_task(db: Session, event_id: str) -> ReviewTask:
    task = db.query(ReviewTask).filter_by(event_id=event_id).order_by(ReviewTask.id.desc()).first()
    if not task:
        raise ReviewError(f"事件 {event_id} 无复核任务")
    return task


def submit_for_review(db: Session, event_id: str, user_id: int):
    task = get_task(db, event_id)
    if task.stage != "extracted":
        raise ReviewError(f"当前阶段 {task.stage} 不能提交")
    task.stage = "first_review"
    db.add(AuditLog(user_id=user_id, action="review.submit", target=event_id))
    db.flush()
    return task


def approve(db: Session, event_id: str, user_id: int, record_schema: dict,
            confirm_allowed: list[str] | None = None) -> ReviewTask:
    """一审/二审通过;末审通过即发布(发布校验红线在 publish 内强制)。"""
    task = get_task(db, event_id)
    ev = db.get(Event, event_id)
    task.needs_double = bool(confirmed_fields(ev.payload))  # 复核中可能新增确认金额
    if task.stage == "extracted":
        task.stage = "first_review"
    if task.stage == "first_review":
        task.first_reviewer = user_id
        if task.needs_double:
            task.stage = "second_review"
            db.add(AuditLog(user_id=user_id, action="review.first_approve", target=event_id))
            db.flush()
            return task
        _do_publish(db, ev, task, record_schema, confirm_allowed, user_id)
        return task
    if task.stage == "second_review":
        if task.first_reviewer == user_id:
            raise ReviewError("双签红线:二审复核人不能与一审相同")
        task.second_reviewer = user_id
        # 二审确认 → 清除 pending_human 标记(人工已确认 confirmed 金额)
        payload = dict(ev.payload)
        for f in confirmed_fields(payload):
            money = dict(payload[f])
            money.pop("pending_human", None)
            payload[f] = money
        ev.payload = payload
        _do_publish(db, ev, task, record_schema, confirm_allowed, user_id)
        return task
    raise ReviewError(f"阶段 {task.stage} 不可批准")


def _do_publish(db, ev, task, record_schema, confirm_allowed, user_id):
    try:
        publish(db, ev, record_schema, confirm_allowed, by_user=user_id)
    except PublishError:
        raise
    task.stage = "published"
    db.add(AuditLog(user_id=user_id, action="review.publish", target=ev.event_id))
    db.flush()


def reject(db: Session, event_id: str, user_id: int, reason: str) -> ReviewTask:
    task = get_task(db, event_id)
    task.stage = "rejected"
    task.comments = list(task.comments or []) + [{"by": user_id, "action": "reject", "reason": reason}]
    db.add(AuditLog(user_id=user_id, action="review.reject", target=event_id, detail={"reason": reason}))
    db.flush()
    return task
