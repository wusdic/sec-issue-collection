"""源库自动运维(自动驾驶):把"人工按按钮"变成"系统按周期自己做"。

数据源模块原来有五件事全靠人点:整理查重、给根域源定位栏目、体检、主动找源、候选转正。
不点就不做,源库只会越来越旧、候选越堆越多。这里用一个调度把它们串起来,各有各的周期,
每一步都落 AutoOpsRun 记录——自动化但不黑箱,人能事后核对系统做了什么。

真正留给人的只剩极少数:自动定级判不了的(主要是"该给 S2 吗",涉及已确认金额红线),
在候选/数据源页作为"待确认"呈现,一键处理。
"""
import threading
import time as _time
from datetime import datetime, timedelta

from app.config import settings
from app.db import SessionLocal
from app.models import AutoOpsRun, Source

_lock = threading.Lock()
_running = threading.Event()

# 各任务的默认周期(天),可用 settings.autopilot_*_days 覆盖
TASKS = [
    # 顺序即执行顺序:先把内置源补齐、再挑好可用引擎,后面的找源才不会白跑
    ("seeds", "载入内置种子源清单(升级后自动补新源)", "autopilot_seeds_days", 7),
    ("engines", "找源引擎自检并自动只留可用的", "autopilot_engines_days", 3),
    ("dedup", "整理源键与查重合并", "autopilot_dedup_days", 7),
    ("locate", "给根域源精准定位栏目", "autopilot_locate_days", 7),
    ("health", "源体检 + 停用源复检恢复", "autopilot_health_days", 3),
    # 找源之前先进化词表:淘汰拖后腿的限定词、从语料里挖新词,这一轮就能用上
    ("queries", "找源词进化:算增益、淘汰废词、挖新词", "autopilot_queries_days", 7),
    ("prospect", "主动找源 + 候选相关度初评", "autopilot_prospect_days", 7),
    ("grade", "试运行源自动定级/淘汰", "autopilot_grade_days", 1),
    ("candidates", "候选池:补初评 + 达标入库 + 清理无关", "autopilot_candidates_days", 1),
    # 主线闭环:文档型记录的来源页面变了要让回访看见;发布的记录按画像导出(本地资料库/外部表)
    ("recheck", "到期回访记录的来源再核查(内容变化提示)", "autopilot_recheck_days", 7),
    ("exports", "按画像导出已发布记录(本地资料库/多维表格)", "autopilot_exports_days", 1),
]


def _cadence(key: str, default: int) -> int:
    return int(getattr(settings, key, default) or default)


def _last_run(db, need_id: str, task: str) -> AutoOpsRun | None:
    return (db.query(AutoOpsRun)
            .filter(AutoOpsRun.need_id == need_id, AutoOpsRun.task == task,
                    AutoOpsRun.status.in_(["done", "skipped"]))
            .order_by(AutoOpsRun.started_at.desc()).first())


def due_tasks(db, need_id: str, force: bool = False) -> list[tuple[str, str]]:
    """到期该跑的任务(force=全部跑)。"""
    out = []
    for task, label, key, default in TASKS:
        if force:
            out.append((task, label))
            continue
        last = _last_run(db, need_id, task)
        days = _cadence(key, default)
        if last is None or (datetime.utcnow() - last.started_at) >= timedelta(days=days):
            out.append((task, label))
    return out


def plan(db, need_id: str) -> list[dict]:
    """自动运维计划表(页面展示:每项多久跑一次、上次什么时候跑的、下次什么时候)。"""
    out = []
    for task, label, key, default in TASKS:
        last = _last_run(db, need_id, task)
        days = _cadence(key, default)
        nxt = (last.started_at + timedelta(days=days)) if last else None
        out.append({"task": task, "label": label, "every_days": days,
                    "last_run": last.started_at.isoformat(timespec="seconds") if last else None,
                    "last_summary": (last.summary if last else None),
                    "next_run": nxt.isoformat(timespec="seconds") if nxt else "尽快",
                    "due": last is None or (datetime.utcnow() - last.started_at) >= timedelta(days=days)})
    return out


