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


def expand_queries(keyword_content: dict, ctx=None) -> list[str]:
    """关键词矩阵展开(B1)。实现在 keywords 能力模块(支持配方组合与旧版四组交叉)。"""
    from app.services import keywords
    return keywords.expand_queries(keyword_content, ctx)


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
