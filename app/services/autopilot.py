"""源库自动运维(自动驾驶):把"人工按按钮"变成"系统按周期自己做"。

数据源模块原来有五件事全靠人点:整理查重、给根域源定位栏目、体检、主动找源、候选转正。
不点就不做,源库只会越来越旧、候选越堆越多。这里用一个调度把它们串起来,各有各的周期,
每一步都落 AutoOpsRun 记录——自动化但不黑箱,人能事后核对系统做了什么。

真正留给人的只剩极少数:自动定级判不了的(主要是"该给 S2 吗",涉及已确认金额红线),
在候选/数据源页作为"待确认"呈现,一键处理。
"""
import threading
from datetime import datetime, timedelta

from app.config import settings
from app.db import SessionLocal
from app.models import AutoOpsRun, Source

_lock = threading.Lock()
_running = threading.Event()

# 各任务的默认周期(天),可用 settings.autopilot_*_days 覆盖
TASKS = [
    ("dedup", "整理源键与查重合并", "autopilot_dedup_days", 7),
    ("locate", "给根域源精准定位栏目", "autopilot_locate_days", 7),
    ("health", "源体检 + 停用源复检恢复", "autopilot_health_days", 3),
    ("prospect", "主动找源 + 候选相关度初评", "autopilot_prospect_days", 7),
    ("grade", "试运行源自动定级/淘汰", "autopilot_grade_days", 1),
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


def _do_locate(db, need_id: str) -> dict:
    from app.services import columns, locate
    todo = locate.pending(db, need_id)
    cap = int(getattr(settings, "autopilot_locate_max", 10) or 10)
    todo = todo[:cap] if cap > 0 else todo
    located, cols, failed = 0, 0, 0
    for s in todo:
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
    return {"scanned": len(todo), "located": located, "columns": cols, "no_column": failed}


def _do_health(db, need_id: str) -> dict:
    from app.services import health
    cap = int(getattr(settings, "autopilot_health_max", 25) or 25)
    r = health.run_batch(db, need_id, limit=cap)
    r.pop("results", None)          # 摘要落库,明细太长不存
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


_ACTIONS = {"dedup": _do_dedup, "locate": _do_locate, "health": _do_health,
            "prospect": _do_prospect, "grade": _do_grade}


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
    """真正需要人处理的事(自动化跑完后剩下的极少数)。页面据此提示,不必到处翻。"""
    from app.services import grading
    suggest = grading.pending_human(db, need_id)
    stale = grading.stale_trials(db, need_id)
    manual_assist = [s.name for s in db.query(Source).filter_by(manual_assist=True).all()
                     if need_id in (s.serves_needs or [])]
    return {"suggest_credibility": suggest,
            "stale_trials": [{"id": s.id, "name": s.name} for s in stale],
            "manual_assist": manual_assist,
            "total": len(suggest) + len(manual_assist)}