# ---------------- 各任务的实际动作 ----------------

def _do_dedup(db, need_id: str) -> dict:
    from app.services import discovery
    return discovery.recompute_keys(db)


def _deadline():
    """本步的截止时刻。

    真正该限制的是"这一步别跑太久",不是"最多处理几条"——条数上限是拍脑袋的:
    源变多了它就覆盖不到,人还得记着去调。所以条数上限放到实际上不限制,
    用时间预算兜住;没跑完的下一轮自然接着做(两者都按"最久没处理的优先"排序)。
    """
    sec = int(getattr(settings, "autopilot_task_budget_seconds", 0) or 0)
    return (_time.monotonic() + sec) if sec > 0 else None


def _do_locate(db, need_id: str) -> dict:
    from app.services import columns, locate
    todo = locate.pending(db, need_id)
    cap = int(getattr(settings, "autopilot_locate_max", 0) or 0)
    if cap > 0:
        todo = todo[:cap]
    until = _deadline()
    located, cols, failed, scanned = 0, 0, 0, 0
    for s in todo:
        if until and _time.monotonic() > until:
            break                   # 时间到:剩下的下一轮接着做
        scanned += 1
        try:
            kids, _ = columns.discover_and_persist(db, s)
            if kids:
                located += 1
                cols += len(kids)
            else:
                failed += 1
        except Exception:  # noqa: BLE001 单站失败不终止
            db.rollback()
            failed += 1
    out = {"scanned": scanned, "located": located, "columns": cols, "no_column": failed}
    if scanned < len(todo):
        out["remaining"] = len(todo) - scanned
        out["note"] = f"本轮时间到,还有 {out['remaining']} 个站下轮继续"
    return out


def _do_health(db, need_id: str) -> dict:
    from app.services import health
    cap = int(getattr(settings, "autopilot_health_max", 0) or 0)
    until = _deadline()
    r = health.run_batch(db, need_id, limit=cap or None,
                         should_stop=(lambda: bool(until and _time.monotonic() > until)))
    r.pop("results", None)          # 摘要落库,明细太长不存
    if r.pop("canceled", None) and r.get("done", 0) < r.get("total", 0):
        r["remaining"] = r["total"] - r["done"]
        r["note"] = f"本轮时间到,还有 {r['remaining']} 个源下轮继续"
    return r


def _do_prospect(db, need_id: str) -> dict:
    from app.services import discovery, fetcher, prospect
    if not getattr(settings, "prospect_enabled", True):
        return {"skipped": "主动找源已关闭"}
    with fetcher.render_session():
        r = prospect.run_once(db, need_id)
        p = prospect.probe_pending(db, need_id)
    cands = discovery.evaluate_candidates(db, need_id, prospect.llm_scores(db))
    auto = [c for c in cands if c.get("auto_trial")]
    return {"queries": r["queries"], "hits": r["hits"], "new_candidates": len(r["new_keys"]),
            "probed": p.get("probed", 0), "auto_trial": len(auto),
            "trial_names": [c.get("name") or c["identity_key"] for c in auto[:20]]}


def _do_grade(db, need_id: str) -> dict:
    from app.services import grading
    r = grading.auto_grade(db, need_id)
    r["results"] = r.get("results", [])[:30]     # 只留前 30 条明细
    return r


