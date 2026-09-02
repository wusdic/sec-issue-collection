"""主流水线(方案 9.1):采集 → 存档 → 去重 → 粗筛 → 抽取 → 记录去重 → 建草稿 → 复核队列。

同时承担搜索行为 B1(事件发现)与源发现 D1/D2/D3 的伴生登记。
"""
import re
import time
from datetime import datetime, timedelta

from dateutil import parser as dtparser
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    CrawlRun, DocCluster, KeywordRun, NeedProfile, RawDocument, SearchWatermark, Source,
)
from app.services import (
    archive, columns, dedup, diagnostics, discovery, fetcher, health, need_ctx, reputation, url_tools,
)
from app.services.adapters import DiscoveredItem, SearchEngineAdapter, get_adapter
from app.services.errors import error_headline
from app.services.events import create_draft
from app.services.extraction import extract_record, load_record_schema, screen_document
from app.services.profiles import get_active_dictionaries

# 分隔符必须含全角「:」(U+FF1A),否则会把冒号一起捕进来,形成「:新华社」与「新华社」两个不同的键
CITATION_RE = re.compile(r"(?:来源|转载自|首发于|原文链接)[:：\s]*([^\s,，、。;；<>\"]{2,60})")
_REF_STOPWORDS = url_tools.REF_STOPWORDS      # 兼容旧名


def _parse_dt(s: str | None):
    if not s:
        return None
    try:
        return dtparser.parse(s, fuzzy=True, ignoretz=True)
    except (ValueError, OverflowError):
        return None


def _pub_date_from(url: str):
    """从 URL 猜发布日期(政务/新闻站路径含日期),返回 date 或 None。"""
    return url_tools.date_from_url(url)


def _as_dt(d):
    """date/datetime → datetime(便于存 published_at)。"""
    if isinstance(d, datetime):
        return d
    return datetime(d.year, d.month, d.day) if d else None


def _too_old(d, ctx=None) -> bool:
    """发布日期早于时效窗口(画像 scope.time_window_days,缺省运行时设置 collect_recency_days)→ True。"""
    days = int(ctx.time_window_days) if ctx is not None else int(getattr(settings, "collect_recency_days", 0) or 0)
    if days <= 0 or d is None:
        return False
    cutoff = (datetime.utcnow() - timedelta(days=days)).date()
    dd = d.date() if isinstance(d, datetime) else d
    return dd < cutoff


def _reputation(need: NeedProfile):
    """取该需求的发布主体信誉名录与转载检测开关(通用能力,见 services/reputation)。"""
    path, repost_on = reputation.registry_path_for(need.config)
    reg = reputation.load_registry(path) if path else None
    return reg, repost_on


