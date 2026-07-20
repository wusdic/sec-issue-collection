"""红线用例:金额三态守卫。"""
from app.services.money_guard import apply_guard, confirmed_fields


def _payload(loss_l1=None, ransom=None, spans=None):
    p = {
        "loss_L1": loss_l1 or {"status": "未披露"},
        "loss_L2": {"status": "未披露"}, "loss_L3": {"status": "未披露"},
        "loss_L4": {"status": "未披露"}, "loss_L5": {"status": "未披露"},
        "ransom": ransom or {"applicable": False},
        "_source_spans": spans or {},
    }
    return p


def test_claimed_context_demotes_confirmed():
    """『要求 200 万』被误填 confirmed → 必须降级为 claimed。"""
    p = _payload(
        loss_l1={"confirmed_cny": {"point": 2000000}, "status": "已确认"},
        spans={"loss_L1": "攻击者要求支付200万元赎金"},
    )
    r = apply_guard(p)
    assert not r.clean
    assert p["loss_L1"]["confirmed_cny"] is None
    assert p["loss_L1"]["claimed_cny"] == {"point": 2000000}
    assert p["loss_L1"]["status"] == "仅声称"


def test_lawsuit_claim_demoted():
    """『索赔 5000 万』不能进 confirmed。"""
    p = _payload(
        loss_l1={"confirmed_cny": {"point": 50000000}, "status": "已确认"},
        spans={"loss_L1": "原告向法院索赔5000万元"},
    )
    r = apply_guard(p)
    assert "loss_L1" in r.demoted_fields


def test_confirmed_context_kept_but_pending_human():
    """判决语境的 confirmed 保留,但必须标 pending_human 待人工确认。"""
    p = _payload(
        loss_l1={"confirmed_cny": {"point": 3000000}, "status": "已确认"},
        spans={"loss_L1": "法院判决被告赔偿300万元,处罚决定书已生效"},
    )
    r = apply_guard(p)
    assert p["loss_L1"]["confirmed_cny"] == {"point": 3000000}
    assert p["loss_L1"]["pending_human"] is True
    assert confirmed_fields(p) == ["loss_L1"]


def test_no_context_demoted():
    """无任何 confirmed 语境证据 → 降级。"""
    p = _payload(loss_l1={"confirmed_cny": {"point": 100}, "status": "已确认"})
    r = apply_guard(p, full_text="据媒体报道估计损失约100元")
    assert p["loss_L1"]["confirmed_cny"] is None


def test_ransom_isolation():
    """赎金要求金额出现在损失通道 → 清除。"""
    p = _payload(
        loss_l1={"claimed_cny": {"point": 2000000}, "status": "仅声称"},
        ransom={"applicable": True, "demanded_amount": 2000000, "paid": "未披露"},
    )
    r = apply_guard(p)
    assert p["loss_L1"]["claimed_cny"] is None
    assert any("赎金" in v for v in r.violations)
