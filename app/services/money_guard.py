"""金额三态守卫(方案第 5/6 节红线的代码强制层)。

规则:
- R1 声称语境:金额证据文本命中『要求/索赔/勒索/拟处罚/预计/或将/传/主张/称』
  → 该金额不得进入 confirmed 通道;若 LLM 误填,自动降级到 claimed 并记录违规。
- R2 confirmed 语境:必须命中『判决/裁定/处罚决定/决定书/公告/年报/确认』之一,
  否则 confirmed 降级为 claimed。
- R3 赎金隔离:ransom.demanded_amount 不得出现在任何 loss_* 通道;
  仅 ransom.paid='已支付' 且有 paid_amount 时,允许(由人工)计入 loss_L1 并备注。
- R4 LLM 产出的 confirmed 一律标记 pending_human=True,复核通过前不参与统计。
"""
import re
from dataclasses import dataclass, field

CLAIMED_MARKERS = re.compile(r"要求|索赔|勒索|拟处罚|拟罚|预计|或将|据传|传闻|主张|声称|估计|约合")
CONFIRMED_MARKERS = re.compile(r"判决|裁定|处罚决定|决定书|行政处罚|公告(?:披露|确认)|年报(?:披露|确认)|已支付|已缴纳")

LOSS_FIELDS = ["loss_L1", "loss_L2", "loss_L3", "loss_L4", "loss_L5"]


@dataclass
class GuardResult:
    payload: dict
    violations: list[str] = field(default_factory=list)
    demoted_fields: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.violations


def _span_for(payload: dict, key: str) -> str:
    spans = payload.get("_source_spans") or {}
    return str(spans.get(key) or "")


def _has_amount(channel) -> bool:
    if not isinstance(channel, dict):
        return channel is not None
    return any(channel.get(k) is not None for k in ("point", "low", "high"))


def apply_guard(payload: dict, full_text: str = "") -> GuardResult:
    """对抽取结果执行三态守卫;修改后的 payload 原地返回。"""
    result = GuardResult(payload=payload)

    for f in LOSS_FIELDS:
        money = payload.get(f)
        if not isinstance(money, dict):
            continue
        confirmed = money.get("confirmed_cny")
        if not _has_amount(confirmed):
            continue
        evidence = _span_for(payload, f) or (money.get("note") or "") or full_text[:2000]
        claimed_hit = bool(CLAIMED_MARKERS.search(evidence))
        confirmed_hit = bool(CONFIRMED_MARKERS.search(evidence))
        if claimed_hit and not confirmed_hit:
            # R1: 声称语境金额被填入 confirmed → 强制降级
            money["claimed_cny"] = money.get("claimed_cny") or confirmed
            money["confirmed_cny"] = None
            money["status"] = "仅声称"
            result.violations.append(f"{f}: 声称语境金额误入 confirmed,已降级为 claimed")
            result.demoted_fields.append(f)
        elif not confirmed_hit:
            # R2: 无 confirmed 语境证据 → 降级
            money["claimed_cny"] = money.get("claimed_cny") or confirmed
            money["confirmed_cny"] = None
            money["status"] = "仅声称"
            result.violations.append(f"{f}: confirmed 缺少判决/决定书/公告类语境证据,已降级")
            result.demoted_fields.append(f)
        else:
            # R4: 语境合格也只是"候选确认",待人工
            money["pending_human"] = True

    # R3: 赎金隔离
    ransom = payload.get("ransom") or {}
    demanded = ransom.get("demanded_amount")
    if demanded:
        for f in LOSS_FIELDS:
            money = payload.get(f)
            if not isinstance(money, dict):
                continue
            for ch in ("confirmed_cny", "claimed_cny", "estimated_cny"):
                amt = money.get(ch)
                if isinstance(amt, dict) and amt.get("point") == demanded:
                    money[ch] = None
                    if not _has_amount(money.get("claimed_cny")) and not _has_amount(money.get("confirmed_cny")):
                        money["status"] = "未披露"
                    result.violations.append(f"{f}.{ch}: 与赎金要求金额相同({demanded}),疑似赎金误计入损失,已清除")
                    result.demoted_fields.append(f)
    if ransom.get("paid") == "已支付" and ransom.get("paid_amount") is None:
        result.violations.append("ransom: 标记已支付但无支付金额来源,请复核")

    return result


def confirmed_fields(payload: dict) -> list[str]:
    """返回存在 confirmed 金额的损失字段(用于双人复核判定与发布校验)。"""
    out = []
    for f in LOSS_FIELDS:
        money = payload.get(f)
        if isinstance(money, dict) and _has_amount(money.get("confirmed_cny")):
            out.append(f)
    return out
