"""试运行源自动定级/自动淘汰:把"候选转正"这件事从纯人工改成规则自动 + 人工兜底。

原来 discovery.promote 是唯一转正入口且必须人工点,候选源只会越堆越多没人处理。
这里按"客观可验证的事实 + 试运行期实际产出"自动决策:

- S1(官方权威):只给**域名本身可验证**的官方来源(.gov.cn 等政务域名、名录内的监管机构
  与官方技术机构)。这是客观事实不是判断,自动给零风险;
- S3(专业媒体):试运行期产出达标(篇数够 + 相关率够)即自动转正。S3 不能支撑"已确认"
  金额,自动给不会突破发布红线;
- S2(企业自披露):需要判断"这个站是不是该企业自己的官网",机器判不可靠,**只给建议**
  等人工一键确认——S2 能支撑已确认金额,是红线,不自动;
- 产出很多但相关率极低 → 自动淘汰;样本太少 → 自动延长试运行,不急着下结论。

人工添加的源(discovered_from=manual)只升不降,绝不自动淘汰。
"""
from datetime import datetime, timedelta
from urllib.parse import urlparse

import yaml

from app.config import settings
from app.models import RawDocument, Source, SourceProbe
from app.services import actions, url_tools

# 政务/军队域名后缀:域名本身即可证明是官方来源
_OFFICIAL_SUFFIXES = (".gov.cn", ".mil.cn")


def _cfg(need_id: str | None = None) -> dict:
    """定级规则:画像 sources.grading,缺省取画像 discovery_file 的 grading 节。"""
    from app.services import need_ctx
    return need_ctx.get(None, need_id or need_ctx.default_need_id()).grading


def official_domains() -> set[str]:
    """名录内的官方机构域名(非 .gov.cn 但同样权威,如 CNCERT/交易所/裁判文书网)。"""
    return {str(d).strip().lower() for d in _cfg().get("official_domains") or [] if str(d).strip()}


def is_official(src: Source) -> bool:
    """域名是否可客观验证为官方来源(政务域名或名录内官方机构)。"""
    url = src.entry_url or ""
    host = (urlparse(url).netloc or "").lower().split(":")[0]
    if not host:
        return False
    if host.endswith(_OFFICIAL_SUFFIXES) or host in ("gov.cn",):
        return True
    dom = url_tools.registered_domain(host) or host
    return dom in official_domains() or host in official_domains()


def source_metrics(db, src: Source) -> dict:
    """试运行期的实际表现:产出多少、多少被判为相关、原创率、候选期 LLM 初评分。"""
    docs = db.query(RawDocument).filter_by(source_id=src.id).all()
    total = len(docs)
    relevant = sum(1 for d in docs if d.screen_status == "screened_in")
    manual = sum(1 for d in docs if d.screen_status == "manual_queue")
    primary = sum(1 for d in docs if d.is_primary)
    probe = db.get(SourceProbe, src.site_key) if src.site_key else None
    # 待人工的算半个相关:它没被判为不相干,只是信息不足
    ratio = ((relevant + 0.5 * manual) / total) if total else 0.0
    days = (datetime.utcnow() - src.trial_started_at).days if src.trial_started_at else None
    return {"docs_total": total, "relevant": relevant, "manual_queue": manual,
            "relevant_ratio": round(ratio, 2),
            "originality": round(primary / total, 2) if total else 0.0,
            "llm_relevance": (float(probe.relevance) if (probe and probe.ok) else None),
            "trial_days": days}


def decide(db, src: Source) -> dict:
    """对一个试运行源给出处置决定(不落库,便于预览/测试)。

    返回 {action, credibility, reason, metrics}。action ∈
    promote(自动转正) / suggest(建议等级,待人工确认) / extend(样本不足,延长试运行) / retire(自动淘汰)。
    """
    c = _cfg()
    m = source_metrics(db, src)
    min_days = int(c.get("trial_days", 14) or 14)
    min_docs = int(c.get("promote_min_docs", 5) or 5)
    keep_ratio = float(c.get("promote_min_relevant_ratio", 0.3) or 0.3)
    drop_ratio = float(c.get("retire_max_relevant_ratio", 0.05) or 0.05)
    drop_docs = int(c.get("retire_min_docs", 20) or 20)

    if is_official(src):
        return {"action": "promote", "credibility": "S1", "metrics": m,
                "reason": "政务域名/名录内官方机构,来源权威性由域名客观可验证"}

    if (m["trial_days"] or 0) < min_days:
        return {"action": "extend", "credibility": None, "metrics": m,
                "reason": f"试运行第 {m['trial_days'] or 0} 天(需满 {min_days} 天),继续观察"}

    if m["docs_total"] >= drop_docs and m["relevant_ratio"] <= drop_ratio:
        return {"action": "retire", "credibility": None, "metrics": m,
                "reason": (f"试运行产出 {m['docs_total']} 篇,相关率仅 {m['relevant_ratio']}"
                           f"(≤{drop_ratio}),判定为噪声源,自动淘汰")}

    if m["docs_total"] < min_docs:
        return {"action": "extend", "credibility": None, "metrics": m,
                "reason": (f"试运行 {m['trial_days']} 天只产出 {m['docs_total']} 篇(需 {min_docs} 篇),"
                           "样本不足不下结论,继续观察")}

    if m["relevant_ratio"] >= keep_ratio:
        # S2 需要判断"是不是该企业自己的官网",机器判不可靠且 S2 能支撑已确认金额(红线),
        # 故自动只到 S3;真该是 S2 的由人工在候选页一键改。
        return {"action": "promote", "credibility": "S3", "metrics": m,
                "reason": (f"试运行 {m['trial_days']} 天产出 {m['docs_total']} 篇、"
                           f"相关率 {m['relevant_ratio']}(≥{keep_ratio}),自动转正为 S3 专业媒体级")}

    return {"action": "suggest", "credibility": "S3", "metrics": m,
            "reason": (f"产出 {m['docs_total']} 篇但相关率 {m['relevant_ratio']} 未达 {keep_ratio},"
                       "既不够格自动转正也不够差到淘汰,转人工判断")}


