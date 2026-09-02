"""KPI 与报表(通用平台):交叉表 / 金额汇总 / 状态计数 / 缺字段清单 / 可追溯校验 / 仪表盘。

口径全部来自画像 outputs.reports_engine(见 design/platform/02 F2);声明为 null 的报表返回
enabled=False 而不是报错,前端按此隐藏。物理列经 ROLE_COLUMNS 由角色映射。
"""
from collections import Counter, defaultdict
from datetime import date, timedelta

from sqlalchemy.orm import Session

from app.models import Event, EventSource, FollowupTask, Lead, RawDocument
from app.services import need_ctx, url_tools
from app.services.money_guard import confirmed_fields
from app.services.need_ctx import ROLE_COLUMNS

LIVE_STATUSES = ["published", "monitoring", "closed"]


def _ctx(db, need_id, ctx=None):
    return ctx or need_ctx.get(db, need_id)


def _live(db, need_id):
    return db.query(Event).filter(Event.need_id == need_id, Event.status.in_(LIVE_STATUSES)).all()


def _col(ev: Event, role: str):
    col = ROLE_COLUMNS.get(role)
    return getattr(ev, col, None) if col else None


def _as_list(v) -> list:
    if v in (None, "", [], {}):
        return []
    return [str(x) for x in v if x] if isinstance(v, list) else [str(v)]


def heatmap(db: Session, need_id: str, days: int = 365, ctx=None) -> dict:
    """交叉表:行角色 × 各列角色 的计数(默认 分类 × 标签A / 标签B)。"""
    c = _ctx(db, need_id, ctx)
    ct = c.reports.get("crosstab") or {}
    row_role = ct.get("row_role") or "dim1"
    col_roles = [r for r in (ct.get("col_roles") or ["tags_a", "tags_b"]) if r in ROLE_COLUMNS]
    since = date.today() - timedelta(days=days)
    counters = {r: Counter() for r in col_roles}
    for ev in _live(db, need_id):
        if ev.disclosed_date and ev.disclosed_date < since:
            continue
        rv = _col(ev, row_role) or "未知"
        for cr in col_roles:
            for v in _as_list(_col(ev, cr)):
                counters[cr][(rv, v)] += 1
    return {
        "row_role": row_role, "row_label": c.role_label(row_role),
        "col_roles": col_roles, "col_labels": {r: c.role_label(r) for r in col_roles},
        "tables": {r: [{"row": k[0], "col": k[1], "count": n} for k, n in counters[r].most_common()]
                   for r in col_roles},
    }


def _amount(channel) -> float:
    if isinstance(channel, (int, float)) and not isinstance(channel, bool):
        return float(channel)
    if isinstance(channel, str):
        try:
            return float(channel.replace(",", ""))
        except ValueError:
            return 0.0
    if not isinstance(channel, dict):
        return 0.0
    if channel.get("value") is not None and "point" not in channel:
        return _amount(channel["value"])
    if channel.get("amount") is not None:
        return _amount(channel["amount"])
    if channel.get("point") is not None:
        return float(channel["point"])
    if channel.get("low") is not None:
        return float(channel["low"])  # 区间取下限:保守口径
    return 0.0


def amount_stats(db: Session, need_id: str, scope: str | None = None, ctx=None) -> dict:
    """金额(三态字段)汇总:默认口径只汇总已确认;声称/估算需显式选择且报表标注。"""
    c = _ctx(db, need_id, ctx)
    cfg = c.reports.get("amount_sum")
    if not cfg or not (cfg.get("fields") or c.tristate_fields):
        return {"enabled": False, "note": "本需求未声明金额汇总(outputs.reports_engine.amount_sum)"}
    plain = (cfg.get("kind") or "tristate") == "plain"       # plain:字段本身就是数字/{value},没有三态通道
    scope = "plain" if plain else (scope or cfg.get("default_scope") or "confirmed")
    if not plain:
        assert scope in ("confirmed", "claimed", "estimated")
    channel_key = None if plain else ((c.assertions.get("channels") or {}).get(scope) or f"{scope}_cny")
    fields = list(cfg.get("fields") or c.tristate_fields)
    group_role = cfg.get("group_role") or "dim1"
    per_field, per_group = defaultdict(float), defaultdict(float)
    n_events = 0
    for ev in _live(db, need_id):
        p = ev.payload or {}
        touched = False
        for f in fields:
            amt = _amount(url_tools.dget(p, *f.split("."))) if plain else _amount(url_tools.dget(p, f, channel_key))
            if amt:
                per_field[f] += amt
                per_group[_col(ev, group_role) or "未知"] += amt
                touched = True
        if touched:
            n_events += 1
    labels = c.assertions.get("labels") or {}
    return {"enabled": True, "scope": scope,
            "scope_note": ("字段为普通数值,无三态口径" if plain else "默认统计口径=已确认;本报表口径=" + scope),
            "events_counted": n_events, "group_role": group_role, "group_label": c.role_label(group_role),
            "by_field": {f: {"amount": v, "label": labels.get(f, f)} for f, v in per_field.items()},
            "by_group": dict(per_group),
            # 兼容旧键
            "by_loss_category": dict(per_field), "by_industry": dict(per_group)}


