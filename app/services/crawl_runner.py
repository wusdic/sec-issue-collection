"""后台采集任务:异步执行 + 持久化进度 + 详细日志。

点击"开始采集"即建 CrawlJob 并起后台线程,请求立即返回。任何页面/刷新都能通过
CrawlJob 查到"是否在跑、跑到哪、结果如何";每一步与每次失败都写 CrawlLog 便于排查。
"""
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal
from app.models import CrawlJob, CrawlLog, KeywordSet, NeedProfile, RawDocument, Source
from app.services import diagnostics, discovery, leads, pipeline
from app.services.scheduler import expand_queries

_CANCEL: set[int] = set()  # 请求取消的 job_id


def _log(db: Session, job_id: int, level: str, source: str | None, message: str):
    db.add(CrawlLog(job_id=job_id, level=level, source=source, message=(message or "")[:2000]))


def has_running(db: Session, need_id: str) -> CrawlJob | None:
    return db.query(CrawlJob).filter_by(need_id=need_id, status="running").order_by(CrawlJob.id.desc()).first()


def current_job(db: Session, need_id: str) -> CrawlJob | None:
    return db.query(CrawlJob).filter_by(need_id=need_id).order_by(CrawlJob.id.desc()).first()


def cancel(job_id: int):
    _CANCEL.add(job_id)


def start_job(need_id: str, limit_sources: int, user_id: int | None) -> int:
    """创建任务并后台启动,返回 job_id(不阻塞)。"""
    db = SessionLocal()
    try:
        job = CrawlJob(need_id=need_id, status="running", phase="准备",
                       limit_sources=limit_sources, triggered_by=user_id)
        db.add(job)
        db.commit()
        jid = job.id
    finally:
        db.close()
    threading.Thread(target=_run, args=(jid,), daemon=True).start()
    return jid


def _pick_sources(db: Session, need: NeedProfile, limit: int) -> list[Source]:
    """手动采集:取活跃/试运行、服务本需求、非半自动的源前 N 个(不看 tier 到期)。"""
    out = []
    for s in db.query(Source).filter(Source.lifecycle.in_(["active", "trial"])).order_by(Source.id).all():
        if (s.adapter_config or {}).get("parent_site_id"):
            continue  # 自动发现的子栏目由父源统一采集,不独立占用名额
        if need.id in (s.serves_needs or []) and not s.manual_assist:
            out.append(s)
        if len(out) >= limit:
            break
    return out


def _crawl_one(need_id: str, queries, max_pages: int, src_id: int, rec) -> dict:
    """并行抓取单个源:独立 DB 会话 + 绑定共享诊断记录器。返回统计供主线程汇总。"""
    wdb = SessionLocal()
    diagnostics.bind(rec)
    try:
        need = wdb.get(NeedProfile, need_id)
        src = wdb.get(Source, src_id)
        run = pipeline.crawl_source(wdb, need, src, queries=queries, max_pages=max_pages, do_archive=True)
        wdb.commit()
        return {"name": src.name, "kind": src.kind, "adapter": src.adapter,
                "status": run.status, "found": run.urls_found, "new": run.urls_new,
                "skipped": run.urls_skipped, "failed": run.urls_failed, "error": run.error}
    except Exception as e:  # noqa: BLE001 单源失败不终止整批
        wdb.rollback()
        return {"name": f"源#{src_id}", "status": "failed", "error": str(e)[:200],
                "found": 0, "new": 0, "skipped": 0, "failed": 0}
    finally:
        wdb.close()


def _process_one(need_id: str, doc_id: int, rec) -> dict:
    """并行处理单篇文档:独立 DB 会话 + 绑定共享诊断记录器。"""
    wdb = SessionLocal()
    diagnostics.bind(rec)
    try:
        need = wdb.get(NeedProfile, need_id)
        doc = wdb.get(RawDocument, doc_id)
        r = pipeline.process_document(wdb, need, doc)
        r["publisher"] = doc.publisher
        r["title"] = doc.title
        r["screen_reason"] = doc.screen_reason
        wdb.commit()
        return r
    except Exception as e:  # noqa: BLE001
        wdb.rollback()
        return {"action": "error", "error": str(e)[:200], "doc_id": doc_id}
    finally:
        wdb.close()


