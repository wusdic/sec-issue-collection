"""覆盖度盘点(通用平台):按画像声明的覆盖维度看近 N 天有没有记录,空白就是"该去找源的方向"。

维度角色(coverage.dimension_role,默认 dim1)、维度取值(词表文件里 coverage.dictionary_key 那一节)、
找源词模板(query_templates,{ind} 占位)、短名映射、占位桶,全部来自画像;窗口/门槛缺省用运行时设置。
"""
from datetime import datetime, timedelta

from app.models import Event, RawDocument, Source
from app.services import need_ctx
from app.services.need_ctx import ROLE_COLUMNS


def _ctx(db, need_id, ctx=None):
    return ctx or need_ctx.get(db, need_id)


def dimension_values(ctx) -> dict[str, list[str]]:
    """覆盖维度的取值 → 子项列表。词表里可写成 {一级: [二级...]} 或 [值...] 或 [{name: 值}]。"""
    data = ctx.load_dictionaries_file()
    v = data.get(ctx.coverage.get("dictionary_key") or "industries")
    if not v:
        # 没有词表文件/该节为空 → 用范围限定(scope)里与覆盖维度对应的那一维当取值
        from app.services.need_ctx import SCOPE_KINDS
        kinds = [ctx.coverage.get("scope_kind")] if ctx.coverage.get("scope_kind") else \
            ["industries", "topics", "doc_types", "regions", "entities"]
        for k in kinds:
            if k in SCOPE_KINDS and ctx.scope_values(k):
                v = ctx.scope_values(k)
                break
    if isinstance(v, dict):
        return {str(k): [str(x) for x in vv] if isinstance(vv, list) else [] for k, vv in v.items()}
    if isinstance(v, list):
        out = {}
        for x in v:
            if isinstance(x, dict):
                name = x.get("name") or x.get("value") or x.get("label")
                if name:
                    out[str(name)] = [str(s) for s in (x.get("sub") or x.get("children") or [])]
            elif x not in (None, ""):
                out[str(x)] = []
        return out
    return {}


def _industries() -> dict[str, list[str]]:
    """兼容旧名:默认需求的覆盖维度取值。"""
    return dimension_values(need_ctx.get(None, need_ctx.default_need_id()))


def industry_coverage(db, need_id: str, days: int | None = None, ctx=None) -> list[dict]:
    """覆盖维度每个取值近 N 天的记录数 / 覆盖等级,按"最缺"排前面。键名 industry 沿用(=维度值)。"""
    c = _ctx(db, need_id, ctx)
    days = int(days or c.coverage_window_days)
    role = c.coverage.get("dimension_role") or "dim1"
    col = ROLE_COLUMNS.get(role) or ROLE_COLUMNS["dim1"]
    since = datetime.utcnow() - timedelta(days=days)
    rows = db.query(Event).filter(Event.need_id == need_id, Event.created_at >= since).all()
    counts: dict[str, int] = {}
    for e in rows:
        v = getattr(e, col, None)
        for x in (v if isinstance(v, list) else [v]):
            k = str(x) if x else "未分类"
            counts[k] = counts.get(k, 0) + 1
    floor = c.coverage_min_records
    out = []
    for l1, l2s in dimension_values(c).items():
        n = counts.get(l1, 0)
        out.append({"industry": l1, "value": l1, "sub": l2s, "events": n,
                    "gap": n < floor,
                    "level": "空白" if n == 0 else ("偏少" if n < floor else "有覆盖")})
    # 词表里没有但记录里出现的取值(含"未分类")也列出来,便于发现词表该补
    for k, n in counts.items():
        if k not in {o["industry"] for o in out}:
            out.append({"industry": k, "value": k, "sub": [], "events": n, "gap": False, "level": "词表外"})
    return sorted(out, key=lambda o: (o["events"], o["industry"]))


def summary(db, need_id: str, days: int | None = None, ctx=None) -> dict:
    """覆盖度总览:维度分布 + 源结构 + 该补什么。页面与日报都用这一份。"""
    c = _ctx(db, need_id, ctx)
    days = int(days or c.coverage_window_days)
    cov = industry_coverage(db, need_id, days, c)
    gaps = [x for x in cov if x["gap"]]
    srcs = [s for s in db.query(Source).all() if need_id in (s.serves_needs or [])]
    active = [s for s in srcs if s.lifecycle in ("active", "trial")]
    since = datetime.utcnow() - timedelta(days=days)
    producing = {r[0] for r in db.query(RawDocument.source_id)
                 .filter(RawDocument.need_id == need_id, RawDocument.fetched_at >= since).distinct().all()}
    role = c.coverage.get("dimension_role") or "dim1"
    return {
        "window_days": days,
        "min_events": c.coverage_min_records,
        "dimension_role": role, "dimension_label": c.role_label(role),
        "industries": cov,
        "gap_count": len(gaps),
        "gap_industries": [g["industry"] for g in gaps],
        "sources_total": len(srcs),
        "sources_active": len(active),
        "sources_producing": len([s for s in active if s.id in producing]),
        "sources_silent": [s.name for s in active if s.id not in producing][:50],
        "prospect_queries": prospect_queries(db, need_id, days, ctx=c),
    }


def prospect_queries(db, need_id: str, days: int | None = None, per_industry: int = 2, ctx=None) -> list[str]:
    """把覆盖空白翻译成找源检索词(交给 prospect.run_once 去搜索引擎捞渠道)。

    模板只组 **两三个词**:搜索引擎按 AND 收紧,词越多召回越窄,找源恰恰需要广度。
    占位桶(其他/未分类)不是真实取值,组出来的词毫无意义,必须排除。
    """
    c = _ctx(db, need_id, ctx)
    tpls = [str(t) for t in (c.coverage.get("query_templates") or []) if "{ind}" in str(t)]
    if not tpls:
        tpls = ["{ind} " + c.name]
    short = {str(k): str(v) for k, v in (c.coverage.get("short_names") or {}).items()}
    placeholders = {str(x) for x in (c.coverage.get("placeholders") or [])}
    out = []
    for x in industry_coverage(db, need_id, days, c):
        if not x["gap"] or x["industry"] in placeholders:
            continue
        ind = short.get(x["industry"], x["industry"])
        for tpl in tpls[:per_industry]:
            out.append(tpl.format(ind=ind))
    return out
