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
from app.services import archive, columns, dedup, diagnostics, discovery, fetcher, reputation, url_tools
from app.services.adapters import DiscoveredItem, SearchEngineAdapter, get_adapter
from app.services.events import create_draft
from app.services.extraction import extract_record, load_record_schema, screen_document
from app.services.profiles import get_active_dictionaries

CITATION_RE = re.compile(r"(?:来源|转载自|首发于|原文链接)[::\s]*([^\s,。;<>\"]{2,60})")


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


def _too_old(d) -> bool:
    """发布日期早于时效窗口(近 collect_recency_days 天)→ True。"""
    days = int(getattr(settings, "collect_recency_days", 0) or 0)
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
    if pub_guess and _too_old(pub_guess):
        _bump("too_old")
        db.add(RawDocument(
            need_id=need.id, source_id=source.id, crawl_run_id=crawl_run_id,
            url=url, url_normalized=url_tools.normalize_url(url), final_url=url,
            title=item.title, publisher=item.publisher or item.wechat_account or source.name,
            published_at=_as_dt(pub_guess), content_text=None,
            screen_status="screened_out",
            screen_reason=f"早于时效窗口({settings.collect_recency_days}天),历史内容不采集"))
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
            ref = m.group(1)
            if ref.startswith("http"):
                discovery.record_evidence(db, ref, "citation", doc_id=doc.id)
            elif "公众号" in ref or len(ref) <= 20:
                discovery.record_evidence(db, None, "wechat_reference",
                                          display_name=ref, wechat_account=ref, doc_id=doc.id)
        # 转载溯源(通用能力,任何需求可用):识别转载→本篇不作首发,原始出处登记候选源
        if reg is not None and repost_on:
            rp = reputation.detect_repost(text)
            if rp["is_repost"]:
                doc.is_primary = False   # 转载版不作首发,优先追原始出处
                if rp["original_subject"]:
                    discovery.record_evidence(db, None, "citation",
                                              display_name=rp["original_subject"],
                                              wechat_account=rp["original_subject"], doc_id=doc.id)
                for u in (rp["original_wechat_url"], rp["original_url"]):
                    if u:
                        discovery.record_evidence(db, u, "citation", doc_id=doc.id)

    # 去重后存储(广采薄存):首发存完整原文(含图片附件),转载/重复副本只薄存文本,省空间
    if do_archive:
        primary_ref = None
        if not doc.is_primary and doc.cluster_id:
            cluster = db.get(DocCluster, doc.cluster_id)
            primary = db.get(RawDocument, cluster.primary_doc_id) if cluster else None
            primary_ref = primary.snapshot_id if primary else None
        snap = archive.archive_page(db, url, fr=fr, lite=not doc.is_primary,
                                    primary_snapshot_id=primary_ref)
        doc.snapshot_id = snap.snapshot_id
        db.flush()

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
    """粗筛 + 抽取 + 记录级去重;产出草稿事件或标记淘汰/合并。"""
    result = {"doc_id": doc.id, "action": None, "event_id": None}
    diagnostics.set_ref(doc.url)  # 后续 LLM/决策留痕自动关联到本文档
    if doc.screen_status == "screened_out":
        result["action"] = "skipped"
        return result
    # 转载(非首发)不再"未读先丢":仍走粗筛+抽取,真同事件由记录级指纹/语义去重合并。
    # 这样即便 SimHash 误判同稿,有价值内容也不会被静默丢弃;只在诊断里标注供分析。
    if not doc.is_primary:
        diagnostics.record("dedup", "同稿簇非首发(转载),仍走粗筛后由记录级去重把关",
                           detail={"cluster_id": doc.cluster_id})

    cfg = need.config
    verdict = screen_document(cfg, doc.title or "", doc.content_text or "")
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
        db.flush()
        return result
    doc.screen_status = "screened_in"

    schema_file = (cfg.get("record_schemas") or [{}])[0].get("file") or str(settings.schema_dir / "event.schema.json")
    record_schema = load_record_schema(schema_file)
    dictionaries = get_active_dictionaries(db, need.id)
    extraction = extract_record(cfg, dictionaries, record_schema, doc.title or "", doc.content_text or "")
    payload = extraction["payload"]
    diagnostics.record("extract", "结构化抽取完成",
                       detail={"payload": payload, "violations": extraction["violations"],
                               "schema_errors": extraction["schema_errors"]})
    # 非网络安全范畴(内容治理/名单/政策)→ 不入库,直接过滤(粗筛漏网时的兜底闸门)
    if _is_out_of_scope(payload, doc.title or "", doc.content_text or ""):
        doc.screen_status = "screened_out"
        doc.screen_reason = "非网络安全范畴(内容治理/名单/政策解读),不入库"
        result["action"] = "screened_out"
        diagnostics.record("extract", "判为非安全范畴,过滤不入库",
                           detail={"record_type": payload.get("record_type"), "title": doc.title})
        db.flush()
        return result
    # 抽取空壳(标题/单位/要素全无)→ 不建空事件,转人工待定,避免仪表盘一堆空记录
    if not _payload_has_content(payload):
        doc.screen_status = "manual_queue"
        doc.screen_reason = "抽取结果为空(疑似模型输出异常/正文不足),待人工确认"
        result["action"] = "manual_queue"
        diagnostics.record("extract", "抽取为空,转人工待定(不建事件)",
                           detail={"raw_keys": list(payload.keys())[:20]})
        db.flush()
        return result

    # 记录级去重:指纹 → 语义召回
    existing = dedup.fingerprint_match(db, need.id, payload)
    if existing:
        # 疑似同一事件:不建新记录,文档转人工队列并挂明疑似目标,等待人工合并(10.3 跨时间合并)
        doc.screen_status = "manual_queue"
        doc.screen_reason = f"疑似与 {existing.event_id} 为同一事件(指纹命中),请人工确认合并"
        result["action"] = "merge_suggested"
        result["event_id"] = existing.event_id
        result["extraction"] = extraction
        diagnostics.record("dedup", f"指纹命中疑似同事件 {existing.event_id},转人工合并",
                           detail={"matched_event": existing.event_id})
        db.flush()
        return result

    src = db.get(Source, doc.source_id)
    src_cred = src.credibility if src else "S4"
    # 通用:发布主体命中该需求信誉名录 → 按主体重定级(官方号/权威机关→S1/S2);
    # 未命中则保留渠道默认等级(不丢弃)
    reg, _ = _reputation(need)
    if reg is not None and doc.publisher:
        src_cred = reputation.subject_credibility(reg, doc.publisher, src_cred)
    ev = create_draft(db, need.id, payload, doc=doc,
                      source_credibility=src_cred,
                      dict_version=str(dictionaries.get("version") or ""))
    recall = dedup.semantic_recall(db, need.id, ev.embedding, exclude_event_id=ev.event_id)
    if recall:
        result["semantic_suspects"] = [(e.event_id, round(s, 3)) for e, s in recall]
    result["action"] = "draft_created"
    result["event_id"] = ev.event_id
    result["violations"] = extraction["violations"]
    result["schema_errors"] = extraction["schema_errors"]
    diagnostics.record("draft", f"生成草稿事件 {ev.event_id}(信誉 {src_cred})", ref=ev.event_id,
                       detail={"event_id": ev.event_id, "source_credibility": src_cred,
                               "semantic_suspects": result.get("semantic_suspects"),
                               "violations": extraction["violations"]})
    db.flush()
    return result


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


