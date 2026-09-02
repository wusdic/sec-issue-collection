"""记录服务(通用平台):创建/合并/发布校验/变更日志。所有行业含义来自 NeedContext。

发布红线(schema.sql 触发器的应用层实现,与领域无关):
- 三态字段存在 confirmed 断言 ⇒ 必须存在 credibility ∈ 画像 confirm_allowed 的来源;
- 发布时 strict schema 校验;
- 模型产出的 confirmed 未经人工确认(pending_human)不得发布。
物理查询列沿用旧名(industry_l1/org_name/severity/attack_types...),语义由画像 field_roles 决定,
见 need_ctx.ROLE_COLUMNS。
"""
from datetime import datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, object_session

from app.models import Event, EventChangeLog, EventSource, ReviewTask
from app.services import need_ctx, url_tools
from app.services.extraction import completeness_score, validate_payload
from app.services.llm import get_llm
from app.services.money_guard import confirmed_fields
from app.services.need_ctx import ROLE_COLUMN_LIMITS as _COL_LIMITS
from app.services.need_ctx import ROLE_COLUMNS


class PublishError(ValueError):
    pass


def _ctx(ctx=None, db=None, need_id: str | None = None):
    return ctx or need_ctx.get(db, need_id or need_ctx.default_need_id())


def _ctx_for_event(ev: Event, ctx=None):
    if ctx is not None:
        return ctx
    return need_ctx.get(object_session(ev), ev.need_id or need_ctx.default_need_id())


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


def _to_date(v):
    """容错日期解析(统一走 url_tools.to_date:支持 str/{date|value|raw}/月精度/带时间,失败返回 None)。"""
    return url_tools.to_date(v)


def _scalar_severity(v) -> str | None:
    if isinstance(v, dict):
        v = v.get("level") or v.get("value")
    return str(v)[:8] if v else None


def _scalar(v, limit: int = 256) -> str | None:
    """任意形态 → 标量字符串(dict 取 value/name/level/text;list 顿号拼接)。"""
    if v in (None, "", [], {}):
        return None
    if isinstance(v, dict):
        for k in ("value", "name", "level", "text", "label"):
            if v.get(k) not in (None, ""):
                return str(v[k])[:limit]
        return None
    if isinstance(v, list):
        return "、".join(str(x) for x in v if x not in (None, ""))[:limit] or None
    return str(v)[:limit]


def _as_list(v) -> list:
    if v in (None, "", {}):
        return []
    if isinstance(v, list):
        return [x for x in v if x not in (None, "")]
    if isinstance(v, dict):
        return [str(x) for x in v.values() if x not in (None, "")]
    return [v]


def _infer_record_type(p: dict, ctx=None) -> str:
    """记录类型:显式且合法直接用;字符串模糊匹配;否则按『无单一主体 + 汇总特征』推断为汇总型。"""
    c = _ctx(ctx)
    rt_cfg = c.record_types
    values = [str(x) for x in (rt_cfg.get("values") or [])]
    default = str(rt_cfg.get("default") or (values[0] if values else "单一记录"))
    oos = rt_cfg.get("out_of_scope")
    adv = rt_cfg.get("advisory") or {}
    adv_value = adv.get("value")
    hints = [str(h) for h in (adv.get("hints") or [])]
    blank = {str(x) for x in (adv.get("subject_blank_values") or [""])}
    rt = p.get("record_type") if isinstance(p, dict) else None
    if rt in values:
        return rt
    if isinstance(rt, str) and rt:
        if oos and (oos in rt or rt in oos):
            return oos
        if adv_value and (adv_value in rt or rt in adv_value or any(h in rt for h in hints)):
            return adv_value
        return default
    if not adv_value:
        return default
    subject = str(c.get_role(p, "subject") or "").strip()
    blob = " ".join([str(c.get_role(p, "title") or p.get("title") or "")]
                    + [str(x) for x in _as_list(c.get_role(p, "tags_b"))])
    if subject in blank and any(h in blob for h in hints):
        return adv_value
    return default


