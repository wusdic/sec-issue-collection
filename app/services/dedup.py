"""三层去重(方案第 10 节):URL 层 / 文档层(SimHash 同稿簇)/ 记录层(指纹+语义召回)。"""
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.config import settings
from app.models import DocCluster, Event, RawDocument
from app.services import url_tools
from app.services.llm import cosine
from app.services.simhash import hamming, simhash64


def find_existing_url(db: Session, url: str) -> RawDocument | None:
    """10.1 URL 层:归一化后精确查重;命中累计热度。"""
    normalized = url_tools.normalize_url(url)
    doc = db.query(RawDocument).filter_by(url_normalized=normalized).one_or_none()
    if doc:
        doc.seen_again += 1
    return doc


def _title_key(t: str | None) -> str:
    """标题归一(去空白/标点)用于同稿二次确认,防模板化页面 SimHash 误命中。"""
    import re
    return re.sub(r"[\s\W_]+", "", (t or "").lower())


def assign_cluster(db: Session, doc: RawDocument, lookback_days: int | None = None) -> DocCluster:
    """10.2 文档层:与近 N 天文档 SimHash 比对,近重复并入同稿簇,只有首发 is_primary。

    政务站页面模板化严重(导航/页脚雷同)会致 SimHash 误判同稿。故 SimHash 命中后再做一道
    确认:两篇都有标题时必须标题一致才认定同稿(不同标题的新闻即便版式相近也不并簇);缺标题
    时退回正文长度相近判断。避免把不同文章误并、误标"转载非首发"。
    """
    body = doc.content_text or ""
    doc.simhash = simhash64(body or doc.title or "")
    lookback_days = lookback_days if lookback_days is not None else settings.dedup_lookback_days
    since = datetime.utcnow() - timedelta(days=lookback_days)
    candidates = (
        db.query(RawDocument)
        .filter(RawDocument.need_id == doc.need_id,
                RawDocument.fetched_at >= since,
                RawDocument.id != (doc.id or -1),
                RawDocument.simhash.isnot(None))
        .all()
    )
    my_title = _title_key(doc.title)
    for other in candidates:
        if hamming(doc.simhash, other.simhash) <= settings.simhash_hamming_max:
            # 二次确认防模板化误命中:双方都有标题 → 必须标题一致;否则退回正文长度相近(比值>0.6)
            ot = _title_key(other.title)
            if my_title and ot:
                if my_title != ot:
                    continue
            else:
                ol = len(other.content_text or "")
                if not (ol > 0 and min(len(body), ol) / max(len(body), ol) > settings.dedup_len_ratio_min):
                    continue
            cluster = db.get(DocCluster, other.cluster_id) if other.cluster_id else None
            if cluster is None:
                cluster = DocCluster(primary_doc_id=other.id,
                                     first_published_at=other.published_at or other.fetched_at)
                db.add(cluster)
                db.flush()
                other.cluster_id = cluster.id
                other.is_primary = True
            cluster.member_count += 1
            doc.cluster_id = cluster.id
            # 首发判定:发布时间更早者为主
            mine = doc.published_at or doc.fetched_at
            theirs = cluster.first_published_at or datetime.utcnow()
            if mine and mine < theirs:
                # 当前文档更早 → 改任首发
                old_primary = db.get(RawDocument, cluster.primary_doc_id)
                if old_primary:
                    old_primary.is_primary = False
                cluster.primary_doc_id = doc.id
                cluster.first_published_at = mine
                doc.is_primary = True
            else:
                doc.is_primary = False
            return cluster
    cluster = DocCluster(primary_doc_id=doc.id,
                         first_published_at=doc.published_at or doc.fetched_at)
    db.add(cluster)
    db.flush()
    doc.cluster_id = cluster.id
    doc.is_primary = True
    return cluster


def _org_key(payload: dict) -> str:
    return payload.get("org_uscc") or (
        (payload.get("org_name") or "未披露") + "|" + ((payload.get("region") or {}).get("province") or "")
    )


def fingerprint_match(db: Session, need_id: str, payload: dict) -> Event | None:
    """10.3 记录层第一步:单位键 + 攻击类型交集 + 时间窗 ±N 天。"""
    org = _org_key(payload)
    occurred = ((payload.get("occurred_date") or {}).get("date"))
    attack = set(payload.get("attack_type") or [])
    if not occurred:
        return None
    from datetime import date as _date
    d = _date.fromisoformat(occurred)
    window = timedelta(days=settings.fingerprint_window_days)
    candidates = (
        db.query(Event)
        .filter(Event.need_id == need_id,
                Event.occurred_date.isnot(None),
                Event.occurred_date >= d - window,
                Event.occurred_date <= d + window)
        .all()
    )
    for ev in candidates:
        if _org_key(ev.payload) == org and (attack & set(ev.attack_types or [])):
            return ev
    return None


def semantic_recall(db: Session, need_id: str, embedding: list[float],
                    exclude_event_id: str | None = None) -> list[tuple[Event, float]]:
    """10.3 记录层第二步:embedding 余弦近邻兜底(生产换 pgvector 检索)。"""
    out = []
    for ev in db.query(Event).filter(Event.need_id == need_id, Event.embedding.isnot(None)).all():
        if exclude_event_id and ev.event_id == exclude_event_id:
            continue
        sim = cosine(embedding, ev.embedding)
        if sim >= settings.semantic_recall_threshold:
            out.append((ev, sim))
    return sorted(out, key=lambda t: -t[1])[:5]
