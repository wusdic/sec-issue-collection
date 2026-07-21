"""事件(记录)服务:创建/合并/发布校验/变更日志。

发布红线(方案 5/6/11,对应 schema.sql 触发器的应用层实现):
- confirmed 金额通道非空 ⇒ 必须存在 credibility ∈ 画像 confirm_allowed(默认 S1/S2)的来源;
- 发布时 strict schema 校验;
- B7/B9(root_cause/security_controls)必填(可为"未披露/不明")。
"""
from datetime import date, datetime

from sqlalchemy.orm import Session

from app.models import Event, EventChangeLog, EventSource, ReviewTask
from app.services.extraction import completeness_score, validate_payload
from app.services.llm import get_llm
from app.services.money_guard import confirmed_fields


class PublishError(ValueError):
    pass


def next_event_id(db: Session, prefix: str = "SEC") -> str:
    today = datetime.utcnow().strftime("%Y%m%d")
    like = f"{prefix}-{today}-%"
    last = (
        db.query(Event.event_id)
        .filter(Event.event_id.like(like))
        .order_by(Event.event_id.desc())
        .first()
    )
    seq = int(last[0].rsplit("-", 1)[1]) + 1 if last else 1
    return f"{prefix}-{today}-{seq:04d}"


def _sync_columns(ev: Event):
    """payload → 查询列(对应 PG 触发器同步)。"""
    p = ev.payload or {}
    od = (p.get("occurred_date") or {}).get("date")
    ev.occurred_date = date.fromisoformat(od) if od else None
    ev.disclosed_date = date.fromisoformat(p["disclosed_date"]) if p.get("disclosed_date") else None
    ev.industry_l1 = (p.get("industry") or {}).get("level1")
    ev.industry_l2 = (p.get("industry") or {}).get("level2")
    ev.province = (p.get("region") or {}).get("province")
    ev.city = (p.get("region") or {}).get("city")
    ev.org_name = p.get("org_name")
    ev.org_uscc = p.get("org_uscc")
    ev.org_type = p.get("org_type")
    ev.org_size = p.get("org_size")
    ev.severity = (p.get("severity") or {}).get("level")
    ev.attack_types = p.get("attack_type") or []
    ev.consequences = p.get("consequences") or []
    ev.confidence_overall = p.get("confidence_overall")
    ev.completeness_score = completeness_score(p)


def full_record(ev: Event) -> dict:
    """合并系统信封字段(event_id/status/review/change_log)与内容 payload,用于完整 schema 校验。"""
    rec = dict(ev.payload or {})
    rec["event_id"] = ev.event_id
    rec["status"] = ev.status
    rec.setdefault("confidence_overall", ev.confidence_overall or "单源待证")
    rec["completeness_score"] = ev.completeness_score or 0
    rec.setdefault("review", {
        "created_by": "system",
        "created_at": (ev.created_at or datetime.utcnow()).isoformat(),
    })
    return rec


def create_draft(db: Session, need_id: str, payload: dict, doc=None,
                 source_credibility: str = "S4", dict_version: str | None = None) -> Event:
    payload.setdefault("confidence_overall", {
        "S1": "已证实", "S2": "多源印证", "S3": "单源待证", "S4": "单源待证",
    }.get(source_credibility, "单源待证"))
    ev = Event(event_id=next_event_id(db), need_id=need_id, payload=payload,
               status="draft", dict_version=dict_version,
               confidence_overall=payload.get("confidence_overall"))
    # 来源数组兜底:抽取结果无 sources 时由采集文档生成
    if not payload.get("sources") and doc is not None:
        payload["sources"] = [{
            "ref_id": "SRC-001",
            "url_or_doc_number": doc.final_url or doc.url,
            "title": doc.title or "",
            "publisher": doc.publisher or "",
            "published_date": (doc.published_at or doc.fetched_at).strftime("%Y-%m-%d"),
            "credibility": source_credibility,
            "snapshot_id": doc.snapshot_id or "",
        }]
    summary = f"{payload.get('title','')} {payload.get('org_name','')} {' '.join(payload.get('attack_type') or [])}"
    try:
        ev.embedding = get_llm().embed(summary)
    except Exception:  # noqa: BLE001 embedding 服务不可用时降级:跳过第三层语义去重,不阻断入库
        ev.embedding = None
    _sync_columns(ev)
    db.add(ev)
    db.flush()
    if doc is not None:
        db.add(EventSource(event_id=ev.event_id, ref_id="SRC-001", doc_id=doc.id,
                           snapshot_id=doc.snapshot_id, credibility=source_credibility,
                           supports_fields=["*"]))
    needs_double = bool(confirmed_fields(payload))
    db.add(ReviewTask(event_id=ev.event_id, stage="extracted", needs_double=needs_double))
    db.flush()
    return ev