def _sync_columns(ev: Event, ctx=None):
    """payload → 查询列(角色驱动;对应 PG 触发器同步)。容错:异常形态不抛,尽量落到标量列。"""
    c = _ctx_for_event(ev, ctx)
    p = ev.payload or {}
    for role, col in ROLE_COLUMNS.items():
        v = c.get_role(p, role)
        if role in ("occurred_date", "disclosed_date"):
            v = _to_date(v)
        elif role in ("tags_a", "tags_b"):
            v = [str(x) if not isinstance(x, (dict, list)) else _scalar(x) for x in _as_list(v)]
            v = [x for x in v if x]
        elif role == "record_type":
            v = _infer_record_type(p, c)[: _COL_LIMITS[col]]
        elif role == "grade":
            v = _scalar_severity(v)
        else:
            v = _scalar(v, _COL_LIMITS.get(col, 256))
        setattr(ev, col, v)
    ev.confidence_overall = p.get("confidence_overall")
    ev.completeness_score = completeness_score(p, ctx=c)


def full_record(ev: Event, ctx=None, record_schema: dict | None = None) -> dict:
    """合并系统信封字段(记录号/系统状态/复核信息)与内容 payload,用于完整 schema 校验。

    信封键名由画像 record.envelope 决定(文档型画像的 status 是业务状态,可声明不注入);
    给了 Schema 时只注入 Schema 里声明过的键,避免 additionalProperties=false 的 Schema 被撑爆。
    """
    c = _ctx_for_event(ev, ctx)
    env = c.envelope
    props = set((record_schema or {}).get("properties") or {}) if record_schema else None

    def _want(key: str) -> bool:
        return bool(key) and (props is None or key in props)

    rec = dict(ev.payload or {})
    if _want(env["id_field"]):
        rec[env["id_field"]] = ev.event_id
    if _want(env["status_field"]):
        rec[env["status_field"]] = ev.status
    if _want("confidence_overall"):
        rec.setdefault("confidence_overall", ev.confidence_overall or "单源待证")
    if _want("completeness_score"):
        rec["completeness_score"] = ev.completeness_score or 0
    if _want(env["review_field"]):
        rec.setdefault(env["review_field"], {
            "created_by": "system",
            "created_at": (ev.created_at or datetime.utcnow()).isoformat(),
        })
    return rec


def _embed_summary(c, payload: dict) -> str:
    parts = [str(payload.get("title") or c.get_role(payload, "title") or ""),
             str(c.get_role(payload, "subject") or "")]
    parts += [str(x) for x in _as_list(c.get_role(payload, "tags_a"))]
    return " ".join(x for x in parts if x)