def loss_stats(db: Session, need_id: str, scope: str = "confirmed", ctx=None) -> dict:
    """兼容旧名。"""
    return amount_stats(db, need_id, scope, ctx)


def status_count(db: Session, need_id: str, ctx=None) -> dict:
    """列表字段里各条目的状态计数(如 安全控制 × 缺位/失效;画像 status_count 声明)。"""
    c = _ctx(db, need_id, ctx)
    cfg = c.reports.get("status_count")
    if not cfg or not cfg.get("field"):
        return {"enabled": False, "items": []}
    item_key, status_key = cfg.get("item_key") or "name", cfg.get("status_key") or "status"
    wanted = set(str(x) for x in (cfg.get("statuses") or []))
    counter = Counter()
    for ev in _live(db, need_id):
        v = url_tools.dget(ev.payload or {}, *str(cfg["field"]).split("."))
        for it in (v if isinstance(v, list) else []):
            if not isinstance(it, dict):
                continue
            st = it.get(status_key)
            if wanted and str(st) not in wanted:
                continue
            counter[(it.get(item_key), st)] += 1
    return {"enabled": True, "field": cfg["field"],
            "items": [{"item": k[0], "status": k[1], "count": n} for k, n in counter.most_common()]}


def controls_stats(db: Session, need_id: str, ctx=None) -> dict:
    """兼容旧名。"""
    return status_count(db, need_id, ctx)


def missing_field(db: Session, need_id: str, ctx=None) -> dict:
    """缺失字段清单:已发布记录里某字段为空的(如 无产品映射 = 产品缺口信号)。"""
    c = _ctx(db, need_id, ctx)
    cfg = c.reports.get("missing_field")
    if not cfg or not cfg.get("field"):
        return {"enabled": False, "items": []}
    field = str(cfg["field"])
    out = []
    for ev in db.query(Event).filter(Event.need_id == need_id,
                                     Event.status.in_(["published", "monitoring"])).all():
        if not url_tools.dget(ev.payload or {}, *field.split(".")):
            out.append({"event_id": ev.event_id, "title": (ev.payload or {}).get("title"),
                        "tags_a": _col(ev, "tags_a") or []})
    return {"enabled": True, "field": field, "items": out}


def whitespace(db: Session, need_id: str, ctx=None) -> list[dict]:
    """兼容旧名(返回清单)。"""
    return missing_field(db, need_id, ctx).get("items") or []


def traceability_check(db: Session, need_id: str, ctx=None) -> dict:
    """硬约束回归(11.4):任何 confirmed 断言必须可回溯到画像 confirm_allowed 等级的来源。违规>0 时报表层拒绝出数。"""
    c = _ctx(db, need_id, ctx)
    allowed = set(c.confirm_allowed)
    violations = []
    for ev in _live(db, need_id):
        conf = confirmed_fields(ev.payload or {}, ctx=c)
        if not conf:
            continue
        creds = {es.credibility for es in db.query(EventSource).filter_by(event_id=ev.event_id).all()}
        creds |= {s.get("credibility") for s in (ev.payload or {}).get("sources") or [] if isinstance(s, dict)}
        if not (creds & allowed):
            violations.append({"event_id": ev.event_id, "fields": conf})
    return {"ok": not violations, "violations": violations, "confirm_allowed": sorted(allowed)}


def dashboard(db: Session, need_id: str, ctx=None) -> dict:
    from app.models import ReviewTask
    c = _ctx(db, need_id, ctx)
    total = db.query(Event).filter_by(need_id=need_id).count()
    published = db.query(Event).filter(Event.need_id == need_id, Event.status.in_(LIVE_STATUSES)).count()
    docs = db.query(RawDocument).filter_by(need_id=need_id).count()
    # 待复核 = 复核队列未完成(新抽取/待一审/待二审),与复核台"待复核(全部)"口径一致
    pending_review = (
        db.query(ReviewTask).join(Event, Event.event_id == ReviewTask.event_id)
        .filter(Event.need_id == need_id,
                ReviewTask.stage.in_(["extracted", "first_review", "second_review"])).count()
    )
    open_followups = (
        db.query(FollowupTask).join(Event, Event.event_id == FollowupTask.event_id)
        .filter(Event.need_id == need_id, FollowupTask.status == "open").count()
    )
    leads_new = db.query(Lead).filter_by(need_id=need_id, status="new").count()
    scores = [ev.completeness_score for ev in db.query(Event).filter(
        Event.need_id == need_id, Event.completeness_score.isnot(None)).all()]
    return {
        "need_id": need_id, "need_name": c.name,
        "events_total": total, "events_published": published, "docs_total": docs,
        "pending_review": pending_review, "followups_open": open_followups, "leads_new": leads_new,
        "avg_completeness": round(sum(scores) / len(scores), 1) if scores else None,
        "traceability": traceability_check(db, need_id, c),
        "tiles": list(c.ui.get("dashboard_tiles") or []),
    }
