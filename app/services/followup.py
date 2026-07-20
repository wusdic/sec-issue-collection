"""生命周期回访(M6 / 行为 B4):发布时按画像 followup_schedule 生成 T+N 任务与一键检索包。"""
from datetime import date, timedelta
from urllib.parse import quote

from sqlalchemy.orm import Session

from app.models import Event, FollowupTask
from app.services.money_guard import LOSS_FIELDS


def _open_reasons(payload: dict) -> list[str]:
    """回访触发条件(方案第 8 节)。"""
    reasons = []
    for f in LOSS_FIELDS:
        money = payload.get(f) or {}
        if money.get("status") in ("仅声称", "仅估算", "待回访"):
            reasons.append(f"{f} 金额未落地({money.get('status')})")
    for act in payload.get("regulatory_actions") or []:
        if "立案" in str(act.get("action", "")) and not act.get("fine_amount"):
            reasons.append("监管已立案未处罚")
    if not payload.get("remediation_actions"):
        reasons.append("事故后采购未知")
    ransom = payload.get("ransom") or {}
    if ransom.get("applicable") and ransom.get("paid") in (None, "未披露"):
        reasons.append("赎金是否支付未披露")
    return reasons


def build_search_pack(payload: dict) -> dict:
    """一键检索包:单位名 × {处罚,判决,中标,招标} 的定向查询链接。"""
    org = payload.get("org_name") or ""
    if not org or org == "未披露":
        return {"note": "单位未披露,回访时先补单位名"}
    q = quote(org)
    return {
        "queries": [f"{org} {kw}" for kw in ("处罚", "判决", "中标", "招标", "整改")],
        "links": {
            "信用中国": f"https://www.creditchina.gov.cn/xinyongxinxi/index.html?keyword={q}",
            "裁判文书网": f"https://wenshu.court.gov.cn/website/wenshu/181217BMTKHNT2W0/index.html?s21={q}",
            "政府采购网": f"http://search.ccgp.gov.cn/bxsearch?searchtype=1&kw={q}",
            "百度": f"https://www.baidu.com/s?wd={q}%20%E4%B8%AD%E6%A0%87%20OR%20%E5%A4%84%E7%BD%9A",
        },
    }


def schedule_followups(db: Session, ev: Event, schedule_days: list[int] | None = None) -> list[FollowupTask]:
    schedule_days = schedule_days or [30, 90, 180, 365]
    reasons = _open_reasons(ev.payload or {})
    if not reasons:
        return []
    base = ev.first_published_at.date() if ev.first_published_at else date.today()
    pack = build_search_pack(ev.payload or {})
    tasks = []
    existing = {t.kind for t in db.query(FollowupTask).filter_by(event_id=ev.event_id).all()}
    for d in schedule_days:
        kind = f"T{d}"
        if kind in existing:
            continue
        t = FollowupTask(event_id=ev.event_id, kind=kind, due_date=base + timedelta(days=d),
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
