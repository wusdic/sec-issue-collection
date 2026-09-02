"""每日简报(通用平台):把某需求某天的产出汇总成结构化 + Markdown,供页面查看、下载、推送。

分组维度/排序角色/标题/是否拆分汇总型记录,来自画像 outputs.digest;文案里的名词
(记录叫什么、分类叫什么)来自画像角色标签。只统计当天(UTC 日)created_at 落在该天的记录,
幂等 upsert 到 daily_digest。
"""
from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from app.models import CrawlJob, DailyDigest, Event, Lead, Source
from app.services import need_ctx
from app.services.need_ctx import ROLE_COLUMNS


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime(day.year, day.month, day.day)
    return start, start + timedelta(days=1)


def _col(e: Event, role: str):
    col = ROLE_COLUMNS.get(role)
    return getattr(e, col, None) if col else None


def build_content(db: Session, need_id: str, day: date, ctx=None) -> dict:
    c = ctx or need_ctx.get(db, need_id)
    dg = c.digest
    group_roles = [r for r in (dg.get("group_roles") or ["dim1", "grade"]) if r in ROLE_COLUMNS] or ["dim1"]
    rank_role = dg.get("rank_role") or "grade"
    adv_value = (c.record_types.get("advisory") or {}).get("value") if dg.get("advisory_split", True) else None
    start, end = _day_bounds(day)

    all_records = db.query(Event).filter(Event.need_id == need_id,
                                         Event.created_at >= start, Event.created_at < end).all()
    # 单一记录 与 汇总型(通报/情报/盘点)分开统计:后者无单一主体,单独归类
    events = [e for e in all_records if not adv_value or (e.record_type or "") != adv_value]
    advisories = [e for e in all_records if adv_value and (e.record_type or "") == adv_value]

    groups: dict[str, dict[str, int]] = {}
    for role in group_roles:
        cnt: dict[str, int] = {}
        for e in events:
            v = _col(e, role)
            for x in (v if isinstance(v, list) else [v]):
                k = str(x) if x else "未分类"
                cnt[k] = cnt.get(k, 0) + 1
        groups[role] = cnt

    def _rank(e: Event) -> tuple:
        if rank_role == "grade" or rank_role in ROLE_COLUMNS and c.grade_order:
            return (c.grade_rank(_col(e, rank_role)), e.completeness_score or 0)
        return (0, e.completeness_score or 0)

    top_events = []
    for e in sorted(events, key=_rank, reverse=True)[:10]:
        top_events.append({
            "event_id": e.event_id, "title": (e.payload or {}).get("title"),
            "subject": _col(e, "subject"), "dim1": _col(e, "dim1"), "grade": _col(e, "grade"),
            "tags_a": _col(e, "tags_a") or [], "tags_b": _col(e, "tags_b") or [],
            "status": e.status, "confidence": e.confidence_overall,
        })

    leads, by_stage, top_leads = [], {}, []
    if c.leads.get("enabled"):
        leads = db.query(Lead).filter(Lead.need_id == need_id,
                                      Lead.updated_at >= start, Lead.updated_at < end).all()
        for ld in leads:
            by_stage[ld.window_stage or "未知"] = by_stage.get(ld.window_stage or "未知", 0) + 1
        for ld in sorted(leads, key=lambda x: x.score, reverse=True)[:10]:
            top_leads.append({"org": ld.target_org, "kind": ld.target_kind,
                              "score": round(ld.score, 2), "stage": ld.window_stage,
                              "products": ld.products or [], "event_id": ld.event_id})

    # 源健康:当天成功过的活跃源 vs 连败源
    active_srcs = db.query(Source).filter(Source.serves_needs.isnot(None)).all()
    serving = [s for s in active_srcs if need_id in (s.serves_needs or [])]
    healthy = sum(1 for s in serving if s.lifecycle in ("active", "trial") and (s.fail_streak or 0) == 0)
    failing = sum(1 for s in serving if (s.fail_streak or 0) >= 1 and s.lifecycle != "retired")
    retired = sum(1 for s in serving if s.lifecycle == "retired")

    jobs = db.query(CrawlJob).filter(CrawlJob.need_id == need_id,
                                     CrawlJob.started_at >= start, CrawlJob.started_at < end).all()
    new_docs = sum(j.new_docs for j in jobs)

    top_advisories = [{"event_id": a.event_id, "title": (a.payload or {}).get("title") or _col(a, "subject"),
                       "subject": _col(a, "subject"), "tags_b": _col(a, "tags_b") or [],
                       "occurred": a.occurred_date.isoformat() if a.occurred_date else None}
                      for a in sorted(advisories, key=lambda x: x.created_at, reverse=True)[:10]]

    g0 = group_roles[0]
    labels = {"record": c.ui.get("record_label") or "记录", "advisory": adv_value or "",
              "group_roles": group_roles, **{r: c.role_label(r) for r in ROLE_COLUMNS},
              "leads_enabled": bool(c.leads.get("enabled"))}
    return {
        "need_id": need_id, "day": day.isoformat(), "title": dg.get("title"), "labels": labels,
        "events_total": len(events), "by_group": groups, "top_events": top_events,
        "advisories_total": len(advisories), "top_advisories": top_advisories,
        "leads_total": len(leads), "leads_by_stage": by_stage, "top_leads": top_leads,
        "hot_groups": sorted(groups.get(g0, {}).items(), key=lambda x: -x[1])[:5],
        "sources": {"healthy": healthy, "failing": failing, "retired": retired,
                    "serving": len(serving)},
        "crawl": {"jobs": len(jobs), "new_docs": new_docs},
        "generated_at": datetime.utcnow().isoformat(timespec="seconds"),
    }