def _item_pub(item: DiscoveredItem):
    """列表项的发布日期(用于时效早停):优先 item.published,回退 URL 日期。"""
    return _parse_dt(item.published) or url_tools.date_from_url(item.url)


# 非网络安全范畴的强排除特征(内容治理/名单/政策),粗筛漏网时抽取后再兜一道
_OUT_OF_SCOPE_RE = re.compile(
    "清朗|专项行动|集中整治|不良信息|未成年人网络保护|团播|直播乱象|账号名称|短视频|"
    "算法备案|备案信息|评估.{0,4}名单|通过.{0,4}名单|遴选结果|支撑单位|认证机构名单|"
    "专家解读|政策解读|报告发布|白皮书")


def _is_out_of_scope(payload: dict, title: str, text: str) -> bool:
    """判定是否非网络安全范畴(不该入库):优先信 LLM 的 record_type,再用关键词兜底。"""
    if payload.get("record_type") == "不该入库":
        return True
    blob = f"{title or ''} {payload.get('title') or ''}"
    # 标题命中内容治理/名单/政策特征,且无明确安全事件要素(攻击/漏洞/泄露等)
    if _OUT_OF_SCOPE_RE.search(blob):
        sec = re.search("漏洞|攻击|入侵|勒索|木马|僵尸网络|钓鱼|泄露|篡改|黑客|后门|挖矿|C2|窃取", blob)
        if not sec:
            return True
    return False


