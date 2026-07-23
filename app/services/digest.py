"""每日简报:把某需求某天的产出汇总成结构化 + Markdown,供页面查看、下载、推送。

内容面向业务决策:今日新增事件(按行业/严重度)、新增销售线索(按窗口期)、行业热点、
源健康。只统计当天(UTC 日)created_at 落在该天的记录,幂等 upsert 到 daily_digest。
"""
from datetime import date, datetime, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import CrawlJob, DailyDigest, Event, Lead, Source


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime(day.year, day.month, day.day)
    return start, start + timedelta(days=1)


def build_content(db: Session, need_id: str, day: date) -> dict:
    start, end = _day_bounds(day)

    ev_q = db.query(Event).filter(Event.need_id == need_id,
                                  Event.created_at >= start, Event.created_at < end)
    all_records = ev_q.all()
    # 单一事件 与 通报情报 分开统计(后者是近期重点情报方向,单独归类)
    events = [e for e in all_records if (e.record_type or "单一事件") != "通报情报"]
    advisories = [e for e in all_records if (e.record_type or "单一事件") == "通报情报"]
    by_industry: dict[str, int] = {}
    by_severity: dict[str, int] = {}
    top_events = []
    for e in events:
        by_industry[e.industry_l1 or "未分类"] = by_industry.get(e.industry_l1 or "未分类", 0) + 1
        by_severity[e.severity or "未定级"] = by_severity.get(e.severity or "未定级", 0) + 1
    # 重点事件:严重度高 + 完整度高优先
    sev_rank = {"L6": 6, "L5": 5, "L4": 4, "L3": 3, "L2": 2, "L1": 1}
    for e in sorted(events, key=lambda x: (sev_rank.get(x.severity or "", 0),
                                           x.completeness_score or 0), reverse=True)[:10]:
        top_events.append({
            "event_id": e.event_id, "org": e.org_name, "industry": e.industry_l1,
            "severity": e.severity, "attack_types": e.attack_types or [],
            "consequences": e.consequences or [], "status": e.status,
            "confidence": e.confidence_overall,
        })

    leads = db.query(Lead).filter(Lead.need_id == need_id,
                                  Lead.updated_at >= start, Lead.updated_at < end).all()
    by_stage: dict[str, int] = {}
    top_leads = []
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

    top_advisories = [{"event_id": a.event_id, "title": (a.payload or {}).get("title") or a.org_name,
                       "org": a.org_name, "consequences": a.consequences or [],
                       "occurred": a.occurred_date.isoformat() if a.occurred_date else None}
                      for a in sorted(advisories, key=lambda x: x.created_at, reverse=True)[:10]]

    return {
        "need_id": need_id, "day": day.isoformat(),
        "events_total": len(events), "events_by_industry": by_industry,
        "events_by_severity": by_severity, "top_events": top_events,
        "advisories_total": len(advisories), "top_advisories": top_advisories,
        "leads_total": len(leads), "leads_by_stage": by_stage, "top_leads": top_leads,
        "hot_industries": sorted(by_industry.items(), key=lambda x: -x[1])[:5],
        "sources": {"healthy": healthy, "failing": failing, "retired": retired,
                    "serving": len(serving)},
        "crawl": {"jobs": len(jobs), "new_docs": new_docs},
        "generated_at": datetime.utcnow().isoformat(timespec="seconds"),
    }


def render_markdown(c: dict) -> str:
    L = [f"# 安全事件日报 · {c['day']}", ""]
    L.append(f"**今日新增事件 {c['events_total']} 条 ｜ 通报情报 {c.get('advisories_total', 0)} 条 ｜ "
             f"新增/更新线索 {c['leads_total']} 条 ｜ 新入库文档 {c['crawl']['new_docs']} 篇**")
    L.append("")
    if c["hot_industries"]:
        L.append("## 行业热点(按新增事件数)")
        for name, n in c["hot_industries"]:
            L.append(f"- {name}:{n} 条")
        L.append("")
    if c["events_by_severity"]:
        L.append("## 严重度分布")
        L.append("　".join(f"{k} {v}" for k, v in sorted(c["events_by_severity"].items(), reverse=True)))
        L.append("")
    if c["top_events"]:
        L.append("## 重点事件")
        for e in c["top_events"]:
            atk = "、".join(e["attack_types"]) or "—"
            L.append(f"- **{e['org'] or '(未知单位)'}**（{e['industry'] or '未分类'}／"
                     f"{e['severity'] or '未定级'}）{atk} ｜ {e['event_id']} [{e['status']}]")
        L.append("")
    if c.get("top_advisories"):
        L.append("## 通报情报(近期重点方向)")
        for a in c["top_advisories"]:
            conseq = "、".join(a["consequences"][:4]) or "—"
            L.append(f"- **{a['title'] or '(无题)'}**（{a['occurred'] or '时间未披露'}）{conseq} ｜ {a['event_id']}")
        L.append("")
    if c["top_leads"]:
        L.append("## 销售线索(评分 Top)")
        for ld in c["top_leads"]:
            prod = "、".join(ld["products"]) or "—"
            L.append(f"- **{ld['org']}**（{ld['stage']}／{ld['kind']}，评分 {ld['score']}）"
                     f"建议产品:{prod}")
        L.append("")
    s = c["sources"]
    L.append("## 源健康")
    L.append(f"服务本需求 {s['serving']} 个:健康 {s['healthy']}、异常 {s['failing']}、停用 {s['retired']}")
    L.append("")
    L.append(f"_生成时间 {c['generated_at']} UTC_")
    return "\n".join(L)


def upsert(db: Session, need_id: str, day: date) -> DailyDigest:
    content = build_content(db, need_id, day)
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
        ok, _msg = deliver_email(f"安全事件日报 {d.day}", d.markdown or "")
        d.delivered = bool(ok)
    except Exception:  # noqa: BLE001
        pass
    db.commit()
    return d


def latest(db: Session, need_id: str) -> DailyDigest | None:
    return (db.query(DailyDigest).filter_by(need_id=need_id)
            .order_by(DailyDigest.day.desc()).first())
