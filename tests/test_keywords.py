"""关键词矩阵展开:去掉隐藏上限、按预算截断、交叉深度可配。"""
from app.services.scheduler import expand_queries


def _content(budget=None, ce=None, ci=None):
    c = {
        "event_terms": [f"E{i}" for i in range(20)],
        "industry_terms": [f"行业{i}" for i in range(30)],
        "consequence_terms": [f"后果{i}" for i in range(15)],
        "org_terms": [f"单位{i}" for i in range(6)],
    }
    if budget is not None:
        c["query_budget_per_source_daily"] = budget
    if ce is not None:
        c["cross_event_terms"] = ce
    if ci is not None:
        c["cross_industry_terms"] = ci
    return c


def test_no_hidden_60_cap():
    """预算 300 时应产出远多于 60 条(旧实现被写死 60)。"""
    q = expand_queries(_content(budget=300))
    assert len(q) > 60
    assert len(q) <= 300


def test_budget_truncates():
    q = expand_queries(_content(budget=25))
    assert len(q) == 25


def test_dedup_and_cross_depth():
    q = expand_queries(_content(budget=1000, ce=2, ci=2))
    # 事件词 20 + 交叉(2×2=4) + 后果×单位(12默认×5默认=60) ,去重后无重复
    assert len(q) == len(set(q))
    # 交叉深度收窄后事件×行业组合变少
    small = expand_queries(_content(budget=1000, ce=1, ci=1))
    assert len(small) < len(q)


def test_budget_zero_means_all():
    q = expand_queries(_content(budget=0))
    assert len(q) == len(set(q)) and len(q) > 60
