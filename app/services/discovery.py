"""源发现引擎(详细设计 §8):D1-D6 证据登记、候选评分、自动 trial、黑名单。"""
from datetime import datetime, timedelta

import yaml
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Source, SourceBlacklist, SourceDiscoveryEvidence
from app.services import url_tools


def _load_scoring(need_id: str | None = None) -> dict:
    """候选评分权重:画像 sources.discovery_scoring,缺省取画像 discovery_file 的 scoring 节。"""
    from app.services import need_ctx
    return need_ctx.get(None, need_id or need_ctx.default_need_id()).discovery_scoring


import re as _re

# 明显不是"发布主体名"的候选(纯日期/纯数字/过短/全符号)——防把日期当成源入库
_DATE_LIKE = _re.compile(r"^\s*(?:\d{4}[-/年.]?\d{0,2}[-/月.]?\d{0,2}日?|\d{1,4})\s*$")


def _valid_subject(name: str | None) -> bool:
    """候选主体名是否可信。除纯日期/过短外,还要排除正文套话碎片与 URL(否则会把
    「请注明出处」「于原作者或互联网共享平台」之类当成公众号写进源库)。"""
    if not name:
        return False
    s = name.strip(" \t:：、,，。;；「」『』\"'()（）")
    if len(s) < 3:
        return False
    if _DATE_LIKE.match(s):
        return False
    if not _re.search(r"[一-鿿A-Za-z]", s):  # 至少含一个中英文字
        return False
    from app.services.url_tools import is_subject_like as _is_subject_like
    return _is_subject_like(s)