def ingest_item(db: Session, need: NeedProfile, source: Source, item: DiscoveredItem,
                crawl_run_id: int | None = None, do_archive: bool = True,
                prefetched: fetcher.FetchResult | None = None,
                stats: dict | None = None) -> RawDocument | None:
    """单条 URL 入库:URL 去重 → 抓取+当刻存档 → 文本提取 → 同稿聚类 → 源发现伴生。

    stats(可选)记录本源本轮:skipped(已采过跳过,增量核心)/blacklist/failed/new。
    """
    def _bump(key):
        if stats is not None:
            stats[key] = stats.get(key, 0) + 1

    reg, repost_on = _reputation(need)
    ctx = need_ctx.for_need(need)
    # 黑名单主体(默认空,仅真正垃圾源)直接丢弃;其余主体一律保留并按名录定级
    if reg is not None and reputation.is_blacklisted(reg, item.wechat_account or item.publisher):
        _bump("blacklist")
        return None
    # 正文抓取渲染偏好:随源配置,默认 auto(httpx 抓到的正文过薄→自动浏览器渲染,需开启渲染开关)
    render_pref = (source.adapter_config or {}).get("render", "auto")
    url = item.url
    if url_tools.is_search_redirect(url):
        fr0 = prefetched or fetcher.fetch(url)  # C3 跳转还原
        url = fr0.final_url if fr0.final_url else url
        prefetched = fr0 if fr0.ok else None
    if dedup.find_existing_url(db, url):
        _bump("skipped")   # 已采过 → 增量跳过(只累加热度,不重复处理)
        return None

    # 时效窗口:发布时间早于近 N 天(默认5年)判为历史,不抓不存,只留一条薄记录供 URL 去重记住
    pub_guess = _parse_dt(item.published) or _pub_date_from(url)
    if pub_guess and _too_old(pub_guess, ctx):
        _bump("too_old")
        db.add(RawDocument(
            need_id=need.id, source_id=source.id, crawl_run_id=crawl_run_id,
            url=url, url_normalized=url_tools.normalize_url(url), final_url=url,
            title=item.title, publisher=item.publisher or item.wechat_account or source.name,
            published_at=_as_dt(pub_guess), content_text=None,
            screen_status="screened_out",
            screen_reason=f"早于时效窗口({ctx.time_window_days}天),历史内容不采集"))
        db.flush()
        return None

    fr = prefetched or fetcher.fetch(url, render=render_pref)
    final_url = fr.final_url or url
    text = archive.extract_text(fr.html) if fr.ok else None
    if not fr.ok:
        _bump("failed")
    else:
        _bump("new")

    # 先建文档(暂不存档),定完首发/转载后再决定存全量还是薄存(去重后存储,省空间)
    doc = RawDocument(
        need_id=need.id, source_id=source.id, crawl_run_id=crawl_run_id,
        url=url, url_normalized=url_tools.normalize_url(url), final_url=final_url,
        title=item.title, publisher=item.publisher or item.wechat_account or source.name,
        published_at=_parse_dt(item.published),
        http_status=fr.status, content_text=text,
        snapshot_id=None,
        screen_status="pending" if text else "screened_out",
        screen_reason=None if text else f"抓取失败: {fr.error or fr.status}",
    )
    db.add(doc)
    db.flush()

    if text:
        dedup.assign_cluster(db, doc)
        # D2 引文/转载溯源(通用)
        for m in CITATION_RE.finditer(text[:5000]):
            ref = (m.group(1) or "").strip(" \t:：、,，。;；「」『』\"'()（）")
            if not ref:
                continue
            if ref.startswith("http"):
                discovery.record_evidence(db, ref, "citation", doc_id=doc.id)
            elif _is_subject_like(ref):
                # 仅在确实像"发布主体名"时才登记为公众号候选。此前 len(ref)<=20 的万能兜底
                # 会把"请注明出处""于原作者或互联网共享平台"等套话当成公众号写进源库。
                discovery.record_evidence(db, None, "wechat_reference",
                                          display_name=ref, wechat_account=ref, doc_id=doc.id)
        # 转载溯源(通用能力,任何需求可用):识别转载→本篇不作首发,原始出处登记候选源
        if reg is not None and repost_on:
            rp = reputation.detect_repost(text)
            if rp["is_repost"]:
                doc.is_primary = False   # 转载版不作首发,优先追原始出处
                subj = (rp["original_subject"] or "").strip(" \t:：、,，。「」『』\"'")
                if subj.startswith("http"):
                    discovery.record_evidence(db, subj, "citation", doc_id=doc.id)
                elif _is_subject_like(subj):
                    discovery.record_evidence(db, None, "citation",
                                              display_name=subj,
                                              wechat_account=subj, doc_id=doc.id)
                for u in (rp["original_wechat_url"], rp["original_url"]):
                    if u:
                        discovery.record_evidence(db, u, "citation", doc_id=doc.id)

    # 去重后存储(广采薄存):首发存完整原文(含图片附件),转载/重复副本只薄存文本,省空间
    if do_archive:
        # 先提交释放 SQLite 写锁:存档要逐个下载图片/附件(单篇最多 archive_max_assets 个),
        # 若攥着上面 INSERT 打开的写事务去做这些网络 I/O,一页 80 篇能占锁好几分钟,
        # 其他并行 worker 全部在写锁上等到 busy_timeout 超时 → "database is locked" 整批崩。
        committed = _safe_commit(db)
        doc = db.get(RawDocument, doc.id) if committed else None
        if doc is None:      # 提交失败=本篇已回滚,别再拿失效实例去存档/写库
            diagnostics.record("note", f"存档前提交失败,跳过本篇:{url}")
            return None
        primary_ref = None
        if not doc.is_primary and doc.cluster_id:
            cluster = db.get(DocCluster, doc.cluster_id)
            primary = db.get(RawDocument, cluster.primary_doc_id) if cluster else None
            primary_ref = primary.snapshot_id if primary else None
        snap = archive.archive_page(db, url, fr=fr, lite=not doc.is_primary,
                                    primary_snapshot_id=primary_ref)
        doc.snapshot_id = snap.snapshot_id
        db.flush()
        source = db.get(Source, source.id) or source   # 提交后按 id 重取,保证后续计数写在活实例上

    # D1/D3 发布方伴生登记
    if item.wechat_account:
        discovery.record_evidence(db, None, "wechat_reference", display_name=item.wechat_account,
                                  wechat_account=item.wechat_account, doc_id=doc.id)
    else:
        discovery.record_evidence(db, final_url, "event_search", doc_id=doc.id,
                                  display_name=item.publisher)
    source.stat_docs_total += 1
    db.flush()
    return doc


