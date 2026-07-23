"""源发现引擎(详细设计 §8):D1-D6 证据登记、候选评分、自动 trial、黑名单。"""
from datetime import datetime, timedelta

import yaml
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Source, SourceBlacklist, SourceDiscoveryEvidence
from app.services import url_tools


def _load_scoring() -> dict:
    try:
        with open(settings.config_dir / "discovery.yaml", encoding="utf-8") as f:
            return yaml.safe_load(f).get("scoring", {})
    except FileNotFoundError:
        return {}


def record_evidence(db: Session, url: str | None, channel: str,
                    display_name: str | None = None, wechat_account: str | None = None,
                    doc_id: int | None = None, was_cluster_primary: bool = False) -> str | None:
    """登记一次候选源证据(D1-D6 通道统一入口)。返回 identity_key,已注册/黑名单返回 None。"""
    if not url and not wechat_account:
        return None
    key = url_tools.identity_key_for(url or "", wechat_account)
    if not key or key in ("baidu.com", "bing.com", "sogou.com", "weibo.com"):
        return None  # C3 未还原的搜索引擎域名不计
    if db.get(SourceBlacklist, key):
        return None
    if db.query(Source).filter_by(site_key=key).first():
        return None  # 该站点已有源(任一栏目)→ 不再当候选
    ev = (
        db.query(SourceDiscoveryEvidence)
        .filter_by(identity_key=key, channel=channel)
        .one_or_none()
    )
    if ev:
        ev.hit_count += 1
        ev.last_seen = datetime.utcnow()
        ev.was_cluster_primary = ev.was_cluster_primary or was_cluster_primary
        if display_name:
            ev.display_name = display_name
    else:
        db.add(SourceDiscoveryEvidence(
            identity_key=key, display_name=display_name,
            kind_guess="wechat_mp" if wechat_account else "website",
            channel=channel, evidence_doc_id=doc_id, evidence_url=url,
            was_cluster_primary=was_cluster_primary,
        ))
    db.flush()
    return key


def candidate_score(db: Session, identity_key: str, llm_relevance: float = 0.0) -> float:
    """评分公式(8.2):2×通道数 + 0.5×30天命中(封顶10) + 3×曾首发 + 2×LLM相关度 + 1×活跃度。"""
    w = _load_scoring()
    rows = db.query(SourceDiscoveryEvidence).filter_by(identity_key=identity_key).all()
    if not rows:
        return 0.0
    channels = len({r.channel for r in rows})
    since = datetime.utcnow() - timedelta(days=30)
    hits30 = min(10, sum(r.hit_count for r in rows if r.last_seen >= since))
    primary = any(r.was_cluster_primary for r in rows)
    fresh = 1.0 if any(r.last_seen >= datetime.utcnow() - timedelta(days=7) for r in rows) else 0.0
    return round(
        float(w.get("weight_channels", 2.0)) * channels
        + float(w.get("weight_hits30d", 0.5)) * hits30
        + float(w.get("weight_cluster_primary", 3.0)) * (1 if primary else 0)
        + float(w.get("weight_llm_relevance", 2.0)) * llm_relevance
        + float(w.get("weight_freshness", 1.0)) * fresh,
        2,
    )


def evaluate_candidates(db: Session, need_id: str, llm_scores: dict[str, float] | None = None) -> list[dict]:
    """日任务/每轮采集收尾:候选池评分,≥阈值自动建 trial 源(自动入库,转正仍需人工定级)。

    阈值优先取运行时设置 settings.discovery_auto_trial_threshold(设置页可调),
    留空/0 才回退 discovery.yaml 的 auto_trial_threshold。调低→自动入库更激进。
    """
    llm_scores = llm_scores or {}
    threshold = float(getattr(settings, "discovery_auto_trial_threshold", 0)
                      or _load_scoring().get("auto_trial_threshold", 8.0))
    keys = {r.identity_key for r in db.query(SourceDiscoveryEvidence).all()}
    results = []
    for key in keys:
        if db.query(Source).filter_by(site_key=key).first():
            continue  # 该站点已有源(任一栏目)→ 不重复建
        score = candidate_score(db, key, llm_scores.get(key, 0.0))
        item = {"identity_key": key, "score": score, "auto_trial": False}
        if score >= threshold:
            rows = db.query(SourceDiscoveryEvidence).filter_by(identity_key=key).all()
            display = next((r.display_name for r in rows if r.display_name), key)
            is_mp = key.startswith("mp:")
            entry = None if is_mp else f"https://{key}/"
            _sk, ident = url_tools.source_keys(
                "query" if is_mp else "page", entry,
                {"account": key[3:]} if is_mp else {})
            db.add(Source(
                name=f"[候选]{display}",
                identity_key=ident, site_key=key, discovery_score=score,
                entry_url=entry,
                kind="query" if is_mp else "page",
                adapter="sogou_wechat" if is_mp else "generic_rss",
                adapter_config={"account": key[3:]} if is_mp else {},
                credibility="S4",  # 候选一律 S4,转正人工定级
                lifecycle="trial", serves_needs=[need_id],
                discovered_from="discovery", trial_started_at=datetime.utcnow(),
            ))
            item["auto_trial"] = True
            item["name"] = display
        results.append(item)
    db.flush()
    return sorted(results, key=lambda x: -x["score"])


