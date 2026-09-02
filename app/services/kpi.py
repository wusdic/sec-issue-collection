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
    try:
        q = quality_scorecard(db, need_id, ctx=c)
        quality = {"score": q["score"], "grade": q["grade"], "dimensions": q["dimensions"]}
    except Exception:  # noqa: BLE001 评分卡算不出不影响看板
        quality = None
    return {
        "need_id": need_id, "need_name": c.name, "quality": quality,
        "events_total": total, "events_published": published, "docs_total": docs,
        "pending_review": pending_review, "followups_open": open_followups, "leads_new": leads_new,
        "avg_completeness": round(sum(scores) / len(scores), 1) if scores else None,
        "traceability": traceability_check(db, need_id, c),
        "tiles": list(c.ui.get("dashboard_tiles") or []),
    }


_SCORE_WEIGHTS = {"completeness": 0.4, "accuracy": 0.3, "consistency": 0.2, "timeliness": 0.1}


def quality_scorecard(db: Session, need_id: str, days: int | None = None, ctx=None) -> dict:
    """数据质量评分卡(借鉴 caijifagui 的 完整性/准确性/一致性/时效性 四维加权与 A–D 定级):
    - 完整性 = 覆盖维度有覆盖的比例 × 0.5 + 已发布记录完备度均分 × 0.5;
    - 准确性 = 可追溯校验通过(1/0)× 0.5 + 无守卫违规记录占比 × 0.5(粗筛存疑不计);
    - 一致性 = 关键角色列(主体/维度/日期)非空占比;
    - 时效性 = 披露→入库中位时延换算(≤1 天满分,≥30 天 0 分)。
    权重可由画像 outputs.quality_scorecard.weights 覆盖;等级阈值 A≥90 B≥75 C≥60。
    """
    from datetime import datetime as _dt
    from app.services import coverage as _cov
    c = _ctx(db, need_id, ctx)
    cfg = (c.outputs.get("quality_scorecard") or {})
    w = {**_SCORE_WEIGHTS, **{k: float(v) for k, v in (cfg.get("weights") or {}).items()}}
    live = _live(db, need_id)
    # 完整性
    cov = _cov.industry_coverage(db, need_id, days, c)
    covered = sum(1 for x in cov if x["level"] in ("有覆盖", "偏少")) / len(cov) if cov else 1.0
    comps = [e.completeness_score for e in live if e.completeness_score is not None]
    comp_avg = (sum(comps) / len(comps) / 100) if comps else 0.0
    from app.services import benchmark as _bm
    bm = _bm.latest(db, need_id)
    recall = bm["recall"] if bm and bm.get("recall") is not None else None
    if recall is not None:      # 有对标基准:漏报率是"找得全"最直接的度量,权重最高
        completeness = 100 * (0.3 * covered + 0.2 * comp_avg + 0.5 * recall)
    else:
        completeness = 100 * (0.5 * covered + 0.5 * comp_avg)
    # 准确性
    trace_ok = 1.0 if traceability_check(db, need_id, c)["ok"] else 0.0
    from app.services.money_guard import apply_guard
    clean = 0
    for e in live:
        try:
            clean += 1 if apply_guard(dict(e.payload or {}), "", ctx=c).clean else 0
        except Exception:  # noqa: BLE001
            clean += 1
    accuracy = 100 * (0.5 * trace_ok + 0.5 * (clean / len(live) if live else 1.0))
    # 一致性:主体/维度/日期列非空
    def _filled(e):
        cols = [ROLE_COLUMNS["subject"], ROLE_COLUMNS["dim1"], ROLE_COLUMNS["occurred_date"]]
        return sum(1 for col in cols if getattr(e, col, None)) / len(cols)
    consistency = 100 * (sum(_filled(e) for e in live) / len(live) if live else 1.0)
    # 时效性
    lags = []
    for e in live:
        if e.disclosed_date and e.created_at:
            lags.append(max(0, (e.created_at.date() - e.disclosed_date).days))
    if lags:
        lags.sort()
        med = lags[len(lags) // 2]
        timeliness = 100 * max(0.0, min(1.0, 1 - (med - 1) / 29)) if med > 1 else 100.0
    else:
        timeliness = 100.0
    # 验证状态分布(真实可信的直观信号)
    from app.models import RawDocument as _RD
    vdist: dict[str, int] = {}
    for (v,) in db.query(_RD.verification).filter(_RD.need_id == need_id, _RD.screen_status == "screened_in").all():
        st = (v or {}).get("status") or "unverified" if isinstance(v, dict) else "unverified"
        vdist[st] = vdist.get(st, 0) + 1
    dims = {"completeness": round(completeness, 1), "accuracy": round(accuracy, 1),
            "consistency": round(consistency, 1), "timeliness": round(timeliness, 1)}
    total = round(sum(dims[k] * w.get(k, 0) for k in dims) / (sum(w.get(k, 0) for k in dims) or 1), 1)
    grade = "A" if total >= 90 else "B" if total >= 75 else "C" if total >= 60 else "D"
    return {"need_id": need_id, "score": total, "grade": grade, "dimensions": dims, "weights": w,
            "labels": {"completeness": "完整性", "accuracy": "准确性", "consistency": "一致性", "timeliness": "时效性"},
            "records": len(live), "coverage_gaps": [x["industry"] for x in cov if x["gap"]][:20],
            "benchmark": bm, "verification": vdist,
            "median_lag_days": (lags[len(lags) // 2] if lags else None),
            "generated_at": _dt.utcnow().isoformat(timespec="seconds")}