def process_document(db: Session, need: NeedProfile, doc: RawDocument) -> dict:
    """粗筛 + 抽取 + 范畴闸门 + 记录级去重 + 建草稿。阶段顺序由画像 pipeline.stages 决定,
    每个阶段是 STAGES 里一个独立函数(也可单独调用,见 capabilities)。"""
    result = {"doc_id": doc.id, "action": None, "event_id": None}
    diagnostics.set_ref(doc.url)  # 后续 LLM/决策留痕自动关联到本文档
    if doc.screen_status == "screened_out":
        result["action"] = "skipped"
        return result
    # 转载(非首发)不再"未读先丢":仍走粗筛+抽取,真同事件由记录级指纹/语义去重合并。
    if not doc.is_primary:
        diagnostics.record("dedup", "同稿簇非首发(转载),仍走粗筛后由记录级去重把关",
                           detail={"cluster_id": doc.cluster_id})
    ctx = need_ctx.for_need(need)
    st = {"payload": None, "extraction": None}
    for name in ctx.pipeline_stages:
        fn = STAGES.get(name)
        if fn is None:
            diagnostics.record("note", f"画像声明了未知阶段 {name},跳过")
            continue
        if fn(db, need, ctx, doc, st, result):
            db.flush()
            return result
    db.flush()
    return result


# ---------------- 处理阶段(每个都是独立可调用的能力) ----------------

def _stage_screen(db, need, ctx, doc, st, result) -> bool:
    verdict = screen_document(need.config, doc.title or "", doc.content_text or "", ctx=ctx)
    conf = verdict["confidence"]
    doc.screen_score = conf
    doc.screen_reason = verdict["reason"]
    diagnostics.record("screen", f"粗筛 {conf:.2f} {'相关' if verdict['is_candidate'] else '不相关'}",
                       detail={"title": doc.title, "confidence": conf,
                               "is_candidate": verdict["is_candidate"], "reason": verdict["reason"],
                               "keep_th": settings.screen_keep_threshold,
                               "manual_th": settings.screen_manual_threshold})
    # 阈值双重把关(可在设置页调严):入选需 is_candidate 且分数≥keep;0.4-0.6 待人工;更低判为不相干淘汰
    if not (verdict["is_candidate"] and conf >= settings.screen_keep_threshold):
        if conf >= settings.screen_manual_threshold:
            doc.screen_status = "manual_queue"
            doc.screen_reason = f"粗筛存疑({conf:.2f}):{verdict['reason']}"
        else:
            doc.screen_status = "screened_out"
            doc.screen_reason = f"判为不相干({conf:.2f}):{verdict['reason']}"
        result["action"] = doc.screen_status
        return True
    doc.screen_status = "screened_in"
    return False


def _stage_extract(db, need, ctx, doc, st, result) -> bool:
    record_schema = ctx.record_schema()
    dictionaries = get_active_dictionaries(db, need.id)
    extraction = extract_record(need.config, dictionaries, record_schema, doc.title or "", doc.content_text or "", ctx=ctx)
    st["extraction"] = extraction
    st["payload"] = extraction["payload"]
    st["dict_version"] = str(dictionaries.get("version") or "")
    diagnostics.record("extract", "结构化抽取完成",
                       detail={"payload": st["payload"], "violations": extraction["violations"],
                               "schema_errors": extraction["schema_errors"]})
    return False


def _stage_scope_gate(db, need, ctx, doc, st, result) -> bool:
    payload = st.get("payload") or {}
    if _is_out_of_scope(payload, doc.title or "", doc.content_text or "", ctx):
        doc.screen_status = "screened_out"
        doc.screen_reason = _out_of_scope_reason(ctx)
        result["action"] = "screened_out"
        diagnostics.record("extract", "判为范畴外,过滤不入库",
                           detail={"record_type": payload.get("record_type"), "title": doc.title})
        return True
    return False