def recompute_keys(db: Session) -> int:
    """回填/校正所有源的 site_key 与 identity_key(目标键)。identity_key 冲突时不覆盖,避免破坏唯一性。"""
    updated = 0
    for s in db.query(Source).all():
        sk, ik = url_tools.source_keys(s.kind, s.entry_url, s.adapter_config)
        changed = False
        if s.site_key != sk:
            s.site_key = sk
            changed = True
        if ik and s.identity_key != ik:
            clash = db.query(Source).filter(Source.identity_key == ik, Source.id != s.id).first()
            if not clash:
                s.identity_key = ik
                changed = True
        if changed:
            updated += 1
    db.flush()
    return updated


def duplicate_groups(db: Session, need_id: str | None = None) -> list[dict]:
    """按 site_key 分组,列出同一站点下的多个源(栏目)。同站不同栏目属正常(各自采集);
    只有同一 identity_key(同栏目)的多条才是真重复,用 has_exact_duplicate 标出。"""
    from collections import defaultdict
    groups: dict[str, list[Source]] = defaultdict(list)
    for s in db.query(Source).filter(Source.site_key.isnot(None)).all():
        if need_id and need_id not in (s.serves_needs or []):
            continue
        groups[s.site_key].append(s)
    out = []
    for site, srcs in groups.items():
        if len(srcs) < 2:
            continue
        by_target: dict[str, list[Source]] = defaultdict(list)
        for s in srcs:
            by_target[s.identity_key or f"__none__{s.id}"].append(s)
        out.append({
            "site_key": site,
            "has_exact_duplicate": any(len(v) > 1 for v in by_target.values()),
            "sources": [{"id": s.id, "name": s.name, "entry_url": s.entry_url,
                         "kind": s.kind, "identity_key": s.identity_key,
                         "lifecycle": s.lifecycle, "docs_total": s.stat_docs_total,
                         "discovered_from": s.discovered_from} for s in srcs],
        })
    return sorted(out, key=lambda x: (-int(x["has_exact_duplicate"]), -len(x["sources"])))


def blacklist(db: Session, identity_key: str, reason: str, by_user: int | None = None):
    if not db.get(SourceBlacklist, identity_key):
        db.add(SourceBlacklist(identity_key=identity_key, reason=reason, by_user=by_user))
    db.query(SourceDiscoveryEvidence).filter_by(identity_key=identity_key).delete()
    db.flush()


def promote(db: Session, source_id: int, credibility: str, by_user: int | None = None) -> Source:
    """转正:人工确认可信度等级(生命线,不自动)。"""
    src = db.get(Source, source_id)
    src.lifecycle = "active"
    src.credibility = credibility
    src.discovery_score = None
    if src.name.startswith("[候选]"):
        src.name = src.name[4:]
    db.flush()
    return src


def trial_report(db: Session, source_id: int) -> dict:
    """试运行报告:原创率/文档量(转正评审依据)。"""
    from app.models import RawDocument
    src = db.get(Source, source_id)
    docs = db.query(RawDocument).filter_by(source_id=source_id).all()
    primary = sum(1 for d in docs if d.is_primary)
    return {
        "source": src.name, "lifecycle": src.lifecycle,
        "docs_total": len(docs),
        "firsthand": primary,
        "originality": round(primary / len(docs), 2) if docs else 0.0,
        "trial_days": (datetime.utcnow() - src.trial_started_at).days if src.trial_started_at else None,
    }
