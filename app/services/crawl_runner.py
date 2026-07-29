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
from app.services.errors import error_headline
from app.services.scheduler import expand_queries

_CANCEL: set[int] = set()  # 请求取消的 job_id


def _now() -> float:
    import time
    return time.time()


# 正在抓取的源:{source_id: (名称, 开始时间)}。并行版此前无从得知"当前在跑什么",
# 一旦某个源卡住,页面只会停在某个数字不动,无法定位元凶。
_INFLIGHT: dict[int, tuple[str, float]] = {}
_INFLIGHT_LOCK = threading.Lock()


def inflight() -> list[dict]:
    """当前正在抓取的源及已耗时(秒),按耗时降序——最久的那个通常就是卡住的。"""
    now = _now()
    with _INFLIGHT_LOCK:
        rows = [{"source_id": sid, "name": nm, "elapsed": round(now - t0, 1)}
                for sid, (nm, t0) in _INFLIGHT.items()]
    return sorted(rows, key=lambda r: -r["elapsed"])


def _log(db: Session, job_id: int, level: str, source: str | None, message: str):
    db.add(CrawlLog(job_id=job_id, level=level, source=source, message=(message or "")[:2000]))


def _tick(db: Session, job_id: int) -> CrawlJob | None:
    """进度记账提交:失败也绝不抛。

    此前循环里是裸 db.commit():某个 worker 偶尔占锁久一点,主线程这一下就抛
    OperationalError("database is locked"),整批采集当场判死——哪怕所有源本身都好好的。
    进度计数属于记账,丢一次无所谓,不该毁掉整批。回滚后按 id 重取 job 继续用。
    """
    try:
        db.commit()
    except Exception:  # noqa: BLE001
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
    return db.get(CrawlJob, job_id)


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
    """选源:活跃/试运行、服务本需求、非半自动。

    按"最久没成功采过的优先"轮转(从未采过的排最前),避免限制源数时永远只采 id 最小的那几个
    ——源库几十上百个时,靠后的源过去一辈子采不到。limit<=0 表示不限(全量)。
    """
    rows = db.query(Source).filter(Source.lifecycle.in_(["active", "trial"])).all()
    live = {s.id for s in rows}
    out = []
    for s in rows:
        pid = (s.adapter_config or {}).get("parent_site_id")
        # 自动发现的子栏目/挂靠检索源由父源统一采集,不独立占用名额;
        # 但父源若已被停用/删除,子源必须自己上场,否则永远采不到(挂靠成了黑洞)。
        if pid and pid in live:
            continue
        if need.id in (s.serves_needs or []) and not s.manual_assist:
            out.append(s)
    # 从未采过(last_success_at 为 None)优先,其余按上次成功时间升序;同序时按 id 稳定排序
    out.sort(key=lambda s: (s.last_success_at is not None, s.last_success_at or datetime.min, s.id))
    return out if limit is None or limit <= 0 else out[:limit]


def _force_fail_job(job_id: int, err: str):
    """用全新会话把任务标记为失败(原会话已不可用时的最后兜底,防止僵尸 running 卡住页面)。"""
    db2 = SessionLocal()
    try:
        job = db2.get(CrawlJob, job_id)
        if job and job.status == "running":
            job.status = "failed"
            job.finished_at = datetime.utcnow()
            job.error = (err or "")[-500:]
            db2.commit()
    except Exception:  # noqa: BLE001
        try:
            db2.rollback()
        except Exception:  # noqa: BLE001
            pass
    finally:
        db2.close()


def _reap_stale_job(job_id: int):
    """线程退出时兜底:若任务仍是 running(说明中途异常退出),标记失败,避免页面永远不能再采集。"""
    _force_fail_job(job_id, "任务线程异常退出(已自动标记失败)")


def reap_orphan_jobs() -> int:
    """启动时回收僵尸任务:进程重启后 DB 里仍是 running 的任务不可能再有线程在跑。"""
    db = SessionLocal()
    try:
        rows = db.query(CrawlJob).filter(CrawlJob.status == "running").all()
        for j in rows:
            j.status = "failed"
            j.finished_at = datetime.utcnow()
            j.error = (j.error or "") + " [进程重启,任务中断]"
        if rows:
            db.commit()
        return len(rows)
    except Exception:  # noqa: BLE001
        db.rollback()
        return 0
    finally:
        db.close()