def _stage_content_check(db, need, ctx, doc, st, result) -> bool:
    payload = st.get("payload") or {}
    if not _payload_has_content(payload, ctx):
        doc.screen_status = "manual_queue"
        doc.screen_reason = "抽取结果为空(疑似模型输出异常/正文不足),待人工确认"
        result["action"] = "manual_queue"
        diagnostics.record("extract", "抽取为空,转人工待定(不建记录)",
                           detail={"raw_keys": list(payload.keys())[:20]})
        return True
    return False


def _stage_dedup_record(db, need, ctx, doc, st, result) -> bool:
    payload = st.get("payload") or {}
    existing = dedup.fingerprint_match(db, need.id, payload, ctx=ctx)
    if existing:
        doc.screen_status = "manual_queue"
        doc.screen_reason = f"疑似与 {existing.event_id} 为同一记录(指纹命中),请人工确认合并"
        result["action"] = "merge_suggested"
        result["event_id"] = existing.event_id
        result["extraction"] = st.get("extraction")
        diagnostics.record("dedup", f"指纹命中疑似同记录 {existing.event_id},转人工合并",
                           detail={"matched_event": existing.event_id})
        return True
    return False


def _stage_draft(db, need, ctx, doc, st, result) -> bool:
    payload = st.get("payload")
    if payload is None:                      # 没跑抽取阶段就建草稿:用标题当最小记录
        payload = {"title": doc.title or "", "summary": (doc.content_text or "")[:500]}
    extraction = st.get("extraction") or {"violations": [], "schema_errors": []}
    src = db.get(Source, doc.source_id)
    src_cred = src.credibility if src else "S4"
    # 通用:发布主体命中该需求信誉名录 → 按主体重定级;未命中则保留渠道默认等级(不丢弃)
    reg, _ = _reputation(need)
    if reg is not None and doc.publisher:
        src_cred = reputation.subject_credibility(reg, doc.publisher, src_cred)
    ev = create_draft(db, need.id, payload, doc=doc, source_credibility=src_cred,
                      dict_version=st.get("dict_version") or "", ctx=ctx)
    recall = dedup.semantic_recall(db, need.id, ev.embedding, exclude_event_id=ev.event_id)
    if recall:
        result["semantic_suspects"] = [(e.event_id, round(s, 3)) for e, s in recall]
    if st.get("related_docs"):
        try:
            from app.services import relations
            relations.link(db, ev, st["related_docs"], ctx)
        except Exception:  # noqa: BLE001 关系落库失败不影响建记录
            pass
    result["action"] = "draft_created"
    result["event_id"] = ev.event_id
    result["violations"] = extraction["violations"]
    result["schema_errors"] = extraction["schema_errors"]
    diagnostics.record("draft", f"生成草稿记录 {ev.event_id}(信誉 {src_cred})", ref=ev.event_id,
                       detail={"event_id": ev.event_id, "source_credibility": src_cred,
                               "semantic_suspects": result.get("semantic_suspects"),
                               "violations": extraction["violations"]})
    return True


def _stage_verify(db, need, ctx, doc, st, result) -> bool:
    """真实性验证:官方域/正文哈希/标题一致/密级标记 → doc.verification;含密级标记的只登记不入库。"""
    from app.services import verify
    v = verify.verify_document(doc, ctx)
    diagnostics.record("verify", f"验证 {v['status']}(域 {v['domain_trust']})", detail=v)
    if v.get("sensitive"):
        doc.screen_status = "manual_queue"
        doc.screen_reason = "正文含内部/密级标记:只登记来源,不入库正文,转人工确认"
        result["action"] = "manual_queue"
        return True
    return False


def _stage_relations(db, need, ctx, doc, st, result) -> bool:
    """记录关系抽取(文档型可选阶段):废止/替代/修订/依据 → payload.related_docs(建草稿后再落边)。"""
    from app.services import relations
    payload = st.get("payload")
    if payload is None:
        return False
    rel = relations.extract(doc.content_text or "", ctx, own_title=doc.title or payload.get("title"))
    if rel:
        payload["related_docs"] = rel
        st["related_docs"] = rel
    return False


STAGES = {"screen": _stage_screen, "verify": _stage_verify, "extract": _stage_extract,
          "scope_gate": _stage_scope_gate, "content_check": _stage_content_check,
          "dedup_record": _stage_dedup_record, "relations": _stage_relations, "draft": _stage_draft}