def log_change(db: Session, event_id: str, field: str, old, new,
               by_user: int | None = None, source_ref: str | None = None):
    db.add(EventChangeLog(event_id=event_id, field=field, old_value=old, new_value=new,
                          by_user=by_user, source_ref=source_ref))


def update_payload(db: Session, ev: Event, new_payload: dict, by_user: int | None = None,
                   source_ref: str | None = None):
    old = ev.payload or {}
    for key in set(list(old.keys()) + list(new_payload.keys())):
        if old.get(key) != new_payload.get(key):
            log_change(db, ev.event_id, key, old.get(key), new_payload.get(key), by_user, source_ref)
    ev.payload = new_payload
    _sync_columns(ev)
    db.flush()


def merge_events(db: Session, primary: Event, duplicate: Event, by_user: int | None = None):
    """合并:来源全保留,字段按可信度择优(简化:主记录优先,主记录缺失取副本),写变更日志。"""
    merged = dict(primary.payload or {})
    dup_payload = duplicate.payload or {}
    for k, v in dup_payload.items():
        if merged.get(k) in (None, "", [], {}) and v not in (None, "", [], {}):
            merged[k] = v
            log_change(db, primary.event_id, k, None, v, by_user, source_ref=f"merge:{duplicate.event_id}")
    # 来源合并
    for es in db.query(EventSource).filter_by(event_id=duplicate.event_id).all():
        exists = db.get(EventSource, (primary.event_id, es.ref_id))
        ref = es.ref_id if not exists else f"{es.ref_id}-M{duplicate.event_id[-4:]}"
        db.add(EventSource(event_id=primary.event_id, ref_id=ref, doc_id=es.doc_id,
                           snapshot_id=es.snapshot_id, credibility=es.credibility,
                           supports_fields=es.supports_fields))
    src_list = list(merged.get("sources") or []) + list(dup_payload.get("sources") or [])
    merged["sources"] = src_list
    related = set(merged.get("related_event_ids") or [])
    related.add(duplicate.event_id)
    merged["related_event_ids"] = sorted(related)
    update_payload(db, primary, merged, by_user)
    duplicate.status = "closed"
    log_change(db, duplicate.event_id, "status", "draft", "closed(merged)", by_user)
    db.flush()
    return primary


def validate_publish(db: Session, ev: Event, record_schema: dict,
                     confirm_allowed: list[str] | None = None) -> list[str]:
    """发布校验:返回错误列表(空=可发布)。"""
    confirm_allowed = confirm_allowed or ["S1", "S2"]
    errors = validate_payload(full_record(ev), record_schema, strict=True)
    conf = confirmed_fields(ev.payload)
    if conf:
        creds = {es.credibility for es in db.query(EventSource).filter_by(event_id=ev.event_id).all()}
        # payload 内 sources 的可信度也纳入
        creds |= {s.get("credibility") for s in (ev.payload.get("sources") or [])}
        if not (creds & set(confirm_allowed)):
            errors.append(
                f"红线:{','.join(conf)} 存在已确认金额,但无 {'/'.join(confirm_allowed)} 权威来源支撑,拒绝发布"
            )
        # pending_human 未清除 = 复核未确认
        for f in conf:
            if (ev.payload.get(f) or {}).get("pending_human"):
                errors.append(f"红线:{f} 的 confirmed 金额未经人工确认(pending_human)")
    return errors


def publish(db: Session, ev: Event, record_schema: dict,
            confirm_allowed: list[str] | None = None, by_user: int | None = None) -> Event:
    errors = validate_publish(db, ev, record_schema, confirm_allowed)
    if errors:
        raise PublishError("; ".join(errors))
    old_status = ev.status
    ev.status = "published"
    ev.first_published_at = ev.first_published_at or datetime.utcnow()
    _sync_columns(ev)
    log_change(db, ev.event_id, "status", old_status, "published", by_user)
    db.flush()
    return ev
