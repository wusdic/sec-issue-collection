"""主流水线(方案 9.1):采集 → 存档 → 去重 → 粗筛 → 抽取 → 记录去重 → 建草稿 → 复核队列。

同时承担搜索行为 B1(事件发现)与源发现 D1/D2/D3 的伴生登记。
"""
import re
from datetime import datetime

from dateutil import parser as dtparser
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    CrawlRun, DocCluster, KeywordRun, NeedProfile, RawDocument, SearchWatermark, Source,
)
from app.services import archive, dedup, discovery, fetcher, reputation, url_tools
from app.services.adapters import DiscoveredItem, get_adapter
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


def _reputation(need: NeedProfile):
    """取该需求的发布主体信誉名录与转载检测开关(通用能力,见 services/reputation)。"""
    path, repost_on = reputation.registry_path_for(need.config)
    reg = reputation.load_registry(path) if path else None
    return reg, repost_on


def ingest_item(db: Session, need: NeedProfile, source: Source, item: DiscoveredItem,
                crawl_run_id: int | None = None, do_archive: bool = True,
                prefetched: fetcher.FetchResult | None = None) -> RawDocument | None:
    """单条 URL 入库:URL 去重 → 抓取+当刻存档 → 文本提取 → 同稿聚类 → 源发现伴生。"""
    reg, repost_on = _reputation(need)
    # 黑名单主体(默认空,仅真正垃圾源)直接丢弃;其余主体一律保留并按名录定级
    if reg is not None and reputation.is_blacklisted(reg, item.wechat_account or item.publisher):
        return None
    url = item.url
    if url_tools.is_search_redirect(url):
        fr0 = prefetched or fetcher.fetch(url)  # C3 跳转还原
        url = fr0.final_url if fr0.final_url else url
        prefetched = fr0 if fr0.ok else None
    if dedup.find_existing_url(db, url):
        return None

    fr = prefetched or fetcher.fetch(url)
    final_url = fr.final_url or url
    text = archive.extract_text(fr.html) if fr.ok else None

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
    if doc.screen_status == "screened_out":
        result["action"] = "skipped"
        return result
    if not doc.is_primary:
        doc.screen_status = "screened_out"
        doc.screen_reason = "同稿簇非首发(转载)"
        result["action"] = "duplicate_doc"
        db.flush()
        return result

    cfg = need.config
    verdict = screen_document(cfg, doc.title or "", doc.content_text or "")
    doc.screen_score = verdict["confidence"]
    doc.screen_reason = verdict["reason"]
    if not verdict["is_candidate"]:
        doc.screen_status = "manual_queue" if verdict["confidence"] >= 0.4 else "screened_out"
        result["action"] = doc.screen_status
        db.flush()
        return result
    doc.screen_status = "screened_in"

    schema_file = (cfg.get("record_schemas") or [{}])[0].get("file") or str(settings.schema_dir / "event.schema.json")
    record_schema = load_record_schema(schema_file)
    dictionaries = get_active_dictionaries(db, need.id)
    extraction = extract_record(cfg, dictionaries, record_schema, doc.title or "", doc.content_text or "")
    payload = extraction["payload"]

    # 记录级去重:指纹 → 语义召回
    existing = dedup.fingerprint_match(db, need.id, payload)
    if existing:
        # 疑似同一事件:不建新记录,文档转人工队列并挂明疑似目标,等待人工合并(10.3 跨时间合并)
        doc.screen_status = "manual_queue"
        doc.screen_reason = f"疑似与 {existing.event_id} 为同一事件(指纹命中),请人工确认合并"
        result["action"] = "merge_suggested"
        result["event_id"] = existing.event_id
        result["extraction"] = extraction
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
    db.flush()
    return result


def crawl_source(db: Session, need: NeedProfile, source: Source,
                 queries: list[str] | None = None, behavior: str = "B1",
                 max_pages: int = 1, do_archive: bool = True) -> CrawlRun:
    """执行一个源的抓取(页面型 discover / 查询型 search),含水位线与截断上报。"""
    run = CrawlRun(source_id=source.id)
    db.add(run)
    db.flush()
    adapter = get_adapter(source)
    new_docs = 0
    found = 0
    try:
        if source.kind == "query":
            for q in queries or []:
                qh = url_tools.query_hash(q)
                wm = db.get(SearchWatermark, (source.id, qh))
                items, truncated = adapter.search(q, max_pages=max_pages)
                kr = KeywordRun(need_id=need.id, source_id=source.id, behavior=behavior,
                                query=q, pages_fetched=max_pages, truncated=truncated,
                                results=len(items),
                                result_snapshot=[{"url": i.url, "title": i.title} for i in items[:50]])
                db.add(kr)
                found += len(items)
                q_new = 0  # 本查询新增(C9 命中率统计依赖逐查询口径)
                for item in items:
                    doc = ingest_item(db, need, source, item, run.id, do_archive=do_archive)
                    if doc:
                        q_new += 1
                new_docs += q_new
                kr.new_docs = q_new
                if wm:
                    wm.last_ran_at = datetime.utcnow()
                else:
                    db.add(SearchWatermark(source_id=source.id, query_hash=qh,
                                           last_ran_at=datetime.utcnow()))
        else:
            items = adapter.discover()
            found = len(items)
            for item in items:
                doc = ingest_item(db, need, source, item, run.id, do_archive=do_archive)
                if doc:
                    new_docs += 1
        run.status = "ok"
        source.last_success_at = datetime.utcnow()
        source.fail_streak = 0
    except Exception as e:  # noqa: BLE001 单源失败不拖垮批次
        run.status = "failed"
        run.error = str(e)[:500]
        source.fail_streak += 1
    run.urls_found = found
    run.urls_new = new_docs
    run.finished_at = datetime.utcnow()
    db.flush()
    return run