def _early_stop_config(source: Source, adapter) -> tuple[bool, int]:
    """早停开关与阈值。搜索引擎(相关性排序)默认不早停;时间倒序列表/公众号历史早停。
    adapter_config: list_order(time_desc/relevance)、no_early_stop、stop_consecutive 可覆盖。"""
    cfg_order = (source.adapter_config or {}).get("list_order")
    if cfg_order == "time_desc":
        ordered = True
    elif cfg_order == "relevance":
        ordered = False
    else:
        ordered = not isinstance(adapter, SearchEngineAdapter)
    no_early_stop = bool((source.adapter_config or {}).get("no_early_stop"))
    early_enabled = ordered and not no_early_stop
    stop_th = int((source.adapter_config or {}).get("stop_consecutive")
                  or settings.crawl_stop_consecutive_seen)
    return early_enabled, stop_th


def _is_subject_like(ref: str) -> bool:
    return url_tools.is_subject_like(ref)


def _negative_terms(db, need_id: str) -> list[str]:
    """取该需求关键词矩阵里的排除词(降噪)。设置页填了就必须真的生效。"""
    from app.models import KeywordSet
    ks = db.query(KeywordSet).filter_by(need_id=need_id, is_active=True).first()
    if not ks:
        return []
    return [str(t).strip() for t in (ks.content or {}).get("negative_terms") or [] if str(t).strip()]


def _hits_negative(title: str | None, neg: list[str]) -> str | None:
    """标题命中排除词则返回该词(用于日志说明为什么被剔除)。"""
    t = title or ""
    for term in neg:
        # 词条可写成"课程 培训班"表示需同时出现,拆开逐个判
        parts = [p for p in term.split() if p]
        if parts and all(p in t for p in parts):
            return term
    return None


def _safe_commit(db) -> bool:
    """提交并释放写锁;失败则回滚保证会话可继续用(避免后续 flush 抛 PendingRollbackError)。"""
    try:
        db.commit()
        return True
    except Exception:  # noqa: BLE001
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return False


def _item_pub(item: DiscoveredItem):
    """列表项的发布日期(用于时效早停):优先 item.published,回退 URL 日期。"""
    return _parse_dt(item.published) or url_tools.date_from_url(item.url)


_SCOPE_RE_CACHE: dict[str, re.Pattern] = {}


def _rx(pat: str) -> re.Pattern:
    r = _SCOPE_RE_CACHE.get(pat)
    if r is None:
        try:
            r = re.compile(pat)
        except re.error:
            r = re.compile(re.escape(pat))
        _SCOPE_RE_CACHE[pat] = r
    return r


def _out_of_scope_reason(ctx) -> str:
    if ctx.scope_guard.get("out_of_scope_reason"):
        return str(ctx.scope_guard["out_of_scope_reason"])
    from app.services.need_ctx import SCOPE_LABEL
    rm = ctx.require_mention
    if rm:
        return f"未提及范围限定的{'/'.join(SCOPE_LABEL[k] for k in rm)},不入库"
    return f"非『{ctx.name}』范畴,不入库"


def _is_out_of_scope(payload: dict, title: str, text: str, ctx=None) -> bool:
    """范畴闸门(通用):优先信 LLM 的 record_type=画像 out_of_scope 值;再用画像 scope_guard 的
    排除正则兜底——命中排除特征且未命中『强相关覆盖特征』(include_override_patterns)才判范畴外。"""
    c = ctx or need_ctx.get(None, need_ctx.default_need_id())
    oos = c.record_types.get("out_of_scope")
    if oos and isinstance(payload, dict) and payload.get("record_type") == oos:
        return True
    # 范围限定:声明 require_mention 的维度,标题/正文/抽取结果里必须提到其中至少一个词
    for kind in c.require_mention:
        terms = c.scope_terms(kind)
        if not terms:
            continue
        hay = f"{title or ''} {(payload or {}).get('title') or ''} {(text or '')[:20000]} " \
              f"{' '.join(str(v) for v in (payload or {}).values() if isinstance(v, str))}"
        if not any(t in hay for t in terms):
            return True
    sg = c.scope_guard
    excl = [str(x) for x in (sg.get("exclude_patterns") or []) if str(x)]
    if not excl:
        return False
    blob = f"{title or ''} {(payload or {}).get('title') or ''}"
    if not any(_rx(x).search(blob) for x in excl):
        return False
    override = [str(x) for x in (sg.get("include_override_patterns") or []) if str(x)]
    return not any(_rx(x).search(blob) for x in override)


