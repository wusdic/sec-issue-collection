"""断言三态守卫(通用平台:原"金额三态守卫"的参数化版,模块名保留以兼容既有调用)。

事实核实型质量模型的核心:任何关键断言分 声称(claimed)/第三方估算(estimated)/权威确认(confirmed)
三个通道。守卫规则与领域无关,**哪些字段是三态、用什么语境词、隔离哪个字段**由画像声明:
- R1 声称语境:证据文本命中 claimed_markers → 不得进 confirmed;误填则降级并记违规;
- R2 confirmed 语境:必须命中 confirmed_markers,否则降级;
- R3 隔离:画像 `isolation` 声明的来源字段(如赎金要求金额)不得出现在任何三态通道;
- R4 模型产出的 confirmed 一律 pending_human=True,复核通过前不参与统计;
- 附加 paid_check:声明"已支付"却无支付金额 → 记违规。
画像键:quality.assertions(见 design/platform/02 D4)。缺省用默认需求的画像。
"""
import re
from dataclasses import dataclass, field

from app.services import need_ctx
from app.services.need_ctx import dget

@dataclass
class GuardResult:
    payload: dict
    violations: list[str] = field(default_factory=list)
    demoted_fields: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.violations


def _resolve(ctx):
    return ctx or need_ctx.get(None, need_ctx.default_need_id())


def tristate_fields(ctx=None) -> list[str]:
    return list(_resolve(ctx).tristate_fields)


def _channels(ctx) -> dict:
    ch = _resolve(ctx).assertions.get("channels") or {}
    return {"claimed": ch.get("claimed", "claimed_cny"),
            "estimated": ch.get("estimated", "estimated_cny"),
            "confirmed": ch.get("confirmed", "confirmed_cny")}


def _span_for(payload: dict, key: str) -> str:
    spans = payload.get("_source_spans") or {}
    return str(spans.get(key) or "")


def _has_amount(channel) -> bool:
    if not isinstance(channel, dict):
        return channel is not None
    return any(channel.get(k) is not None for k in ("point", "low", "high"))


def apply_guard(payload: dict, full_text: str = "", ctx=None) -> GuardResult:
    """对抽取结果执行三态守卫;修改后的 payload 原地返回。"""
    c = _resolve(ctx)
    a = c.assertions
    fields = list(a.get("tristate_fields") or [])
    ch = _channels(c)
    claimed_re = re.compile(str(a.get("claimed_markers") or "(?!)"))
    confirmed_re = re.compile(str(a.get("confirmed_markers") or "(?!)"))
    result = GuardResult(payload=payload)

    for f in fields:
        money = payload.get(f)
        if not isinstance(money, dict):
            continue
        confirmed = money.get(ch["confirmed"])
        if not _has_amount(confirmed):
            continue
        evidence = _span_for(payload, f) or (money.get("note") or "") or full_text[:2000]
        claimed_hit = bool(claimed_re.search(evidence))
        confirmed_hit = bool(confirmed_re.search(evidence))
        if claimed_hit and not confirmed_hit:
            money[ch["claimed"]] = money.get(ch["claimed"]) or confirmed
            money[ch["confirmed"]] = None
            money["status"] = "仅声称"
            result.violations.append(f"{f}: 声称语境断言误入 confirmed,已降级为 claimed")
            result.demoted_fields.append(f)
        elif not confirmed_hit:
            money[ch["claimed"]] = money.get(ch["claimed"]) or confirmed
            money[ch["confirmed"]] = None
            money["status"] = "仅声称"
            result.violations.append(f"{f}: confirmed 缺少权威确认语境证据,已降级")
            result.demoted_fields.append(f)
        else:
            money["pending_human"] = True          # R4:语境合格也只是"候选确认",待人工

    # R3 隔离:画像声明的来源值不得等于任何三态通道的点值
    for rule in a.get("isolation") or []:
        src_val = dget(payload, str(rule.get("source_path") or ""))
        if not src_val:
            continue
        for f in fields:
            money = payload.get(f)
            if not isinstance(money, dict):
                continue
            for chname in ch.values():
                amt = money.get(chname)
                if isinstance(amt, dict) and amt.get("point") == src_val:
                    money[chname] = None
                    if not _has_amount(money.get(ch["claimed"])) and not _has_amount(money.get(ch["confirmed"])):
                        money["status"] = "未披露"
                    result.violations.append(
                        f"{f}.{chname}: 与 {rule.get('source_path')} 相同({src_val}),"
                        f"{rule.get('reason') or '疑似误计入'},已清除")
                    result.demoted_fields.append(f)

    pc = a.get("paid_check")
    if pc:
        if dget(payload, pc.get("flag_path", "")) == pc.get("flag_value") and dget(payload, pc.get("amount_path", "")) is None:
            result.violations.append(f"{pc.get('flag_path')}: 标记为『{pc.get('flag_value')}』但无金额来源,请复核")
    return result


def confirmed_fields(payload: dict, fields: list[str] | None = None, ctx=None) -> list[str]:
    """返回存在 confirmed 断言的三态字段(用于双签判定与发布校验)。"""
    c = _resolve(ctx)
    fields = fields if fields is not None else list(c.tristate_fields)
    conf_key = _channels(c)["confirmed"]
    out = []
    for f in fields:
        money = payload.get(f)
        if isinstance(money, dict) and _has_amount(money.get(conf_key)):
            out.append(f)
    return out