def auto_grade(db, need_id: str, dry_run: bool = False) -> dict:
    """扫描所有试运行源并执行决定。返回各类动作明细,供自动运维报告与页面展示。"""
    if not bool(_cfg().get("auto_grade_enabled", True)):
        return {"skipped": "已在画像 discovery_file 的 grading 里关闭自动定级", "results": []}
    from app.services import discovery
    rows = [s for s in db.query(Source).filter_by(lifecycle="trial").all()
            if need_id in (s.serves_needs or [])]
    out = {"promoted": 0, "retired": 0, "suggested": 0, "extended": 0, "results": []}
    for s in rows:
        d = decide(db, s)
        act = d["action"]
        if act == "retire" and s.discovered_from == "manual":
            act, d["reason"] = "suggest", "人工添加的源不自动淘汰,转人工确认:" + d["reason"]
        if not dry_run:
            if act == "promote":
                discovery.promote(db, s.id, d["credibility"])
                actions.record(
                    db,
                    "source.auto_promote_s1" if d["credibility"] == "S1" else "source.auto_promote",
                    f"「{s.name}」自动转正为 {d['credibility']}:{d['reason']}",
                    need_id=need_id, target=s.entry_url or s.name,
                    detail={"source_id": s.id, "credibility": d["credibility"], **d["metrics"]})
            elif act == "retire":
                s.lifecycle = "retired"
                cfg = dict(s.adapter_config or {})
                cfg["auto_retired_at"] = datetime.utcnow().isoformat(timespec="seconds")
                cfg["auto_graded_out"] = True
                s.adapter_config = cfg
                actions.record(db, "source.auto_graded_out",
                               f"「{s.name}」相关率过低被自动淘汰:{d['reason']}",
                               need_id=need_id, target=s.entry_url or s.name,
                               detail={"source_id": s.id, **d["metrics"]})
            elif act == "suggest":
                cfg = dict(s.adapter_config or {})
                cfg["suggest_credibility"] = d["credibility"]
                cfg["suggest_reason"] = d["reason"][:300]
                s.adapter_config = cfg
            note = f"[自动定级 {datetime.utcnow():%Y-%m-%d}] {d['reason']}"
            s.note = ((s.note or "").split("[自动定级")[0] + note)[:250]
        out[{"promote": "promoted", "retire": "retired",
             "suggest": "suggested", "extend": "extended"}[act]] += 1
        out["results"].append({"id": s.id, "name": s.name, "action": act,
                               "credibility": d["credibility"], "reason": d["reason"],
                               **d["metrics"]})
    if not dry_run:
        db.flush()
    return out


def pending_human(db, need_id: str) -> list[dict]:
    """真正需要人拍板的极少数:自动定级给了建议但没自动执行的源。"""
    out = []
    for s in db.query(Source).filter(Source.lifecycle.in_(["trial", "active"])).all():
        if need_id not in (s.serves_needs or []):
            continue
        cfg = s.adapter_config or {}
        if cfg.get("suggest_credibility"):
            out.append({"id": s.id, "name": s.name, "entry_url": s.entry_url,
                        "current": s.credibility, "suggest": cfg["suggest_credibility"],
                        "reason": cfg.get("suggest_reason", ""),
                        **source_metrics(db, s)})
    return out


def trial_horizon_days() -> int:
    return int(_cfg().get("trial_days", 14) or 14)


def stale_trials(db, need_id: str) -> list[Source]:
    """已过试运行期还挂着 trial 的源(自动定级没跑或跑不动时用于报警)。"""
    horizon = datetime.utcnow() - timedelta(days=trial_horizon_days())
    return [s for s in db.query(Source).filter_by(lifecycle="trial").all()
            if need_id in (s.serves_needs or [])
            and s.trial_started_at and s.trial_started_at < horizon]
