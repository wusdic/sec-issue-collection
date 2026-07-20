"""线索引擎(M8 / 方案 12.3):四维产品映射 + 评分 + 采购窗口三阶段。"""
from datetime import date

import yaml
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Event, Lead

_SEVERITY_W = {"特别重大": 1.0, "重大": 0.85, "较大": 0.65, "一般": 0.45, "未定级": 0.35}
_SIZE_W = {"特大": 1.0, "大": 0.85, "中": 0.6, "小微": 0.35, "未知": 0.5}
_STAGE_W = {"应急期": 1.0, "整改期": 0.9, "预算期": 0.6, "已过窗": 0.2}


def load_mapping_rules(path=None) -> list[dict]:
    path = path or settings.config_dir / "product_mapping.yaml"
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f).get("rules", [])


def window_stage(disclosed: date | None, today: date | None = None) -> str:
    """采购窗口三阶段(12.3)。"""
    if not disclosed:
        return "整改期"
    days = ((today or date.today()) - disclosed).days
    if days <= 30:
        return "应急期"
    if days <= 180:
        return "整改期"
    if days <= 540:
        return "预算期"
    return "已过窗"


def _match_rule(rule: dict, payload: dict) -> bool:
    """四维匹配:手段×入口×根因×后果,规则内各维为 OR,维间为 AND。"""
    checks = {
        "attack_type": set(payload.get("attack_type") or []),
        "consequences": set(payload.get("consequences") or []),
        "entry_vector": {e.get("vector") for e in payload.get("entry_vector") or []},
        "root_cause": {(payload.get("root_cause") or {}).get("category")},
        "security_controls": {c.get("control") for c in payload.get("security_controls") or []
                              if c.get("status") in ("缺位", "在位但失效", "在位被绕过")},
    }
    for dim, wanted in rule.get("match", {}).items():
        have = checks.get(dim, set())
        if wanted and not (set(wanted) & have):
            return False
    return True


def map_products(payload: dict, rules: list[dict] | None = None) -> list[str]:
    rules = rules if rules is not None else load_mapping_rules()
    products: list[str] = []
    for rule in rules:
        if _match_rule(rule, payload):
            for p in rule.get("products", []):
                if p not in products:
                    products.append(p)
    return products


def score_lead(ev: Event, stage: str, products: list[str], reachable_bonus: float = 0.0) -> float:
    """线索分 = 严重度 × 窗口权重 × 匹配度 × 规模 ×(1+可触达加分),映射到 0-100。"""
    sev = _SEVERITY_W.get(ev.severity or "未定级", 0.35)
    stg = _STAGE_W.get(stage, 0.5)
    match = min(1.0, 0.3 + 0.14 * len(products))
    size = _SIZE_W.get(ev.org_size or "未知", 0.5)
    return round(100 * sev * stg * match * size * (1 + reachable_bonus), 1)


def talk_track(ev: Event, products: list[str]) -> str:
    p = ev.payload or {}
    facts = []
    if ev.industry_l1:
        facts.append(f"同行业({ev.industry_l1})近期发生:{p.get('title','安全事件')}")
    for f in ("loss_L4", "loss_L2", "loss_L1"):
        money = p.get(f) or {}
        if money.get("status") not in (None, "未披露", "无此类损失"):
            facts.append(f"{f} 状态:{money.get('status')}")
    if (p.get("ransom") or {}).get("demanded_amount"):
        facts.append(f"攻击者要求赎金 {p['ransom']['demanded_amount']}(注意:要求≠损失)")
    facts.append(f"建议切入:{'、'.join(products[:5]) or '待产品映射'}")
    facts.append("话术仅引用公开来源事实,禁止贬损任何在位厂商")
    return ";".join(facts)


def generate_leads(db: Session, ev: Event, rules: list[dict] | None = None) -> list[Lead]:
    """事件发布/更新后生成或刷新线索(victim 类;same_product 待企业库集成)。"""
    payload = ev.payload or {}
    products = map_products(payload, rules)
    # 回写 D5 可售映射(自动初筛,人工在复核台确认)
    if products and payload.get("sellable_mapping") != products:
        payload = dict(payload)
        payload["sellable_mapping"] = products
        ev.payload = payload
    stage = window_stage(ev.disclosed_date)
    org = ev.org_name or "未披露"
    if org == "未披露":
        return []
    score = score_lead(ev, stage, products)
    existing = db.query(Lead).filter_by(event_id=ev.event_id, target_org=org).one_or_none()
    if existing:
        existing.score = score
        existing.window_stage = stage
        existing.products = products
        db.flush()
        return [existing]
    lead = Lead(need_id=ev.need_id, event_id=ev.event_id, target_org=org, target_kind="victim",
                score=score, window_stage=stage, products=products,
                talk_track=talk_track(ev, products))
    db.add(lead)
    db.flush()
    return [lead]


def refresh_window_stages(db: Session, need_id: str) -> int:
    """每日重算窗口阶段与评分(过期自动降级)。"""
    n = 0
    for lead in db.query(Lead).filter(Lead.need_id == need_id,
                                      Lead.status.in_(["new", "dispatched", "followed"])).all():
        ev = db.get(Event, lead.event_id)
        stage = window_stage(ev.disclosed_date)
        if stage != lead.window_stage:
            lead.window_stage = stage
            lead.score = score_lead(ev, stage, lead.products or [])
            if stage == "已过窗" and lead.status == "new":
                lead.status = "dropped"
            n += 1
    db.flush()
    return n