def _payload_has_content(p: dict) -> bool:
    """抽取结果是否有实质内容(至少有标题、或具体单位、或攻击/后果要素)。"""
    if not isinstance(p, dict):
        return False
    if (p.get("title") or "").strip():
        return True
    org = (p.get("org_name") or "").strip()
    if org and org not in ("未披露", "未知", "不明", ""):
        return True
    return bool(p.get("attack_type") or p.get("consequences"))


def _consume_paginated(db, need, source, run, fetch_page, max_pages,
                       early_enabled, stop_th, do_archive, stats, deadline=None):
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


def crawl_source(db: Session, need: NeedProfile, source: Source,
                 queries: list[str] | None = None, behavior: str = "B1",
                 max_pages: int = 1, do_archive: bool = True) -> CrawlRun:
    """执行一个源的抓取(页面型 discover / 查询型 search),含翻页早停增量与截断上报。"""
    run = CrawlRun(source_id=source.id)
    db.add(run)
    db.flush()
    adapter = get_adapter(source)
    stats = {"new": 0, "skipped": 0, "failed": 0, "blacklist": 0, "too_old": 0}
    found = 0
    early_enabled, stop_th = _early_stop_config(source, adapter)
    budget = int(getattr(settings, "source_time_budget_seconds", 0) or 0)
    deadline = (time.time() + budget) if budget > 0 else None
    try:
        # 批次内浏览器实例复用:本源所有需渲染的页面共用一个浏览器(嵌套则复用上层 job 会话)
        with fetcher.render_session():
            if source.kind == "query":
                has_pager = hasattr(adapter, "search_page")
                # 搜索型源限流:关键词截到上限,避免 400 词硬打慢站空跑几十分钟
                cap = int(getattr(settings, "search_source_query_cap", 0) or 0)
                qlist = (queries or [])[:cap] if cap > 0 else (queries or [])
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
                        do_archive, stats, deadline)
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
                            max_pages, early_enabled, stop_th, do_archive, stats, deadline)
                        found += f
                        child.last_success_at = datetime.utcnow()
                else:
                    # 没识别到有效栏目 → 退回抓根页本身
                    found, _pu, _tr, _sn = _consume_paginated(
                        db, need, source, run, lambda page: adapter.discover_page(page),
                        max_pages, early_enabled, stop_th, do_archive, stats, deadline)
            else:
                # 页面型:官方栏目/公众号历史列表按时间倒序,支持翻页 + 早停(默认早停开启)
                found, _pu, _tr, _sn = _consume_paginated(
                    db, need, source, run, lambda page: adapter.discover_page(page),
                    max_pages, early_enabled, stop_th, do_archive, stats, deadline)
        run.status = "ok"
        source.last_success_at = datetime.utcnow()
        source.fail_streak = 0
    except Exception as e:  # noqa: BLE001 单源失败不拖垮批次
        run.status = "failed"
        run.error = str(e)[:500]
        source.fail_streak += 1
    run.urls_found = found
    run.urls_new = stats["new"]
    run.urls_skipped = stats["skipped"]
    run.urls_failed = stats["failed"]
    run.finished_at = datetime.utcnow()
    db.flush()
    return run
