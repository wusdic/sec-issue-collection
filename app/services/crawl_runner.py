"""后台采集任务:异步执行 + 持久化进度 + 详细日志。

点击"开始采集"即建 CrawlJob 并起后台线程,请求立即返回。任何页面/刷新都能通过
CrawlJob 查到"是否在跑、跑到哪、结果如何";每一步与每次失败都写 CrawlLog 便于排查。
"""
import threading
import traceback
from datetime import datetime

from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import CrawlJob, CrawlLog, KeywordSet, NeedProfile, RawDocument, Source
from app.services import discovery, leads, pipeline
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
        if need.id in (s.serves_needs or []) and not s.manual_assist:
            out.append(s)
        if len(out) >= limit:
            break
    return out


def _run(job_id: int):
    db = SessionLocal()
    try:
        job = db.get(CrawlJob, job_id)
        need = db.get(NeedProfile, job.need_id)
        ks = db.query(KeywordSet).filter_by(need_id=need.id, is_active=True).first()
        queries = expand_queries(ks.content) if ks else []
        max_pages = int(ks.content.get("max_pages_per_query", 3)) if ks else 3

        srcs = _pick_sources(db, need, job.limit_sources)
        job.total_sources = len(srcs)
        job.phase = "抓取"
        db.commit()
        _log(db, job_id, "info", None,
             f"开始采集:选中 {len(srcs)} 个源、关键词 {len(queries)} 条(每查询最多 {max_pages} 页)")
        db.commit()

        for src in srcs:
            if job_id in _CANCEL:
                job.status = "canceled"
                job.finished_at = datetime.utcnow()
                _log(db, job_id, "warn", None, "用户取消采集")
                db.commit()
                return
            _log(db, job_id, "info", src.name,
                 f"开始抓取({'检索型' if src.kind == 'query' else '页面型'},适配器 {src.adapter})")
            db.commit()
            try:
                run = pipeline.crawl_source(db, need, src, queries=queries,
                                            max_pages=max_pages, do_archive=True)
                lvl = "info" if run.status == "ok" else "error"
                msg = f"完成:发现 {run.urls_found} 条、新增入库 {run.urls_new} 条、状态 {run.status}"
                if run.error:
                    msg += f" | 错误:{run.error}"
                _log(db, job_id, lvl, src.name, msg)
                job.new_docs += run.urls_new
            except Exception as e:  # noqa: BLE001 单源失败不终止整批
                _log(db, job_id, "error", src.name, f"源抓取异常:{e}")
            job.done_sources += 1
            db.commit()

        # 处理待粗筛文档
        job.phase = "过滤与抽取"
        pend = db.query(RawDocument).filter_by(need_id=need.id, screen_status="pending").limit(500).all()
        job.total_docs = len(pend)
        db.commit()
        _log(db, job_id, "info", None, f"抓取完成,开始处理 {len(pend)} 篇文档(粗筛过滤 → 抽取)")
        db.commit()

        for doc in pend:
            if job_id in _CANCEL:
                job.status = "canceled"
                job.finished_at = datetime.utcnow()
                db.commit()
                return
            try:
                r = pipeline.process_document(db, need, doc)
                a = r.get("action")
                if a == "draft_created":
                    job.new_events += 1
                    job.kept_docs += 1
                    _log(db, job_id, "info", doc.publisher, f"[相关·已抽取] {r['event_id']} ← {doc.title}")
                elif a == "merge_suggested":
                    job.kept_docs += 1
                    _log(db, job_id, "info", doc.publisher, f"[相关·疑似已有] 转人工合并 ← {doc.title}")
                elif a == "manual_queue":
                    _log(db, job_id, "info", doc.publisher, f"[待人工] {doc.screen_reason} ← {doc.title}")
                else:  # screened_out / duplicate_doc / skipped
                    job.dropped_docs += 1
                    _log(db, job_id, "info", doc.publisher, f"[过滤] {doc.screen_reason or a} ← {doc.title}")
            except Exception as e:  # noqa: BLE001
                _log(db, job_id, "error", doc.publisher, f"文档处理异常:{e} ← {doc.title}")
            job.done_docs += 1
            db.commit()

        job.phase = "收尾(候选源评分/线索刷新)"
        db.commit()
        try:
            discovery.evaluate_candidates(db, need.id)
            leads.refresh_window_stages(db, need.id)
        except Exception as e:  # noqa: BLE001
            _log(db, job_id, "warn", None, f"收尾步骤异常(不影响主结果):{e}")

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
        _CANCEL.discard(job_id)
        db.close()