def _payload_has_content(p: dict, ctx=None) -> bool:
    """抽取结果是否有实质内容(至少有标题、或具体主体、或标签要素)。"""
    if not isinstance(p, dict):
        return False
    c = ctx or need_ctx.get(None, need_ctx.default_need_id())
    if str(p.get("title") or c.get_role(p, "title") or "").strip():
        return True
    blank = {str(x) for x in ((c.record_types.get("advisory") or {}).get("subject_blank_values") or [""])}
    subject = str(c.get_role(p, "subject") or "").strip()
    if subject and subject not in blank:
        return True
    return bool(c.get_role(p, "tags_a") or c.get_role(p, "tags_b"))


def _consume_paginated(db, need, source, run, fetch_page, max_pages,
                       early_enabled, stop_th, do_archive, stats, deadline=None, neg=None):
    """逐页消费 + 早停(query/page 共用)。fetch_page(page)->list|None。
    早停信号:连续遇到『已采过』或『早于时效窗口』的条目(时间倒序源);另有单源时长上限。
    返回 (found, pages_used, truncated, snapshot)。"""
    found, pages_used, truncated, snapshot = 0, 0, False, []
    consecutive_stop = 0   # 连续"已采过 或 太旧"计数
    early = False
    for page in range(max_pages):
        if deadline and time.time() > deadline:
            truncated = True  # 超时:该源不再翻页
            break
        page_items = fetch_page(page)
        if not page_items:
            break
        pages_used += 1
        found += len(page_items)
        snapshot += [{"url": i.url, "title": i.title} for i in page_items[:20]]
        page_new = 0
        for item in page_items:
            # 排除词降噪:标题命中即跳过,不抓正文也不喂大模型(设置页"排除词"在此生效)
            hit = _hits_negative(item.title, neg or [])
            if hit:
                stats["excluded"] = stats.get("excluded", 0) + 1
                diagnostics.record("note", f"排除词命中「{hit}」,跳过:{item.title}", ref=item.url)
                continue
            # 时效早停:列表按时间倒序,遇到早于窗口的历史条目 → 不抓,计入连续停止信号
            if _too_old(_item_pub(item)):
                stats["too_old"] = stats.get("too_old", 0) + 1
                consecutive_stop += 1
                if early_enabled and consecutive_stop >= stop_th:
                    early = True
                    break
                continue
            prev_skip = stats["skipped"]
            ingest_item(db, need, source, item, run.id, do_archive=do_archive, stats=stats)
            if stats["skipped"] > prev_skip:          # 已采过
                consecutive_stop += 1
                if early_enabled and consecutive_stop >= stop_th:
                    early = True
                    break
            else:                                      # 新增(或失败)→ 重置连续计数
                consecutive_stop = 0
                page_new += 1
        _safe_commit(db)   # 每页落盘并释放写锁,避免长事务阻塞其他并行 worker
        if early:
            break
        if early_enabled and page_new == 0 and page_items:
            early = True  # 整页无新增 → 后续更旧,停翻
            break
        if page < max_pages - 1:
            time.sleep(settings.crawl_delay_seconds)
    if pages_used == max_pages and not early:
        truncated = True  # 翻满仍未遇到重复区,可能还有更多
    return found, pages_used, truncated, snapshot