def record_evidence(db: Session, url: str | None, channel: str,
                    display_name: str | None = None, wechat_account: str | None = None,
                    doc_id: int | None = None, was_cluster_primary: bool = False,
                    platform_key: str | None = None, found_by_query: str | None = None) -> str | None:
    """登记一次候选源证据(D1-D6 通道统一入口)。返回 identity_key,已注册/黑名单返回 None。

    platform_key:百家号/微博等"平台内的号"(bjh:/wb:)。按注册域算它们会退化成
    baidu.com/weibo.com 并被通用大平台规则丢掉,故由调用方直接给出号级身份键。
    """
    if not url and not wechat_account and not platform_key:
        return None
    # 公众号/引文主体名做校验:纯日期/数字/过短的不当候选源(F5)
    if wechat_account and not _valid_subject(wechat_account):
        return None
    if not url and not platform_key and display_name and not _valid_subject(display_name):
        return None
    key = platform_key or url_tools.identity_key_for(url or "", wechat_account)
    if not key or key in ("baidu.com", "bing.com", "sogou.com", "weibo.com"):
        return None  # C3 未还原的搜索引擎域名不计
    if db.get(SourceBlacklist, key):
        return None
    if db.query(Source).filter_by(site_key=key).first():
        return None  # 该站点已有源(任一栏目)→ 不再当候选
    # 用 first() 而非 one_or_none():并发下 check-then-insert 可能留下同 (key, channel) 重复行,
    # one_or_none 会抛 MultipleResultsFound,并使此后每轮采集碰到该域名都失败(持久性损坏)。
    ev = (
        db.query(SourceDiscoveryEvidence)
        .filter_by(identity_key=key, channel=channel)
        .order_by(SourceDiscoveryEvidence.id)
        .first()
    )
    if ev:
        ev.hit_count += 1
        ev.last_seen = datetime.utcnow()
        ev.was_cluster_primary = ev.was_cluster_primary or was_cluster_primary
        if display_name:
            ev.display_name = display_name
        if found_by_query and not ev.found_by_query:
            ev.found_by_query = found_by_query
    else:
        db.add(SourceDiscoveryEvidence(
            identity_key=key, display_name=display_name,
            kind_guess="wechat_mp" if wechat_account else "website",
            channel=channel, evidence_doc_id=doc_id, evidence_url=url,
            was_cluster_primary=was_cluster_primary, found_by_query=found_by_query,
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


# 搜索结果标题里常见的站名分隔符:取最后一段通常就是站名("XX通报_湖南省互联网..." → 站名)
_TITLE_SPLIT = _re.compile(r"\s*[_|\-–—»·]\s*")


def candidate_name(db: Session, key: str) -> str:
    """候选的展示名:要的是**渠道名**,不是某篇文章的标题。

    优先级:初评时抓到的站点 <title> → 证据里的名字(公众号名可靠;网站的多是文章标题,
    退而取其中的站名段)→ 键本身。此前直接用文章标题,候选池里会出现
    "什么是网警?-安康市公安局""湘西州2026年上半年网络生态治理情况通报_湖南省互联网违法和不良..."
    这种一看不知道是什么渠道的名字。
    """
    from app.models import SourceProbe
    probe = db.get(SourceProbe, key)
    if probe and (probe.site_title or "").strip():
        return probe.site_title.strip()[:80]
    rows = db.query(SourceDiscoveryEvidence).filter_by(identity_key=key).all()
    raw = next((r.display_name for r in rows if r.display_name), None)
    if not raw:
        return key
    if key.startswith(("mp:", "bjh:", "wb:")):
        return raw[:80]                       # 平台号:证据里的名字就是号名,直接用
    # 网站:从文章标题里挑最像站名的一段(最后一段、长度适中、不含标点句式)
    parts = [p.strip() for p in _TITLE_SPLIT.split(raw) if 2 <= len(p.strip()) <= 30]
    for p in reversed(parts):
        if not _re.search(r"[?？!!。,,]", p):
            return p[:80]
    return key


def create_from_candidate(db: Session, key: str, need_id: str, score: float | None = None) -> Source:
    """候选键 → 建成 trial 源(自动入库与人工"收下"共用同一份构造逻辑)。"""
    display = candidate_name(db, key)
    is_mp = key.startswith("mp:")
    plat_entry = url_tools.platform_entry_url(key)   # bjh:/wb: → 该号的主页(就是文章列表)
    entry = None if is_mp else (plat_entry or f"https://{key}/")
    if is_mp:
        ident, kind, adapter, cfg = key, "query", "sogou_wechat", {"account": key[3:]}
    elif plat_entry:
        # 平台号主页多为 JS 渲染,用通用列表适配器 + auto 渲染;身份键就是号本身
        ident, kind, adapter, cfg = key, "page", "generic_list", {"render": "auto"}
    else:
        _sk, ident = url_tools.source_keys("page", entry, {})
        kind, adapter, cfg = "page", "generic_rss", {}
    src = Source(
        name=f"[候选]{display}", identity_key=ident, site_key=key, discovery_score=score,
        entry_url=entry, kind=kind, adapter=adapter, adapter_config=cfg,
        credibility="S4",  # 候选一律 S4,转正人工定级
        lifecycle="trial", serves_needs=[need_id],
        discovered_from="discovery", trial_started_at=datetime.utcnow(),
    )
    db.add(src)
    db.flush()
    return src


def evaluate_candidates(db: Session, need_id: str, llm_scores: dict[str, float] | None = None) -> list[dict]:
    """日任务/每轮采集收尾:候选池评分,≥阈值自动建 trial 源(自动入库,转正仍需人工定级)。

    阈值优先取运行时设置 settings.discovery_auto_trial_threshold(设置页可调),
    留空/0 才回退 discovery.yaml 的 auto_trial_threshold。调低→自动入库更激进。
    """
    from app.services import actions
    llm_scores = llm_scores or {}
    auto_added: list[str] = []
    threshold = float(getattr(settings, "discovery_auto_trial_threshold", 0)
                      or _load_scoring(need_id).get("auto_trial_threshold", 8.0))
    keys = {r.identity_key for r in db.query(SourceDiscoveryEvidence).all()}
    results = []
    for key in keys:
        if db.query(Source).filter_by(site_key=key).first():
            continue  # 该站点已有源(任一栏目)→ 不重复建
        score = candidate_score(db, key, llm_scores.get(key, 0.0))
        item = {"identity_key": key, "score": score, "auto_trial": False}
        rows_all = db.query(SourceDiscoveryEvidence).filter_by(identity_key=key).all()
        # 硬闸门:单通道的孤证(常是正文里偶然出现的名字)不自动入库。
        # 但"主动找源命中 + LLM 初评判定确实持续产出安全内容"本身就是有意的双重证据,
        # 否则主动找源找到的渠道永远卡在候选池进不了库(实测一轮 4 个候选、入库 0 个)。
        probe_pass = float(getattr(settings, "discovery_probe_pass", 0) or 0)
        multi = (len({r.channel for r in rows_all}) >= 2
                 or any(r.was_cluster_primary for r in rows_all)
                 or (probe_pass > 0 and llm_scores.get(key, 0.0) >= probe_pass
                     and any(r.channel == "source_search" for r in rows_all)))
        if score >= threshold and multi:
            src = create_from_candidate(db, key, need_id, score)
            _credit_query(db, need_id, rows_all)
            item["auto_trial"] = True
            item["name"] = src.name.replace("[候选]", "")
            item["source_id"] = src.id
            auto_added.append(item["name"])
        results.append(item)
    db.flush()
    if auto_added:
        actions.record(db, "source.auto_trial",
                       f"新源自动入库试运行 {len(auto_added)} 个(S4 待定级):" + "、".join(auto_added[:10]),
                       need_id=need_id, count=len(auto_added), detail={"names": auto_added[:50]})
    return sorted(results, key=lambda x: -x["score"])


def _credit_query(db: Session, need_id: str, rows) -> None:
    """候选真的进了源库 —— 把功劳记回当初捞到它的那条找源词(进化机制最强的正反馈)。"""
    q = next((r.found_by_query for r in rows if r.found_by_query), None)
    if not q:
        return
    try:
        from app.services import query_evolution
        query_evolution.attribute_admitted(db, need_id, q)
    except Exception:  # noqa: BLE001 回填失败不该挡住入库
        pass


def prune_candidates(db: Session, need_id: str) -> dict:
    """自动清理候选池:已初评且明确不相关、又很久没再出现的候选,直接清掉。

    候选池不清理就会无限膨胀,人一看几百条就更不想看了——那自动化等于没做完。
    只清"已初评 + 相关度明确偏低 + 长期没新证据"这三条同时成立的,拿不准的一律留着。
    """
    rel_floor = float(getattr(settings, "candidate_prune_relevance", 0) or 0)
    stale_days = int(getattr(settings, "candidate_prune_days", 0) or 0)
    if rel_floor <= 0 or stale_days <= 0:
        return {"pruned": 0, "skipped": "已关闭候选自动清理"}
    from app.models import SourceProbe
    cutoff = datetime.utcnow() - timedelta(days=stale_days)
    pruned = []
    keys = {r.identity_key for r in db.query(SourceDiscoveryEvidence).all()}
    for key in keys:
        if db.query(Source).filter_by(site_key=key).first():
            continue                                   # 已建成源的不动
        probe = db.get(SourceProbe, key)
        if not probe or not probe.ok or probe.relevance >= rel_floor:
            continue                                   # 没初评 / 初评失败 / 分不低 → 留着
        rows = db.query(SourceDiscoveryEvidence).filter_by(identity_key=key).all()
        if any(r.last_seen > cutoff for r in rows):
            continue                                   # 最近还在出现 → 留着再看看
        for r in rows:
            db.delete(r)
        pruned.append(key)
    if pruned:
        from app.services import actions
        actions.record(db, "source.candidates_pruned",
                       f"候选池自动清理 {len(pruned)} 个:已初评判定不相关(<{rel_floor})"
                       f"且超过 {stale_days} 天没再出现",
                       need_id=need_id, count=len(pruned), detail={"keys": pruned[:50]})
    db.flush()
    return {"pruned": len(pruned), "keys": pruned[:50]}


def recompute_keys(db: Session) -> dict:
    """回填/校正所有源的 site_key 与 identity_key,并自动查重:目标键(identity_key)相同的多个源
    即同一采集目标(真重复),自动合并——保留文档最多者,其余转停用并并入其服务需求。

    先分组再分配,天然保证 identity_key 唯一,不会再触发唯一约束 500。
    """
    from collections import defaultdict
    srcs = db.query(Source).all()
    plan = {s.id: url_tools.source_keys(s.kind, s.entry_url, s.adapter_config) for s in srcs}
    by_ik: dict[str, list] = defaultdict(list)
    for s in srcs:
        sk, ik = plan[s.id]
        s.site_key = sk               # 站点键不唯一,直接更新
        if ik:
            by_ik[ik].append(s)
    # 先全部清空目标键,避免旧值与新分配相互冲突
    for s in srcs:
        s.identity_key = None
    db.flush()
    merged = 0
    for ik, group in by_ik.items():
        # 保留者:文档最多 → 未停用 → id 最小
        keeper = sorted(group, key=lambda s: (-(s.stat_docs_total or 0),
                                              s.lifecycle == "retired", s.id))[0]
        keeper.identity_key = ik
        for s in group:
            if s is keeper:
                continue
            keeper.serves_needs = sorted(set(keeper.serves_needs or []) | set(s.serves_needs or []))
            cfg = dict(s.adapter_config or {})
            if s.lifecycle != "retired":
                # 尊重人工:曾被自动并掉、用户又手动启用的源不再反复停用(此前每次重启都会被并掉)
                if cfg.get("auto_merged"):
                    continue
                s.lifecycle = "retired"
                cfg["auto_merged"] = True
                s.adapter_config = cfg
                tag = " [自动查重:并入同采集目标的源]"
                if tag.strip() not in (s.note or ""):        # 备注只追加一次,避免重启刷屏
                    s.note = ((s.note or "") + tag)[:250]
            merged += 1
    db.flush()
    if merged:
        from app.services import actions
        actions.record(db, "source.auto_merge",
                       f"查重整理:{merged} 个重复源自动并入同采集目标的源(被并方转停用,可恢复)",
                       count=merged, detail={"updated": len(srcs), "merged": merged})
    return {"updated": len(srcs), "merged": merged}


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