def _run(job_id: int):
    db = SessionLocal()
    _diag = diagnostics.session(job_id)   # 全程诊断留痕:LLM 调用+每步决策记入 run_trace,可下载分析
    _diag.__enter__()
    try:
        job = db.get(CrawlJob, job_id)
        need = db.get(NeedProfile, job.need_id)
        ks = db.query(KeywordSet).filter_by(need_id=need.id, is_active=True).first()
        queries = expand_queries(ks.content) if ks else []
        max_pages = int(ks.content.get("max_pages_per_query", 3)) if ks else 3

        srcs = _pick_sources(db, need, job.limit_sources)
        src_ids = [s.id for s in srcs]
        job.total_sources = len(srcs)
        job.phase = "抓取"
        db.commit()
        rec = diagnostics.current()          # 主记录器,worker 线程共享绑定
        cc = max(1, int(getattr(settings, "crawl_concurrency", 1) or 1))
        _log(db, job_id, "info", None,
             f"开始采集:选中 {len(srcs)} 个源、关键词 {len(queries)} 条(每查询最多 {max_pages} 页)、"
             f"并发 {cc}")
        db.commit()

        # 并行抓取:多源同时抓,单源失败不影响其他
        canceled = False
        with ThreadPoolExecutor(max_workers=cc) as ex:
            futs = {ex.submit(_crawl_one, need.id, queries, max_pages, sid, rec): sid
                    for sid in src_ids}
            for fut in as_completed(futs):
                res = fut.result()
                lvl = "info" if res["status"] == "ok" else "error"
                msg = (f"完成:发现 {res['found']} 条、新增 {res['new']} 条、已采过跳过 "
                       f"{res['skipped']} 条、抓取失败 {res['failed']} 条、状态 {res['status']}")
                if res.get("error"):
                    msg += f" | 错误:{res['error']}"
                _log(db, job_id, lvl, res["name"], msg)
                job.new_docs += res["new"]
                job.done_sources += 1
                db.commit()
                if job_id in _CANCEL:
                    canceled = True
                    break
            if canceled:
                ex.shutdown(wait=False, cancel_futures=True)
                job.status = "canceled"
                job.finished_at = datetime.utcnow()
                _log(db, job_id, "warn", None, "用户取消采集")
                db.commit()
                return

        # 处理待粗筛文档(并行:LLM 抽取是网络等待,多篇并发大幅提速)
        job.phase = "过滤与抽取"
        pend_ids = [r[0] for r in db.query(RawDocument.id)
                    .filter_by(need_id=need.id, screen_status="pending").limit(500).all()]
        job.total_docs = len(pend_ids)
        db.commit()
        pc = max(1, int(getattr(settings, "process_concurrency", 1) or 1))
        _log(db, job_id, "info", None,
             f"抓取完成,开始处理 {len(pend_ids)} 篇文档(粗筛过滤 → 抽取),并发 {pc}")
        db.commit()

        with ThreadPoolExecutor(max_workers=pc) as ex:
            futs = {ex.submit(_process_one, need.id, did, rec): did for did in pend_ids}
            for fut in as_completed(futs):
                r = fut.result()
                a = r.get("action")
                pub, title = r.get("publisher"), r.get("title")
                if a == "draft_created":
                    job.new_events += 1
                    job.kept_docs += 1
                    _log(db, job_id, "info", pub, f"[相关·已抽取] {r['event_id']} ← {title}")
                elif a == "merge_suggested":
                    job.kept_docs += 1
                    _log(db, job_id, "info", pub, f"[相关·疑似已有] 转人工合并 ← {title}")
                elif a == "manual_queue":
                    _log(db, job_id, "info", pub, f"[待人工] {r.get('screen_reason')} ← {title}")
                elif a == "error":
                    _log(db, job_id, "error", pub, f"文档处理异常:{r.get('error')} ← {title}")
                else:  # screened_out / duplicate_doc / skipped
                    job.dropped_docs += 1
                    _log(db, job_id, "info", pub, f"[过滤] {r.get('screen_reason') or a} ← {title}")
                job.done_docs += 1
                db.commit()
                if job_id in _CANCEL:
                    ex.shutdown(wait=False, cancel_futures=True)
                    job.status = "canceled"
                    job.finished_at = datetime.utcnow()
                    db.commit()
                    return

        job.phase = "收尾(候选源自动入库/线索刷新)"
        db.commit()
        try:
            cands = discovery.evaluate_candidates(db, need.id)
            auto = [c for c in cands if c.get("auto_trial")]
            if auto:
                names = "、".join(c.get("name") or c["identity_key"] for c in auto[:10])
                _log(db, job_id, "info", None,
                     f"源自动发现:本轮从采集内容中新识别 {len(cands)} 个候选域名,"
                     f"其中 {len(auto)} 个达标自动入库(trial 试运行,S4 待人工定级):{names}")
            elif cands:
                top = max(cands, key=lambda c: c["score"])
                _log(db, job_id, "info", None,
                     f"源自动发现:识别 {len(cands)} 个候选域名,暂无达标自动入库"
                     f"(最高分 {top['score']},阈值 {settings.discovery_auto_trial_threshold};"
                     f"可在设置页调低『新源自动入库阈值』)")
            leads.refresh_window_stages(db, need.id)
        except Exception as e:  # noqa: BLE001
            _log(db, job_id, "warn", None, f"收尾步骤异常(不影响主结果):{e}")

        # 生成当天简报(新增事件/线索/行业热点/源健康),供页面查看与下载
        try:
            from app.services import digest as digest_svc
            d = digest_svc.generate_today(db, need.id)
            _log(db, job_id, "info", None,
                 f"已生成 {d.day} 日报:新增事件 {d.content.get('events_total', 0)} 条、"
                 f"线索 {d.content.get('leads_total', 0)} 条")
        except Exception as e:  # noqa: BLE001 简报失败不影响采集结果
            _log(db, job_id, "warn", None, f"日报生成异常(不影响采集):{e}")

        job.status = "done"
        job.phase = "完成"
        job.finished_at = datetime.utcnow()
        _log(db, job_id, "info", None,
             f"采集完成:新入库 {job.new_docs} 篇,相关 {job.kept_docs} 篇,过滤 {job.dropped_docs} 篇,"
             f"生成事件 {job.new_events} 条")
        db.commit()
    except Exception:  # noqa: BLE001 兜底记录完整栈
        job = db.get(CrawlJob, job_id)
        if job:
            job.status = "failed"
            job.finished_at = datetime.utcnow()
            job.error = traceback.format_exc()[-500:]
        try:
            _log(db, job_id, "error", None, f"任务失败:\n{traceback.format_exc()[:1500]}")
            db.commit()
        except Exception:  # noqa: BLE001
            pass
    finally:
        try:
            _diag.__exit__(None, None, None)   # 关闭诊断会话(flush 留痕)
        except Exception:  # noqa: BLE001
            pass
        _CANCEL.discard(job_id)
        db.close()