def _run_queries(db, need, source, run, adapter, queries, behavior, max_pages,
                 early_enabled, stop_th, do_archive, stats, deadline, neg) -> int:
    """检索型采集:逐关键词翻页取结果并记账。返回本源命中条数。

    抽成函数是为了让"根域源没识别到栏目"时也能走同一套站内检索兜底逻辑。
    """
    has_pager = hasattr(adapter, "search_page")
    # 搜索型源限流:关键词截到上限,避免 400 词硬打慢站空跑几十分钟
    cap = int(getattr(settings, "search_source_query_cap", 0) or 0)
    qlist = (queries or [])[:cap] if cap > 0 else (queries or [])
    found = 0
    for q in qlist:
        if deadline and time.time() > deadline:
            break  # 单源超时:放弃剩余关键词
        qh = url_tools.query_hash(q)
        wm = db.get(SearchWatermark, (source.id, qh))
        before = stats["new"]

        def fetch_page(page, _q=q):
            if has_pager:
                return adapter.search_page(_q, page)
            return adapter.search(_q, max_pages=1)[0] if page == 0 else None

        q_found, pages_used, truncated, snapshot = _consume_paginated(
            db, need, source, run, fetch_page, max_pages, early_enabled, stop_th,
            do_archive, stats, deadline, neg)
        db.add(KeywordRun(need_id=need.id, source_id=source.id, behavior=behavior,
                          query=q, pages_fetched=pages_used, truncated=truncated,
                          results=q_found, new_docs=stats["new"] - before,
                          result_snapshot=snapshot[:50]))
        found += q_found
        if wm:
            wm.last_ran_at = datetime.utcnow()
        else:
            db.add(SearchWatermark(source_id=source.id, query_hash=qh,
                                   last_ran_at=datetime.utcnow()))
    return found


def _root_fallback(db, need, source, run, queries, behavior, max_pages,
                   early_enabled, stop_th, do_archive, stats, deadline, neg) -> int:
    """根域源识别不到有效栏目时的兜底(默认转站内检索,而不是抓首页)。

    抓首页拿到的是"要闻/领导活动"这类与需求无关的内容(用户反馈的"习近平会见…"就是这么进来的);
    站内检索用 site:域名 + 需求关键词,同样只用一次网络往返,却能精准圈出该站的相关页面集合。
    行为可用 root_no_column_fallback 配置:search(默认)/ root(旧行为)/ skip。
    """
    mode = str(getattr(settings, "root_no_column_fallback", "search") or "search").lower()
    if mode == "skip":
        diagnostics.record("note", f"源「{source.name}」未定位到相关栏目,按配置跳过(不抓根页)")
        return 0
    if mode == "root":
        found, _pu, _tr, _sn = _consume_paginated(
            db, need, source, run, lambda page: get_adapter(source).discover_page(page),
            max_pages, early_enabled, stop_th, do_archive, stats, deadline, neg)
        return found
    domain = url_tools.identity_key_for(source.entry_url or "")
    if not domain or domain.startswith("mp:"):
        return 0
    sibling = _site_search_sibling(db, source, domain)
    if sibling is None:
        return 0
    # 建完立刻提交:否则这条 INSERT 会把 SQLite 写锁一直攥到下一次提交,
    # 而后面是几十次搜索引擎网络往返,期间其他并行 worker 全部卡死在写锁上(database is locked)。
    _safe_commit(db)
    sibling = db.get(Source, sibling.id) or sibling
    run = db.get(CrawlRun, run.id) or run      # 回滚后按 id 重取,避免用到已失效的实例
    diagnostics.record("note",
                       f"源「{source.name}」未定位到相关栏目 → 转站内检索 site:{domain}(不抓根页,避免首页噪声)")
    found = _run_queries(db, need, sibling, run, get_adapter(sibling), queries, behavior,
                         max_pages, early_enabled, stop_th, do_archive, stats, deadline, neg)
    if found:
        sibling.last_success_at = datetime.utcnow()
    return found


def _site_search_sibling(db, source: Source, domain: str) -> Source | None:
    """取/建该站的『站内检索』兄弟源(site:域名),供根域兜底与后续独立调度复用。"""
    ident = f"site:{domain}"
    sib = db.query(Source).filter_by(identity_key=ident).one_or_none()
    if sib is not None:
        if sib.lifecycle == "retired":
            sib.lifecycle = "active"
        return sib
    sib = Source(
        name=f"{source.name}·站内检索", entry_url=None, kind="query", adapter="baidu_search",
        adapter_config={"site": domain, "list_order": "relevance",
                        "parent_site_id": source.id},
        credibility=source.credibility, tier=source.tier, lifecycle="active",
        serves_needs=list(source.serves_needs or []), identity_key=ident, site_key=domain,
        discovered_from="root_fallback",
        note=f"根域源「{source.name}」未定位到相关栏目,自动转站内检索精准定位相关页面")
    db.add(sib)
    try:
        db.flush()
    except Exception:  # noqa: BLE001 并发下可能被别的 worker 抢先建了同一条
        db.rollback()
        return db.query(Source).filter_by(identity_key=ident).one_or_none()
    return sib