def render_markdown(c: dict) -> str:
    lb = c.get("labels") or {}
    rec = lb.get("record") or "记录"
    groups = lb.get("group_roles") or ["dim1", "grade"]
    g0 = groups[0]
    g0_label, g1_label = lb.get(g0, "分类"), (lb.get(groups[1], "等级") if len(groups) > 1 else "")
    title = c.get("title") or f"{rec}日报"
    L = [f"# {title} · {c['day']}", ""]
    head = f"**今日新增{rec} {c['events_total']} 条"
    if lb.get("advisory"):
        head += f" ｜ {lb['advisory']} {c.get('advisories_total', 0)} 条"
    if lb.get("leads_enabled"):
        head += f" ｜ 新增/更新线索 {c['leads_total']} 条"
    head += f" ｜ 新入库文档 {c['crawl']['new_docs']} 篇**"
    L += [head, ""]
    if c.get("hot_groups"):
        L.append(f"## {g0_label}热点(按新增{rec}数)")
        for name, n in c["hot_groups"]:
            L.append(f"- {name}:{n} 条")
        L.append("")
    g1 = (c.get("by_group") or {}).get(groups[1]) if len(groups) > 1 else None
    if g1 and g1_label:
        L.append(f"## {g1_label}分布")
        L.append("　".join(f"{k} {v}" for k, v in sorted(g1.items(), reverse=True)))
        L.append("")
    if c["top_events"]:
        L.append(f"## 重点{rec}")
        for e in c["top_events"]:
            tags = "、".join(e.get("tags_a") or []) or "—"
            L.append(f"- **{e.get('subject') or e.get('title') or '(未知主体)'}**({e.get('dim1') or '未分类'}／"
                     f"{e.get('grade') or '未定级'}){tags} ｜ {e['event_id']} [{e['status']}]")
        L.append("")
    if c.get("top_advisories"):
        L.append(f"## {lb.get('advisory') or '汇总情报'}(近期重点方向)")
        for a in c["top_advisories"]:
            conseq = "、".join((a.get("tags_b") or [])[:4]) or "—"
            L.append(f"- **{a['title'] or '(无题)'}**({a['occurred'] or '时间未披露'}){conseq} ｜ {a['event_id']}")
        L.append("")
    if c["top_leads"]:
        L.append("## 销售线索(评分 Top)")
        for ld in c["top_leads"]:
            prod = "、".join(ld["products"]) or "—"
            L.append(f"- **{ld['org']}**({ld['stage']}／{ld['kind']},评分 {ld['score']})"
                     f"建议产品:{prod}")
        L.append("")
    s = c["sources"]
    L.append("## 源健康")
    L.append(f"服务本需求 {s['serving']} 个:健康 {s['healthy']}、异常 {s['failing']}、停用 {s['retired']}")
    L.append("")
    L.append(f"_生成时间 {c['generated_at']} UTC_")
    return "\n".join(L)


def upsert(db: Session, need_id: str, day: date, ctx=None) -> DailyDigest:
    content = build_content(db, need_id, day, ctx)
    md = render_markdown(content)
    row = db.query(DailyDigest).filter_by(need_id=need_id, day=day).one_or_none()
    if row:
        row.content = content
        row.markdown = md
    else:
        row = DailyDigest(need_id=need_id, day=day, content=content, markdown=md)
        db.add(row)
    db.flush()
    return row


def generate_today(db: Session, need_id: str) -> DailyDigest:
    d = upsert(db, need_id, datetime.utcnow().date())
    # 可选邮件推送(未配置 SMTP 则跳过,不影响页面查看/下载)
    try:
        from app.services.daily import deliver_email
        title = (d.content or {}).get("title") or "日报"
        ok, _msg = deliver_email(f"{title} {d.day}", d.markdown or "")
        d.delivered = bool(ok)
    except Exception:  # noqa: BLE001
        pass
    db.commit()
    return d


def latest(db: Session, need_id: str) -> DailyDigest | None:
    return (db.query(DailyDigest).filter_by(need_id=need_id)
            .order_by(DailyDigest.day.desc()).first())
