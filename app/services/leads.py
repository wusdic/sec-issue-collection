"""线索引擎(通用平台 · 输出层):多维映射 + 评分 + 时间窗阶段。

映射规则文件、评分权重、窗口阶段、匹配维度、话术模板全部来自画像 `outputs.leads_engine`;
`enabled=false` 的需求整段不产线索。引擎里不再有任何行业字面量。
"""
from datetime import date

import yaml
from sqlalchemy.orm import Session

from app.models import Event, Lead
from app.services import need_ctx
from app.services.need_ctx import dget


def _ctx(ctx=None, db=None, need_id: str | None = None):
    return ctx or need_ctx.get(db, need_id or need_ctx.default_need_id())


def load_mapping_rules(path=None, ctx=None) -> list[dict]:
    c = _ctx(ctx)
    p = path or c.path(c.leads.get("mapping_file"))
    if not p:
        return []
    try:
        with open(p, encoding="utf-8") as f:
            return (yaml.safe_load(f) or {}).get("rules", [])
    except OSError:
        return []


def window_stage(disclosed: date | None, today: date | None = None, ctx=None) -> str:
    """时间窗阶段:按画像 window_stages 的 max_days 递进;max_days=null 的是"已过窗"。"""
    stages = _ctx(ctx).leads.get("window_stages") or []
    if not stages:
        return "整改期"
    bounded = [s for s in stages if s.get("max_days") is not None]
    if not disclosed:
        return bounded[1]["name"] if len(bounded) > 1 else stages[0]["name"]
    days = ((today or date.today()) - disclosed).days
    for s in bounded:
        if days <= int(s["max_days"]):
            return s["name"]
    return stages[-1]["name"]


def _stage_weight(stage: str, ctx) -> float:
    for s in ctx.leads.get("window_stages") or []:
        if s.get("name") == stage:
            return float(s.get("weight", 0.5))
    return 0.5


def _dim_values(payload: dict, spec: dict) -> set:
    """按画像 match_dims 的一条规格,从 payload 里取出该维度的取值集合。"""
    val = dget(payload, str(spec.get("path") or ""))
    item_key, status_key, status_in = spec.get("item_key"), spec.get("status_key"), spec.get("status_in")
    if isinstance(val, list):
        out = set()
        for it in val:
            if isinstance(it, dict):
                if status_key and status_in and it.get(status_key) not in set(status_in):
                    continue
                out.add(it.get(item_key) if item_key else None)
            else:
                out.add(it)
        return {x for x in out if x is not None}
    return {val} if val not in (None, "", {}) else set()


def _match_rule(rule: dict, payload: dict, ctx=None) -> bool:
    """多维匹配:规则内各维为 OR,维间为 AND;维度定义来自画像 match_dims。"""
    c = _ctx(ctx)
    dims = c.leads.get("match_dims") or {}
    for dim, wanted in (rule.get("match") or {}).items():
        spec = dims.get(dim) or {"path": dim}
        have = _dim_values(payload, spec)
        if wanted and not (set(wanted) & have):
            return False
    return True


def map_products(payload: dict, rules: list[dict] | None = None, ctx=None) -> list[str]:
    c = _ctx(ctx)
    rules = rules if rules is not None else load_mapping_rules(ctx=c)
    products: list[str] = []
    for rule in rules:
        if _match_rule(rule, payload, c):
            for p in rule.get("products", []):
                if p not in products:
                    products.append(p)
    return products


def score_lead(ev: Event, stage: str, products: list[str], reachable_bonus: float = 0.0, ctx=None) -> float:
    """线索分 = 等级 × 窗口权重 × 匹配度 × 规模 ×(1+可触达加分),映射到 0-100。"""
    c = _ctx(ctx, need_id=ev.need_id)
    gw, sw = c.leads.get("grade_weights") or {}, c.leads.get("size_weights") or {}
    sev = float(gw.get(ev.severity or "", 0.35)) if gw else 0.5
    stg = _stage_weight(stage, c)
    match = min(1.0, 0.3 + 0.14 * len(products))
    size = float(sw.get(ev.org_size or "未知", 0.5)) if sw else 0.5
    return round(100 * sev * stg * match * size * (1 + reachable_bonus), 1)


def talk_track(ev: Event, products: list[str], ctx=None) -> str:
    c = _ctx(ctx, need_id=ev.need_id)
    p = ev.payload or {}
    facts = []
    for f in (c.leads.get("talk_track") or {}).get("facts") or []:
        kind, tpl = f.get("kind"), str(f.get("template") or "{v}")
        if kind == "dim1":
            v = c.get_role(p, "dim1")
            if v:
                facts.append(tpl.format(v=v, title=p.get("title", "")))
        elif kind == "tristate_status":
            for fld in f.get("fields") or []:
                money = p.get(fld) or {}
                if isinstance(money, dict) and money.get("status") not in (None, "未披露", "无此类损失"):
                    facts.append(tpl.format(f=fld, status=money.get("status")))
        elif kind == "path":
            v = dget(p, str(f.get("path") or ""))
            if v:
                facts.append(tpl.format(v=v))
        elif kind == "products":
            facts.append(tpl.format(products="、".join(products[:5]) or "待映射"))
        elif kind == "text":
            facts.append(tpl)
    return ";".join(facts)


def generate_leads(db: Session, ev: Event, rules: list[dict] | None = None, ctx=None) -> list[Lead]:
    """记录发布/更新后生成或刷新线索。画像未开启线索引擎的需求直接返回空。"""
    c = _ctx(ctx, db, ev.need_id)
    if not c.leads.get("enabled"):
        return []
    payload = ev.payload or {}
    products = map_products(payload, rules, c)
    wb = c.leads.get("write_back_field")            # 画像可声明把匹配到的产品回写到记录的哪个字段
    if wb and products and payload.get(wb) != products:
        payload = dict(payload)
        payload[wb] = products
        ev.payload = payload
    stage = window_stage(ev.disclosed_date, ctx=c)
    org = str(c.get_role(payload, c.leads.get("subject_role") or "subject") or ev.org_name or "未披露")
    if org in ("", "未披露", "未知"):
        return []
    score = score_lead(ev, stage, products, ctx=c)
    existing = db.query(Lead).filter_by(event_id=ev.event_id, target_org=org).one_or_none()
    if existing:
        existing.score = score
        existing.window_stage = stage
        existing.products = products
        db.flush()
        return [existing]
    lead = Lead(need_id=ev.need_id, event_id=ev.event_id, target_org=org, target_kind="victim",
                score=score, window_stage=stage, products=products,
                talk_track=talk_track(ev, products, c))
    db.add(lead)
    db.flush()
    return [lead]


def refresh_window_stages(db: Session, need_id: str) -> int:
    """每日重算窗口阶段与评分(过期自动降级)。"""
    c = need_ctx.get(db, need_id)
    n = 0
    for lead in db.query(Lead).filter(Lead.need_id == need_id,
                                      Lead.status.in_(["new", "dispatched", "followed"])).all():
        ev = db.get(Event, lead.event_id)
        stage = window_stage(ev.disclosed_date, ctx=c)
        if stage != lead.window_stage:
            lead.window_stage = stage
            lead.score = score_lead(ev, stage, lead.products or [], ctx=c)
            last = (c.leads.get("window_stages") or [{}])[-1].get("name")
            if stage == last and lead.status == "new":
                lead.status = "dropped"
            n += 1
    db.flush()
    return n
