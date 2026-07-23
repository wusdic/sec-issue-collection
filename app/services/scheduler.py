"""调度(beat 可调用的纯函数):分级抓取、每日 B1 定题、回访派发、线索窗口刷新、候选评分。

时效 SLA(框架 9C):需求画像 timeliness_sla 反向决定源调度间隔上限。
"""
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.models import KeywordSet, NeedProfile, Source
from app.services import discovery, leads, pipeline
from app.services.followup import due_tasks

TIER_INTERVAL_HOURS = {"A": 3, "B": 24, "C": 24 * 7}
SLA_MAX_INTERVAL = {"告警级": 1, "小时级": 3, "日级": 24, "周级": 24 * 7}


def _interval_for(source: Source, need: NeedProfile) -> int:
    tier_h = TIER_INTERVAL_HOURS.get(source.tier, 24)
    sla = ((need.config.get("need") or {}).get("timeliness_sla")) or "日级"
    return min(tier_h, SLA_MAX_INTERVAL.get(sla, 24))


def due_sources(db: Session, need: NeedProfile) -> list[Source]:
    out = []
    now = datetime.utcnow()
    for src in db.query(Source).filter(Source.lifecycle.in_(["active", "trial"])).all():
        if need.id not in (src.serves_needs or []):
            continue
        if src.manual_assist:
            continue  # 半自动源不进自动调度
        if (src.adapter_config or {}).get("parent_site_id"):
            continue  # 自动发现的子栏目由父源统一采集,不独立调度
        interval = timedelta(hours=_interval_for(src, need))
        if src.last_success_at is None or now - src.last_success_at >= interval:
            out.append(src)
    return out


def expand_queries(keyword_content: dict) -> list[str]:
    """关键词矩阵展开(B1)。查询 = 事件词单独 + 事件×行业 + 后果×单位 交叉,去重后按
    query_budget_per_source_daily 截断(该值即每源每次查询条数上限,页面可配,无隐藏硬上限)。"""
    events = keyword_content.get("event_terms") or []
    industries = keyword_content.get("industry_terms") or []
    consequences = keyword_content.get("consequence_terms") or []
    orgs = keyword_content.get("org_terms") or []
    # 交叉组合的取词深度可配(cross_event/cross_industry/...),默认放大以覆盖更全
    ce = int(keyword_content.get("cross_event_terms", 12))
    ci = int(keyword_content.get("cross_industry_terms", 20))
    cc = int(keyword_content.get("cross_consequence_terms", 12))
    co = int(keyword_content.get("cross_org_terms", 5))
    queries = list(events)
    queries += [f"{i} {e}" for e in events[:ce] for i in industries[:ci]]
    queries += [f"{o} {c}" for c in consequences[:cc] for o in orgs[:co]]
    # 去重保序
    seen, uniq = set(), []
    for q in queries:
        if q not in seen:
            seen.add(q)
            uniq.append(q)
    budget = int(keyword_content.get("query_budget_per_source_daily", 200))
    return uniq[:budget] if budget > 0 else uniq


def run_daily(db: Session, need_id: str, do_archive: bool = True, limit_sources: int | None = None) -> dict:
    """每日主任务:到期源抓取(B1)+ 文档处理 + 候选源评分 + 线索窗口刷新。"""
    need = db.get(NeedProfile, need_id)
    ks = db.query(KeywordSet).filter_by(need_id=need_id, is_active=True).first()
    queries = expand_queries(ks.content) if ks else []
    max_pages = int((ks.content.get("max_pages_per_query", 3)) if ks else 3)

    stats = {"sources": 0, "runs": [], "processed": [], "candidates": [], "leads_refreshed": 0}
    srcs = due_sources(db, need)
    if limit_sources:
        srcs = srcs[:limit_sources]
    for src in srcs:
        run = pipeline.crawl_source(db, need, src, queries=queries, max_pages=max_pages,
                                    do_archive=do_archive)
        stats["sources"] += 1
        stats["runs"].append({"source": src.name, "status": run.status,
                              "found": run.urls_found, "new": run.urls_new})
    # 处理待粗筛文档
    from app.models import RawDocument
    for doc in db.query(RawDocument).filter_by(need_id=need_id, screen_status="pending").limit(200).all():
        stats["processed"].append(pipeline.process_document(db, need, doc))
    stats["candidates"] = discovery.evaluate_candidates(db, need_id)
    stats["leads_refreshed"] = leads.refresh_window_stages(db, need_id)
    stats["followups_due"] = len(due_tasks(db))
    db.commit()
    return stats