def _do_candidates(db, need_id: str) -> dict:
    """候选池日常自动处理:给没初评的补初评 → 重新评分、达标的自动入库 → 清理明确无关的。

    没有这一步,候选池就只有每周找源时才会被碰一次;而采集途中(引文/公众号署名)发现的
    候选要等一周才被初评,单通道又过不了闸门,等于一直躺着没人管——自动化就没做完。
    """
    from app.services import discovery, fetcher, prospect
    with fetcher.render_session():
        p = prospect.probe_pending(db, need_id)
    cands = discovery.evaluate_candidates(db, need_id, prospect.llm_scores(db))
    auto = [c for c in cands if c.get("auto_trial")]
    pruned = discovery.prune_candidates(db, need_id)
    return {"probed": p.get("probed", 0), "candidates": len(cands),
            "auto_trial": len(auto), "pruned": pruned.get("pruned", 0),
            "trial_names": [c.get("name") or c["identity_key"] for c in auto[:20]]}


def _do_seeds(db, need_id: str) -> dict:
    """把配置文件里的内置种子源载入库(幂等)。

    升级后新增的内置源不该还要人去跑一次 CLI —— 载入是幂等的,已有的不动、只补新的。
    """
    from app.services import profiles
    paths = profiles.need_paths(need_id)
    if not paths["sources"] or not paths["sources"].exists():
        return {"in_file": 0, "added": 0, "total": db.query(Source).count(), "skipped": "画像未声明种子源文件"}
    in_file = profiles.count_seed_sources(paths["sources"])
    before = db.query(Source).count()
    profiles.load_seed_sources(db, need_id, paths["sources"])
    db.commit()
    added = db.query(Source).count() - before
    if added:
        from app.services import actions
        actions.record(db, "source.seeds_loaded",
                       f"内置种子源清单自动载入:新增 {added} 个源", need_id=need_id, count=added)
        db.commit()
    return {"in_file": in_file, "added": added, "total": before + added}


def _do_engines(db, need_id: str) -> dict:
    """自检所有候选搜索引擎,自动把 prospect_engines 设成当前可用的那些。"""
    from app.services import fetcher, prospect
    if not getattr(settings, "prospect_autotune", True):
        return {"skipped": "引擎自动调优已关闭"}
    fresh = prospect.sync_new_engines(db)      # 升级新加的引擎先补进来,再一起测
    with fetcher.render_session():
        r = prospect.autotune_engines(db)
    db.commit()
    if fresh.get("added"):
        r["newly_shipped"] = fresh["added"]
    return r


def _do_queries(db, need_id: str) -> dict:
    """找源词进化:算限定词增益、淘汰废词、从语料里挖新词。"""
    from app.services import query_evolution
    if not getattr(settings, "query_evolution_enabled", True):
        return {"skipped": "关键词进化已关闭"}
    r = query_evolution.evolve(db, need_id)
    db.commit()
    weak, added = r["terms"]["weak"], r["mutate"]["added"]
    if weak or added:
        from app.services import actions
        bits = []
        if weak:
            bits.append("降级拖后腿的限定词 " + "、".join(
                f"{w['term']}(增益 {w['lift']})" for w in weak[:5]))
        if added:
            bits.append(f"新增候选找源词 {len(added)} 条")
        actions.record(db, "source.queries_evolved", ";".join(bits),
                       need_id=need_id, count=len(added), detail=r)
        db.commit()
    return r


def _do_recheck(db, need_id: str) -> dict:
    from app.services import followup, need_ctx
    return followup.recheck_due(db, need_id, need_ctx.get(db, need_id))


def _do_exports(db, need_id: str) -> dict:
    from app.services import exports, need_ctx
    c = need_ctx.get(db, need_id)
    if not (c.outputs.get("exports") or []):
        return {"skipped": "画像未声明 outputs.exports"}
    return exports.run(db, c)


_ACTIONS = {"recheck": _do_recheck, "exports": _do_exports,
            "seeds": _do_seeds, "engines": _do_engines, "queries": _do_queries,
            "dedup": _do_dedup, "locate": _do_locate, "health": _do_health,
            "prospect": _do_prospect, "grade": _do_grade, "candidates": _do_candidates}


# ---------------- 调度 ----------------

