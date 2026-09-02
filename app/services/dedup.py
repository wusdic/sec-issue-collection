"""三层去重(方案第 10 节):URL 层 / 文档层(SimHash 同稿簇)/ 记录层(指纹+语义召回)。"""
from datetime import datetime, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import settings
from app.models import DocCluster, Event, RawDocument
from app.services import need_ctx, url_tools
from app.services.need_ctx import ROLE_COLUMNS
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
    # 只取比对必需的轻量列(不加载 content_text 大字段),命中后再按 id 取完整行:
    # 否则每处理一篇都要把近 N 天所有文档正文读进内存,量大时急剧变慢。
    rows = (
        db.query(RawDocument.id, RawDocument.simhash, RawDocument.title,
                 func.length(RawDocument.content_text))
        .filter(RawDocument.need_id == doc.need_id,
                RawDocument.fetched_at >= since,
                RawDocument.id != (doc.id or -1),
                RawDocument.simhash.isnot(None))
        .all()
    )
    my_title = _title_key(doc.title)
    for other_id, other_simhash, other_title, other_len in rows:
        if hamming(doc.simhash, other_simhash) <= settings.simhash_hamming_max:
            # 二次确认防模板化误命中:双方都有标题 → 必须标题一致;否则退回正文长度相近(比值>0.6)
            ot = _title_key(other_title)
            if my_title and ot:
                if my_title != ot:
                    continue
            else:
                ol = other_len or 0
                if not (ol > 0 and min(len(body), ol) / max(len(body), ol) > settings.dedup_len_ratio_min):
                    continue
            other = db.get(RawDocument, other_id)   # 命中才加载完整行
            if other is None:
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


def _as_list(v) -> list:
    if v in (None, "", {}):
        return []
    if isinstance(v, list):
        return [str(x) for x in v if x not in (None, "")]
    if isinstance(v, dict):
        return [str(x) for x in v.values() if x not in (None, "")]
    return [str(v)]


def _scalar(v) -> str:
    if isinstance(v, dict):
        v = v.get("value") or v.get("name") or v.get("level") or ""
    if isinstance(v, list):
        v = "、".join(str(x) for x in v if x)
    return str(v).strip() if v not in (None, "") else ""


def subject_key(payload: dict, ctx) -> str:
    """记录主体键:按画像 dedup.subject_roles 顺序取第一个能算出来的。

    单角色(如 subject_key=统一社会信用代码/文号)非空即用;复合角色 `a+b` 用 `|` 拼接,
    至少一个分量非空才成立。都算不出 → ""(视为无法指纹,不做记录级去重)。
    """
    blank = ctx.subject_blank_values
    for spec in ctx.dedup.get("subject_roles") or []:
        parts = [x.strip() for x in str(spec).split("+") if x.strip()]
        vals = [_scalar(ctx.get_role(payload, r)) for r in parts]
        vals = ["" if v in blank else v for v in vals]       # 『未披露/未知』不是主体,不能当键
        if not vals[0]:
            continue                                          # 首分量(主体本身)必须非空
        return vals[0] if len(parts) == 1 else "|".join(vals)
    return ""


def _org_key(payload: dict, ctx=None) -> str:
    """兼容旧名。"""
    return subject_key(payload, ctx or need_ctx.get(None, need_ctx.default_need_id()))


def fingerprint_match(db: Session, need_id: str, payload: dict, ctx=None) -> Event | None:
    """10.3 记录层第一步:主体键 + (可选)类型交集 + 时间窗 ±N 天。键/角色/窗口来自画像 record.dedup。"""
    c = ctx or need_ctx.get(db, need_id)
    d = c.dedup
    key = subject_key(payload, c)
    if not key:
        return None
    type_role = d.get("type_role")
    types = set(_as_list(c.get_role(payload, type_role))) if type_role else None
    date_role = d.get("date_role") or "occurred_date"
    # 容错解析:LLM 可能给纯字符串、月精度("2026-04")或嵌套对象,直接 fromisoformat 会抛异常
    # 导致整篇处理失败被丢进人工队列(实测已发生),故统一走 url_tools.to_date。
    dt = url_tools.to_date(c.get_role(payload, date_role))
    if dt is None:
        return None
    window = timedelta(days=c.dedup_window_days)
    col = getattr(Event, ROLE_COLUMNS.get(date_role, "occurred_date"))
    candidates = (
        db.query(Event)
        .filter(Event.need_id == need_id, col.isnot(None), col >= dt - window, col <= dt + window)
        .all()
    )
    for ev in candidates:
        if subject_key(ev.payload or {}, c) != key:
            continue
        if type_role:
            if not (types & set(_as_list(c.get_role(ev.payload or {}, type_role)))):
                continue
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