def crawl_source(db: Session, need: NeedProfile, source: Source,
                 queries: list[str] | None = None, behavior: str = "B1",
                 max_pages: int = 1, do_archive: bool = True) -> CrawlRun:
    """执行一个源的抓取(页面型 discover / 查询型 search),含翻页早停增量与截断上报。"""
    run = CrawlRun(source_id=source.id)
    db.add(run)
    db.flush()
    # 立刻提交释放写锁:SQLite 只允许一个写事务,若整个源抓完才提交,其他并行 worker 会在
    # 第一次写时等锁超时直接 "database is locked"(实测 5 并发会失败 4 个)。
    _safe_commit(db)
    run_id, source_id = run.id, source.id   # 回滚后按 id 重取,避免用到已失效的实例
    adapter = get_adapter(source)
    stats = {"new": 0, "skipped": 0, "failed": 0, "blacklist": 0, "too_old": 0}
    found = 0
    early_enabled, stop_th = _early_stop_config(source, adapter)
    neg = _negative_terms(db, need.id)
    budget = int(getattr(settings, "source_time_budget_seconds", 0) or 0)
    deadline = (time.time() + budget) if budget > 0 else None
    try:
        # 批次内浏览器实例复用:本源所有需渲染的页面共用一个浏览器(嵌套则复用上层 job 会话)
        with fetcher.render_session():
            if source.kind == "query":
                found += _run_queries(db, need, source, run, adapter, queries, behavior,
                                      max_pages, early_enabled, stop_th, do_archive,
                                      stats, deadline, neg)
            elif columns.is_root_only(source.entry_url):
                # 根域页面型源:不抓首页要闻,自动发现并持久化相关栏目为子源,分别抓;
                # 栏目记录 TTL 内复用不重算(应对动态站,过期才重识别验证)。
                children, recomputed = columns.discover_and_persist(db, source)
                diagnostics.record("note",
                                   f"根域源栏目:{len(children)} 个子栏目"
                                   f"({'本次重新识别' if recomputed else '复用已记录'})",
                                   detail={"columns": [c.entry_url for c in children]})
                if children:
                    for child in children:
                        if deadline and time.time() > deadline:
                            break
                        ca = get_adapter(child)
                        f, _pu, _tr, _sn = _consume_paginated(
                            db, need, child, run, lambda page, a=ca: a.discover_page(page),
                            max_pages, early_enabled, stop_th, do_archive, stats, deadline, neg)
                        found += f
                        child.last_success_at = datetime.utcnow()
                else:
                    found += _root_fallback(db, need, source, run, queries, behavior, max_pages,
                                            early_enabled, stop_th, do_archive, stats, deadline, neg)
            else:
                # 页面型:官方栏目/公众号历史列表按时间倒序,支持翻页 + 早停(默认早停开启)
                found, _pu, _tr, _sn = _consume_paginated(
                    db, need, source, run, lambda page: adapter.discover_page(page),
                    max_pages, early_enabled, stop_th, do_archive, stats, deadline, neg)
        run.status = "ok"
        if found > 0:
            health.register_success(db, source)
        else:
            # 解析出 0 条不是"成功"(选择器失效/被反爬也长这样),计入源健康;
            # 但判死留足冗余度:见 health.register_failure —— 还要距上次成功超过容忍天数,
            # 且 S1/S2 官方源永不自动停用,否则低频权威源会被"连续 3 天没新稿"误杀。
            v = health.register_failure(db, source, "本轮解析出 0 条")
            run.error = (run.error or "") + "[本轮解析出 0 条,计入源健康]"
            if v["note"]:
                run.error += f"[{v['note']}]"
    except Exception as e:  # noqa: BLE001 单源失败不拖垮批次
        err = error_headline(e, 500)
        # 先回滚:失败可能来自 flush(唯一约束/锁超时),会话已中毒,不回滚则后续写全部抛
        # PendingRollbackError,真实错因被掩盖、fail_streak 也白加。
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        run = db.get(CrawlRun, run_id) or run
        source = db.get(Source, source_id) or source
        run.status = "failed"
        run.error = err
        v = health.register_failure(db, source, err)   # 同一套带冗余度的判定,不轻易判死
        if v["note"]:
            run.error = (err + f" [{v['note']}]")[:500]
    run.urls_found = found
    run.urls_new = stats["new"]
    run.urls_skipped = stats["skipped"]
    run.urls_failed = stats["failed"]
    run.finished_at = datetime.utcnow()
    _safe_commit(db)
    return run