def run_due(need_id: str, force: bool = False) -> dict:
    """跑一轮自动运维(同步)。每步独立记录、独立提交,一步失败不影响后面几步。"""
    if _running.is_set():
        return {"skipped": "已有自动运维在跑"}
    _running.set()
    db = SessionLocal()
    done = []
    try:
        for task, label in due_tasks(db, need_id, force):
            row = AutoOpsRun(need_id=need_id, task=task, status="running")
            db.add(row)
            try:
                db.commit()
            except Exception:  # noqa: BLE001
                db.rollback()
                continue
            try:
                summary = _ACTIONS[task](db, need_id) or {}
                row = db.get(AutoOpsRun, row.id)
                row.status = "skipped" if summary.get("skipped") else "done"
                row.summary = summary
                row.note = label
            except Exception as e:  # noqa: BLE001 单步失败不影响其它步
                try:
                    db.rollback()
                except Exception:  # noqa: BLE001
                    pass
                from app.services.errors import error_headline
                row = db.get(AutoOpsRun, row.id)
                if row:
                    row.status, row.note = "failed", error_headline(e, 400)
                summary = {"error": True}
            if row:
                row.finished_at = datetime.utcnow()
            try:
                db.commit()
            except Exception:  # noqa: BLE001
                db.rollback()
            done.append({"task": task, "label": label,
                         "status": (row.status if row else "failed"), "summary": summary})
        return {"ran": len(done), "tasks": done}
    finally:
        _running.clear()
        db.close()


def start_async(need_id: str, force: bool = False) -> dict:
    """后台起一轮自动运维(页面手动触发用;调度线程直接调 run_due)。"""
    if _running.is_set():
        return {"started": False, "note": "已有自动运维在跑"}
    threading.Thread(target=run_due, args=(need_id, force), daemon=True).start()
    return {"started": True}


def is_running() -> bool:
    return _running.is_set()


def recent(db, need_id: str, limit: int = 30) -> list[dict]:
    rows = (db.query(AutoOpsRun).filter_by(need_id=need_id)
            .order_by(AutoOpsRun.started_at.desc()).limit(limit).all())
    label = {t[0]: t[1] for t in TASKS}
    return [{"id": r.id, "task": r.task, "label": label.get(r.task, r.task),
             "status": r.status, "summary": r.summary, "note": r.note,
             "started_at": r.started_at.isoformat(timespec="seconds"),
             "finished_at": r.finished_at.isoformat(timespec="seconds") if r.finished_at else None}
            for r in rows]


def human_todo(db, need_id: str) -> dict:
    """真正需要人处理的事(自动化跑完后剩下的极少数)。页面据此提示,不必到处翻。

    刻意只放"机器判不了"的三类,别把自动化已经能处理的事也塞进来变成待办:
      ① 定级建议(主要是"该不该给 S2"——S2 能支撑『已确认』金额,是发布红线,机器不碰);
      ② 半自动源(要人贴结果的);
      ③ 找源引擎全线不可用(自动调优也救不回来,只能人去开渲染/换网络)。
    """
    from app.services import grading
    suggest = grading.pending_human(db, need_id)
    stale = grading.stale_trials(db, need_id)
    manual_assist = [s.name for s in db.query(Source).filter_by(manual_assist=True).all()
                     if need_id in (s.serves_needs or [])]
    engines = [x.strip() for x in str(getattr(settings, "prospect_engines", "")).split(",")
               if x.strip()]
    blocked = []
    if getattr(settings, "prospect_enabled", True) and not engines:
        blocked.append("一个可用的找源引擎都没有:主动找源现在是空跑。"
                       "去设置页打开「启用浏览器渲染/截图」后点一次「🔎 找源路径自检」")
    return {"suggest_credibility": suggest,
            "stale_trials": [{"id": s.id, "name": s.name} for s in stale],
            "manual_assist": manual_assist,
            "blocked": blocked,
            "total": len(suggest) + len(manual_assist) + len(blocked)}