def create_draft(db: Session, need_id: str, payload: dict, doc=None,
                 source_credibility: str = "S4", dict_version: str | None = None, ctx=None) -> Event:
    c = ctx or need_ctx.get(db, need_id)
    payload.setdefault("confidence_overall",
                       c.confidence_by_credibility.get(source_credibility, "单源待证"))
    # 来源 url 反幻觉:抽取给的占位/非法链接用采集文档真实链接回填(禁止 c_XXXXX 之类进库)
    if payload.get("sources") and doc is not None:
        real = doc.final_url or doc.url
        for s in payload["sources"]:
            if isinstance(s, dict):
                u = str(s.get("url_or_doc_number") or s.get("url") or "")
                if (not u.startswith("http")) or "XXXX" in u.upper() or "占位" in u:
                    s["url_or_doc_number"] = real
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
    summary = _embed_summary(c, payload)
    try:
        # 先算 embedding(慢:网络调用),再取号入库。否则取号与插入之间夹着这次网络请求,
        # 并行下多个 worker 必然算出同一个记录号 → 主键冲突 → 已抽取结果被丢弃。
        embedding = get_llm().embed(summary)
    except Exception:  # noqa: BLE001 embedding 服务不可用时降级:跳过第三层语义去重,不阻断入库
        embedding = None

    # 记录号按"当日最大序号+1"生成,并发下会撞主键;用 savepoint 重试重新取号,保证不丢记录。
    ev, last_err = None, None
    for _ in range(10):
        cand = Event(event_id=next_event_id(db, c.id_prefix), need_id=need_id, payload=payload,
                     status="draft", dict_version=dict_version,
                     confidence_overall=payload.get("confidence_overall"),
                     embedding=embedding)
        _sync_columns(cand, c)
        try:
            with db.begin_nested():
                db.add(cand)
                db.flush()
            ev = cand
            break
        except IntegrityError as e:  # 记录号被其他 worker 抢先占用 → 重新取号
            last_err = e
            try:
                db.expunge(cand)
            except Exception:  # noqa: BLE001
                pass
    if ev is None:
        raise last_err
    if doc is not None:
        db.add(EventSource(event_id=ev.event_id, ref_id="SRC-001", doc_id=doc.id,
                           snapshot_id=doc.snapshot_id, credibility=source_credibility,
                           supports_fields=["*"]))
    conf_fields = confirmed_fields(payload, ctx=c)
    needs_double = bool(conf_fields)
    db.add(ReviewTask(event_id=ev.event_id, stage="extracted", needs_double=needs_double))
    if needs_double:
        # 含 confirmed 断言 = 碰发布红线,必须显眼:发布前需 confirm_allowed 等级来源支撑且双人复核
        from app.services import actions
        actions.record(db, "event.money_confirmed",
                       f"生成含『已确认』断言的记录 {ev.event_id}:{payload.get('title', '')}",
                       need_id=need_id, target=ev.event_id,
                       detail={"event_id": ev.event_id, "fields": conf_fields,
                               "source_credibility": source_credibility})
    db.flush()
    return ev


def log_change(db: Session, event_id: str, field: str, old, new,
               by_user: int | None = None, source_ref: str | None = None):
    db.add(EventChangeLog(event_id=event_id, field=field, old_value=old, new_value=new,
                          by_user=by_user, source_ref=source_ref))


def update_payload(db: Session, ev: Event, new_payload: dict, by_user: int | None = None,
                   source_ref: str | None = None, ctx=None):
    old = ev.payload or {}
    for key in set(list(old.keys()) + list(new_payload.keys())):
        if old.get(key) != new_payload.get(key):
            log_change(db, ev.event_id, key, old.get(key), new_payload.get(key), by_user, source_ref)
    ev.payload = new_payload
    _sync_columns(ev, ctx)
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
                     confirm_allowed: list[str] | None = None, ctx=None) -> list[str]:
    """发布校验:返回错误列表(空=可发布)。"""
    c = ctx or need_ctx.get(db, ev.need_id or need_ctx.default_need_id())
    confirm_allowed = confirm_allowed or c.confirm_allowed
    errors = validate_payload(full_record(ev, c, record_schema), record_schema, strict=True, ctx=c)
    conf = confirmed_fields(ev.payload, ctx=c)
    if conf:
        creds = {es.credibility for es in db.query(EventSource).filter_by(event_id=ev.event_id).all()}
        # payload 内 sources 的可信度也纳入
        creds |= {s.get("credibility") for s in (ev.payload.get("sources") or []) if isinstance(s, dict)}
        if not (creds & set(confirm_allowed)):
            errors.append(
                f"红线:{','.join(conf)} 存在已确认断言,但无 {'/'.join(confirm_allowed)} 权威来源支撑,拒绝发布"
            )
        # pending_human 未清除 = 复核未确认
        for f in conf:
            if url_tools.dget(ev.payload, f, "pending_human"):
                errors.append(f"红线:{f} 的 confirmed 断言未经人工确认(pending_human)")
    return errors


def publish(db: Session, ev: Event, record_schema: dict,
            confirm_allowed: list[str] | None = None, by_user: int | None = None, ctx=None) -> Event:
    errors = validate_publish(db, ev, record_schema, confirm_allowed, ctx=ctx)
    if errors:
        raise PublishError("; ".join(errors))
    old_status = ev.status
    ev.status = "published"
    ev.first_published_at = ev.first_published_at or datetime.utcnow()
    _sync_columns(ev, ctx)
    log_change(db, ev.event_id, "status", old_status, "published", by_user)
    db.flush()
    return ev