def _crawl_one(need_id: str, queries, max_pages: int, src_id: int, rec) -> dict:
    """并行抓取单个源:独立 DB 会话 + 绑定共享诊断记录器。返回统计供主线程汇总。"""
    import time as _t
    t0 = _t.time()
    wdb = SessionLocal()
    diagnostics.bind(rec)
    _nm = f"源#{src_id}"
    try:
        need = wdb.get(NeedProfile, need_id)
        src = wdb.get(Source, src_id)
        _nm = src.name if src else _nm
        with _INFLIGHT_LOCK:
            _INFLIGHT[src_id] = (_nm, t0)
        run = pipeline.crawl_source(wdb, need, src, queries=queries, max_pages=max_pages, do_archive=True)
        wdb.commit()
        return {"name": src.name, "kind": src.kind, "adapter": src.adapter,
                "status": run.status, "found": run.urls_found, "new": run.urls_new,
                "skipped": run.urls_skipped, "failed": run.urls_failed, "error": run.error,
                "elapsed": _t.time() - t0}
    except Exception as e:  # noqa: BLE001 单源失败不终止整批
        wdb.rollback()
        return {"name": _nm, "status": "failed", "error": error_headline(e, 300),
                "found": 0, "new": 0, "skipped": 0, "failed": 0, "elapsed": _t.time() - t0}
    finally:
        with _INFLIGHT_LOCK:
            _INFLIGHT.pop(src_id, None)
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
        # 处理异常(如 LLM 超时)不静默丢文档:转人工待定,下次不会重复处理也不会丢
        pub = None
        try:
            doc = wdb.get(RawDocument, doc_id)
            if doc:
                pub = doc.publisher
                if doc.screen_status in ("pending", "screened_in"):
                    doc.screen_status = "manual_queue"
                    doc.screen_reason = f"处理异常(如大模型超时),待人工:{str(e)[:150]}"
                    wdb.commit()
        except Exception:  # noqa: BLE001
            wdb.rollback()
        return {"action": "error", "error": error_headline(e, 300), "doc_id": doc_id, "publisher": pub}
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
        note = ""
        if settings.playwright_enabled:
            # 每个并行 worker 会各自启一个 Chromium(同步 Playwright 的线程要求),故限并发护内存
            cap = max(1, int(getattr(settings, "render_max_concurrency", 2) or 2))
            if cc > cap:
                cc, note = cap, f"(已开浏览器渲染,抓取并发降至 {cap} 以控内存)"
        _log(db, job_id, "info", None,
             f"开始采集:选中 {len(srcs)} 个源、关键词 {len(queries)} 条(每查询最多 {max_pages} 页)、"
             f"并发 {cc}{note}")
        db.commit()

        # 并行抓取:多源同时抓,单源失败不影响其他
        canceled = False
        # 手动管理线程池:取消时用 shutdown(wait=False) 立即返回;若用 with,退出时会再
        # shutdown(wait=True) 等已在跑的源跑完(最长 source_time_budget_seconds),表现为"点了取消没反应"。
        ex = ThreadPoolExecutor(max_workers=cc)
        try:
            futs = {ex.submit(_crawl_one, need.id, queries, max_pages, sid, rec): sid
                    for sid in src_ids}
            # 并行版此前只记"完成",一旦某个源卡住,日志里看不出当前在跑什么 → 像是"没反应"。
            # 这里先把本轮要跑的源列出来,便于对照"已完成"定位卡住的是哪几个。
            _names = [s.name for s in srcs]
            _log(db, job_id, "info", None,
                 "本轮源清单(按最久未采优先): " + "、".join(_names[:60]) +
                 (f" …共 {len(_names)} 个" if len(_names) > 60 else ""))
            db.commit()
            job_budget = int(getattr(settings, "job_max_seconds", 0) or 0)
            _t0 = _now()
            _pending = dict(futs)          # future -> source_id,用于超时后报告未完成的源
            _warned: set[int] = set()
            for fut in as_completed(futs, timeout=job_budget if job_budget > 0 else None):
                _pending.pop(fut, None)
                # 每完成一个就看一眼:有没有源已经跑很久(卡住的那个自己没法记日志)
                slow_th = max(60, int(getattr(settings, "source_time_budget_seconds", 180) or 180))
                for f in inflight():
                    if f["elapsed"] >= slow_th and f["source_id"] not in _warned:
                        _warned.add(f["source_id"])
                        _log(db, job_id, "warn", f["name"],
                             f"该源已运行 {f['elapsed']:.0f}s 仍未完成,可能卡住(超出单源上限 {slow_th}s)")
                        job = _tick(db, job_id) or job
                res = fut.result()
                lvl = "info" if res["status"] == "ok" else "error"
                if res.get("elapsed", 0) >= max(30, int(getattr(settings, "source_time_budget_seconds", 180) or 180)):
                    lvl = "warn"      # 明显超时的源单独标出来,便于定位"拖慢整批"的元凶
                msg = (f"完成(耗时 {res.get('elapsed', 0):.0f}s):发现 {res['found']} 条、新增 {res['new']} 条、已采过跳过 "
                       f"{res['skipped']} 条、抓取失败 {res['failed']} 条、状态 {res['status']}")
                if res.get("error"):
                    msg += f" | 错误:{res['error']}"
                _log(db, job_id, lvl, res["name"], msg)
                job.new_docs += res["new"]
                job.done_sources += 1
                job = _tick(db, job_id) or job
                if job_id in _CANCEL:
                    canceled = True
                    break
            if canceled:
                job.status = "canceled"
                job.finished_at = datetime.utcnow()
                _log(db, job_id, "warn", None, "用户取消采集")
                db.commit()
                return
        except TimeoutError:
            # 整批超时:不再无限等待,收尾并写明是哪些源没跑完(此前会一直挂在 running)
            stuck = [db.get(Source, sid).name for sid in _pending.values() if db.get(Source, sid)]
            _log(db, job_id, "warn", None,
                 f"采集总时长超过上限({settings.job_max_seconds}s),停止等待。"
                 f"未完成 {len(stuck)} 个源:" + "、".join(stuck[:20]))
            db.commit()
        finally:
            ex.shutdown(wait=False, cancel_futures=True)

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

        pex, p_canceled = ThreadPoolExecutor(max_workers=pc), False
        try:
            futs = {pex.submit(_process_one, need.id, did, rec): did for did in pend_ids}
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
                job = _tick(db, job_id) or job
                if job_id in _CANCEL:
                    p_canceled = True
                    job.status = "canceled"
                    job.finished_at = datetime.utcnow()
                    db.commit()
                    return
        finally:
            pex.shutdown(wait=not p_canceled, cancel_futures=True)

        job.phase = "收尾(候选源自动入库/线索刷新)"
        db.commit()
        try:
            from app.services import prospect as prospect_svc
            # 带上候选源 LLM 相关度(评分公式里权重最高之一,此前从没人算过恒为 0)
            cands = discovery.evaluate_candidates(db, need.id, prospect_svc.llm_scores(db))
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
    except Exception as e:  # noqa: BLE001 兜底记录完整栈
        tb = traceback.format_exc()
        head = error_headline(e)
        # 必须先回滚:异常可能来自 commit/写锁,会话已中毒,此时直接 db.get 会再抛
        # PendingRollbackError 使本函数崩溃 → 任务永远停在 running,页面"开始采集"被永久占用。
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        try:
            job = db.get(CrawlJob, job_id)
            if job:
                job.status = "failed"
                job.finished_at = datetime.utcnow()
                job.error = head        # 页面只显示这一行,必须是"真正的原因",不能是栈头
            _log(db, job_id, "error", None, f"任务失败:{head}\n\n完整栈:\n{tb[-2500:]}")
            db.commit()
        except Exception:  # noqa: BLE001 最后兜底:换新会话也要把任务标记成失败,绝不留僵尸 running
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
            _force_fail_job(job_id, head)
    finally:
        try:
            _diag.__exit__(None, None, None)   # 关闭诊断会话(flush 留痕)
        except Exception:  # noqa: BLE001
            pass
        _CANCEL.discard(job_id)
        db.close()
        _reap_stale_job(job_id)
