"""KPI 与报表(方案 12.1 / 16.1):看板数据与硬约束校验。"""
from collections import Counter, defaultdict
from datetime import date, timedelta

from sqlalchemy.orm import Session

from app.models import Event, EventSource, FollowupTask, Lead, RawDocument
from app.services.money_guard import LOSS_FIELDS, confirmed_fields


def heatmap(db: Session, need_id: str, days: int = 365) -> dict:
    """行业 × 手段 × 后果 热力图数据。"""
    since = date.today() - timedelta(days=days)
    by_attack, by_consequence = Counter(), Counter()
    for ev in db.query(Event).filter(Event.need_id == need_id,
                                     Event.status.in_(["published", "monitoring", "closed"])).all():
        if ev.disclosed_date and ev.disclosed_date < since:
            continue
        ind = ev.industry_l1 or "未知"
        for a in ev.attack_types or []:
            by_attack[(ind, a)] += 1
        for c in ev.consequences or []:
            by_consequence[(ind, c)] += 1
    return {
        "industry_x_attack": [{"industry": k[0], "attack_type": k[1], "count": v} for k, v in by_attack.most_common()],
        "industry_x_consequence": [{"industry": k[0], "consequence": k[1], "count": v} for k, v in by_consequence.most_common()],
    }


def _amount(channel) -> float:
    if not isinstance(channel, dict):
        return 0.0
    if channel.get("point") is not None:
        return float(channel["point"])
    if channel.get("low") is not None:
        return float(channel["low"])  # 区间取下限:保守口径
    return 0.0


def loss_stats(db: Session, need_id: str, scope: str = "confirmed") -> dict:
    """损失分布:默认口径只汇总已确认;声称/估算需显式选择且报表标注。"""
    assert scope in ("confirmed", "claimed", "estimated")
    channel_key = f"{scope}_cny"
    per_loss = defaultdict(float)
    per_industry = defaultdict(float)
    n_events = 0
    for ev in db.query(Event).filter(Event.need_id == need_id,
                                     Event.status.in_(["published", "monitoring", "closed"])).all():
        p = ev.payload or {}
        touched = False
        for f in LOSS_FIELDS:
            amt = _amount((p.get(f) or {}).get(channel_key))
            if amt:
                per_loss[f] += amt
                per_industry[ev.industry_l1 or "未知"] += amt
                touched = True
        if touched:
            n_events += 1
    return {"scope": scope, "scope_note": "默认统计口径=已确认;本报表口径=" + scope,
            "events_counted": n_events,
            "by_loss_category": dict(per_loss), "by_industry": dict(per_industry)}


def controls_stats(db: Session, need_id: str) -> dict:
    """控制缺失统计(B9):建设类 vs 效果类产品方向信号。"""
    counter = Counter()
    for ev in db.query(Event).filter(Event.need_id == need_id,
                                     Event.status.in_(["published", "monitoring", "closed"])).all():
        for c in (ev.payload or {}).get("security_controls") or []:
            if c.get("status") in ("缺位", "在位但失效", "在位被绕过"):
                counter[(c.get("control"), c.get("status"))] += 1
    return {"items": [{"control": k[0], "status": k[1], "count": v} for k, v in counter.most_common()]}


def whitespace(db: Session, need_id: str) -> list[dict]:
    """白区清单:无产品映射的已发布事件(产品缺口信号)。"""
    out = []
    for ev in db.query(Event).filter(Event.need_id == need_id,
                                     Event.status.in_(["published", "monitoring"])).all():
        if not (ev.payload or {}).get("sellable_mapping"):
            out.append({"event_id": ev.event_id, "title": (ev.payload or {}).get("title"),
                        "attack_types": ev.attack_types})
    return out


def traceability_check(db: Session, need_id: str) -> dict:
    """硬约束回归(11.4):任何 confirmed 金额必须可回溯到 S1/S2 来源。违规>0 时报表层拒绝出数。"""
    violations = []
    for ev in db.query(Event).filter(Event.need_id == need_id,
                                     Event.status.in_(["published", "monitoring", "closed"])).all():
        conf = confirmed_fields(ev.payload or {})
        if not conf:
            continue
        creds = {es.credibility for es in db.query(EventSource).filter_by(event_id=ev.event_id).all()}
        creds |= {s.get("credibility") for s in (ev.payload or {}).get("sources") or []}
        if not (creds & {"S1", "S2"}):
            violations.append({"event_id": ev.event_id, "fields": conf})
    return {"ok": not violations, "violations": violations}


def dashboard(db: Session, need_id: str) -> dict:
    from app.models import ReviewTask
    total = db.query(Event).filter_by(need_id=need_id).count()
    published = db.query(Event).filter(Event.need_id == need_id,
                                       Event.status.in_(["published", "monitoring", "closed"])).count()
    docs = db.query(RawDocument).filter_by(need_id=need_id).count()
    # 待复核 = 复核队列未完成(新抽取/待一审/待二审),与复核台"待复核(全部)"口径一致
    pending_review = (
        db.query(ReviewTask).join(Event, Event.event_id == ReviewTask.event_id)
        .filter(Event.need_id == need_id,
                ReviewTask.stage.in_(["extracted", "first_review", "second_review"])).count()
    )
    # 回访待办按 need 过滤(经 event 关联)
    open_followups = (
        db.query(FollowupTask).join(Event, Event.event_id == FollowupTask.event_id)
        .filter(Event.need_id == need_id, FollowupTask.status == "open").count()
    )
    leads_new = db.query(Lead).filter_by(need_id=need_id, status="new").count()
    scores = [ev.completeness_score for ev in db.query(Event).filter(
        Event.need_id == need_id, Event.completeness_score.isnot(None)).all()]
    return {
        "events_total": total, "events_published": published, "docs_total": docs,
        "pending_review": pending_review, "followups_open": open_followups, "leads_new": leads_new,
        "avg_completeness": round(sum(scores) / len(scores), 1) if scores else None,
        "traceability": traceability_check(db, need_id),
    }
