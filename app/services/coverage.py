"""覆盖度盘点:按行业看近 N 天有没有事件,空白就是"该去找源的方向"。

以前只知道"现在有什么源、采到了什么",不知道"缺哪块"。这里拿词表里的行业分类
(schema/dictionaries.yaml 的 industries)去比对近 coverage_window_days 天的事件分布,
低于 coverage_min_events 的行业判为覆盖空白,并自动生成对应的找源检索词交给
prospect.py——"缺哪块就去找哪块的源",形成闭环而不是每周搜同样的词。
"""
from datetime import datetime, timedelta

import yaml

from app.config import settings
from app.models import Event, RawDocument, Source


def _industries() -> dict[str, list[str]]:
    try:
        with open(settings.schema_dir / "dictionaries.yaml", encoding="utf-8") as f:
            return dict((yaml.safe_load(f) or {}).get("industries") or {})
    except (OSError, yaml.YAMLError):
        return {}


def industry_coverage(db, need_id: str, days: int | None = None) -> list[dict]:
    """每个一级行业近 N 天的事件数 / 源覆盖情况,按"最缺"排前面。"""
    days = int(days or settings.coverage_window_days)
    since = datetime.utcnow() - timedelta(days=days)
    rows = (db.query(Event)
            .filter(Event.need_id == need_id, Event.created_at >= since).all())
    counts: dict[str, int] = {}
    for e in rows:
        counts[e.industry_l1 or "未分类"] = counts.get(e.industry_l1 or "未分类", 0) + 1
    floor = int(settings.coverage_min_events)
    out = []
    for l1, l2s in _industries().items():
        n = counts.get(l1, 0)
        out.append({"industry": l1, "sub": l2s, "events": n,
                    "gap": n < floor,
                    "level": "空白" if n == 0 else ("偏少" if n < floor else "有覆盖")})
    # 词表里没有但事件里出现的分类(含"未分类")也列出来,便于发现词表该补
    for k, n in counts.items():
        if k not in {o["industry"] for o in out}:
            out.append({"industry": k, "sub": [], "events": n, "gap": False, "level": "词表外"})
    return sorted(out, key=lambda o: (o["events"], o["industry"]))


def summary(db, need_id: str, days: int | None = None) -> dict:
    """覆盖度总览:行业分布 + 源结构 + 该补什么。页面与日报都用这一份。"""
    days = int(days or settings.coverage_window_days)
    cov = industry_coverage(db, need_id, days)
    gaps = [c for c in cov if c["gap"]]
    srcs = [s for s in db.query(Source).all() if need_id in (s.serves_needs or [])]
    active = [s for s in srcs if s.lifecycle in ("active", "trial")]
    since = datetime.utcnow() - timedelta(days=days)
    producing = {r[0] for r in db.query(RawDocument.source_id)
                 .filter(RawDocument.need_id == need_id, RawDocument.fetched_at >= since).distinct().all()}
    return {
        "window_days": days,
        "min_events": int(settings.coverage_min_events),
        "industries": cov,
        "gap_count": len(gaps),
        "gap_industries": [g["industry"] for g in gaps],
        "sources_total": len(srcs),
        "sources_active": len(active),
        "sources_producing": len([s for s in active if s.id in producing]),
        "sources_silent": [s.name for s in active if s.id not in producing][:50],
        "prospect_queries": prospect_queries(db, need_id, days),
    }


# 每个空白行业生成的找源词模板:目标是找"渠道",不是找单条事件
_Q_TPL = ["{ind} 网络安全 事件 通报 公众号",
          "{ind} 数据泄露 案例 网站",
          "{ind} 行业 安全 监管 处罚 公告"]


def prospect_queries(db, need_id: str, days: int | None = None, per_industry: int = 2) -> list[str]:
    """把覆盖空白翻译成找源检索词(交给 prospect.run_once 去搜索引擎捞渠道)。"""
    out = []
    for c in industry_coverage(db, need_id, days):
        if not c["gap"]:
            continue
        for tpl in _Q_TPL[:per_industry]:
            out.append(tpl.format(ind=c["industry"]))
    return out
