"""首次部署流程:一键把"刚装好该干的事"按正确顺序干一遍,并把每步进展显示出来。

为什么要单独做这个,而不是让人点「全部跑一遍」:

1) **顺序不一样**。日常运维的顺序是固定周期轮转,而首次部署有依赖关系——
   找源的两个进化输入(覆盖空白、语料挖词)都要先有已采内容才算得出来。
   所以首次必须是:整理源 → 先采一轮 → 再找源。直接跑找源是它最弱的版本。
2) **有些前置条件不满足时,跑了比不跑更糟**。LLM 还是 mock(离线假模型)就去采集,
   会把库填满假的粗筛/抽取结果,之后还得清。这种必须先拦住。
3) **每轮上限对首次不适用**。日常体检/定位可以摊到几天,首次要一次盖住全部源。

每一步都落 AutoOpsRun(task="bootstrap:<步骤>"),所以切页、刷新、甚至重启都能接着看。
"""
import threading
import time as _time
from datetime import datetime

from app.config import settings
from app.db import SessionLocal
from app.models import AutoOpsRun, Source

_lock = threading.Lock()
_running = threading.Event()
_cancel = threading.Event()
_need: str | None = None

TASK = "bootstrap"

# (key, 标签, 这一步在干什么)
STEPS = [
    ("precheck", "前置检查", "确认大模型/浏览器渲染/搜索引擎就绪——不满足就先别跑,否则白跑或污染数据"),
    ("seeds", "载入内置源清单", "把配置文件里的内置源全部入库(幂等,已有的不动)"),
    ("engines", "找源引擎自检", "整池测一遍,只留真能用的;结论直接写进引擎列表"),
    ("dedup", "整理源键并查重合并", "同一个采集目标的重复源合并成一个"),
    ("locate", "给根域源定位栏目", "只填了网站根地址的源,识别出「执法处罚/安全通报」等相关栏目"),
    ("health", "全量体检", "逐个试抓;抓不到的页面源自动改走站内检索"),
    ("crawl", "第一次采集", "把整理好的源采一轮——找源要靠这批语料才发挥得出来"),
    ("prospect", "主动找源 + 候选入库", "此时覆盖空白算得出来、语料也够挖新词了,找源才是满血状态"),
]


_LABELS = {k: v for k, v, _w in STEPS}


def _default_need() -> str:
    from app.services import need_ctx
    return need_ctx.default_need_id()


def _rec(db, step: str, status: str, summary: dict | None = None, note: str = ""):
    row = AutoOpsRun(need_id=_need or _default_need(), task=f"{TASK}:{step}", status=status,
                     summary=summary or {}, note=note[:2000] or None,
                     finished_at=None if status == "running" else datetime.utcnow())
    try:
        db.add(row)
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
    return row


# ---------------- 前置检查 ----------------

def precheck(db) -> dict:
    """跑之前必须确认的几件事。blocking=True 的不解决就不该往下跑。"""
    items = []
    if str(getattr(settings, "llm_provider", "mock")) == "mock":
        items.append({"key": "llm", "blocking": True, "ok": False,
                      "title": "大模型还是 mock(离线假模型)",
                      "fix": "去设置页把「LLM 模式」改成 openai_compat,填好接口地址/密钥/模型名,"
                             "再点「测试大模型连通」。带着 mock 采集会把库填满假的粗筛与抽取结果,"
                             "之后还得整批清掉——这一步必须先做。"})
    else:
        items.append({"key": "llm", "blocking": True, "ok": True,
                      "title": f"大模型已配置({settings.llm_provider})"})
    render = bool(getattr(settings, "playwright_enabled", False))
    items.append({"key": "render", "blocking": False, "ok": render,
                  "title": "浏览器渲染已开启" if render else "浏览器渲染没开",
                  "fix": "" if render else
                         "去设置页打开「启用浏览器渲染/截图」。政务站的 JS 壳、搜狗/360 的跳转页、"
                         "公众号页都要靠它;不开也能跑,但源的可用率会低不少。"})
    engines = [x for x in str(getattr(settings, "prospect_engines", "")).split(",") if x.strip()]
    items.append({"key": "engines", "blocking": False, "ok": bool(engines),
                  "title": f"找源引擎:{'、'.join(engines)}" if engines else "没有配置任何找源引擎",
                  "fix": "" if engines else "下一步的引擎自检会自动挑,这里不用管。"})
    n = db.query(Source).count()
    items.append({"key": "sources", "blocking": False, "ok": True,
                  "title": f"当前源库 {n} 个源"})
    blocked = [x for x in items if x["blocking"] and not x["ok"]]
    return {"items": items, "ok": not blocked,
            "blocked": [x["title"] for x in blocked]}


# ---------------- 各步骤 ----------------

def _step_seeds(db):
    from app.services import autopilot
    return autopilot._do_seeds(db, _need)


def _step_engines(db):
    from app.services import fetcher, prospect
    with fetcher.render_session():
        from app.services import need_ctx
        r = prospect.selftest(db, ctx=need_ctx.get(db, _need or _default_need()))
        r["applied"] = prospect.apply_selftest(db, r)
    db.commit()
    return {"usable": r["usable"], "tested": r["tested"],
            "applied": r["applied"].get("note", ""), "advice": r["advice"]}


def _step_dedup(db):
    from app.services import autopilot
    return autopilot._do_dedup(db, _need)


def _step_locate(db):
    """首次要一次盖住全部根域源,所以临时解除"每轮最多几个站"的限量。"""
    from app.services import autopilot
    old = getattr(settings, "autopilot_locate_max", 0)
    settings.autopilot_locate_max = 0
    try:
        return autopilot._do_locate(db, _need)
    finally:
        settings.autopilot_locate_max = old


def _step_health(db):
    from app.services import autopilot
    old = getattr(settings, "autopilot_health_max", 0)
    settings.autopilot_health_max = 0
    try:
        return autopilot._do_health(db, _need)
    finally:
        settings.autopilot_health_max = old


def _step_crawl(db):
    """起一轮采集并等它跑完(首次流程的意义就在于人不用盯着一步步点)。"""
    from app.models import CrawlJob
    from app.services import crawl_runner
    running = crawl_runner.has_running(db, _need)
    job_id = running.id if running else crawl_runner.start_job(_need, 0, None)
    waited, budget = 0, int(getattr(settings, "bootstrap_crawl_budget_seconds", 7200) or 0)
    while True:
        if _cancel.is_set():
            crawl_runner.cancel(job_id)
            return {"job_id": job_id, "canceled": True}
        _time.sleep(5)
        waited += 5
        db.expire_all()
        job = db.get(CrawlJob, job_id)
        if not job or job.status != "running":
            break
        if budget and waited > budget:
            return {"job_id": job_id, "phase": job.phase, "still_running": True,
                    "note": f"采集还在跑({waited // 60} 分钟),首次流程不再等它;"
                            "它会自己跑完,之后再点一次首次流程就会接着跑找源那一步"}
    job = db.get(CrawlJob, job_id)
    if not job:
        return {"job_id": job_id, "status": "?"}
    return {"job_id": job_id, "status": job.status, "sources": job.total_sources,
            "new_docs": job.new_docs, "kept_docs": job.kept_docs,
            "new_events": job.new_events, "error": job.error}


def _step_prospect(db):
    from app.services import autopilot, query_evolution
    q = query_evolution.evolve(db, _need)      # 先让词表按刚采到的语料长一轮
    db.commit()
    r = autopilot._do_prospect(db, _need)
    c = autopilot._do_candidates(db, _need)
    db.commit()
    return {"harvested": (q.get("mutate") or {}).get("harvested", []),
            "new_queries": len((q.get("mutate") or {}).get("added", [])),
            **r, "candidates": c}


_ACTIONS = {"seeds": _step_seeds, "engines": _step_engines, "dedup": _step_dedup,
            "locate": _step_locate, "health": _step_health, "crawl": _step_crawl,
            "prospect": _step_prospect}


# ---------------- 编排 ----------------

def status(db, need_id: str | None = None) -> dict:
    need_id = need_id or _default_need()
    """首次流程的进度/结果。切页、刷新、重启都能接着看(状态在 AutoOpsRun 里)。"""
    rows = (db.query(AutoOpsRun)
            .filter(AutoOpsRun.need_id == need_id, AutoOpsRun.task.like(f"{TASK}:%"))
            .order_by(AutoOpsRun.started_at.asc()).all())
    latest: dict[str, AutoOpsRun] = {}
    for r in rows:
        latest[r.task.split(":", 1)[1]] = r
    steps = []
    for key, label, what in STEPS:
        r = latest.get(key)
        steps.append({
            "step": key, "label": label, "what": what,
            "status": r.status if r else "pending",
            "summary": (r.summary or {}) if r else {},
            "note": (r.note or "") if r else "",
            "at": r.started_at.isoformat(timespec="seconds") if r else None,
        })
    done_all = all(s["status"] in ("done", "skipped") for s in steps)
    return {"running": _running.is_set(), "steps": steps,
            "never": not rows, "completed": bool(rows) and done_all and not _running.is_set(),
            "precheck": precheck(db)}


def start(need_id: str | None = None, skip_crawl: bool = False) -> dict:
    need_id = need_id or _default_need()
    """启动首次流程(后台)。前置检查不通过直接拒绝,并说明该改什么。"""
    if _running.is_set():
        return {"started": False, "note": "首次流程已经在跑了"}
    db = SessionLocal()
    try:
        pc = precheck(db)
    finally:
        db.close()
    if not pc["ok"]:
        return {"started": False, "blocked": pc["blocked"], "precheck": pc,
                "note": "前置检查没过:" + ";".join(pc["blocked"])}
    _cancel.clear()
    _running.set()
    threading.Thread(target=_run, args=(need_id, skip_crawl), daemon=True).start()
    return {"started": True}


def cancel():
    _cancel.set()


def _run(need_id: str, skip_crawl: bool):
    global _need
    _need = need_id
    db = SessionLocal()
    try:
        for key, label, _what in STEPS:
            if _cancel.is_set():
                _rec(db, key, "skipped", note="流程被取消")
                break
            if key == "precheck":
                pc = precheck(db)
                _rec(db, key, "done", summary=pc,
                     note=";".join(x["title"] for x in pc["items"] if not x["ok"]))
                continue
            if key == "crawl" and skip_crawl:
                _rec(db, key, "skipped", note="按要求跳过首次采集")
                continue
            row = AutoOpsRun(need_id=need_id, task=f"{TASK}:{key}", status="running")
            try:
                db.add(row)
                db.commit()
            except Exception:  # noqa: BLE001
                db.rollback()
                continue
            try:
                summary = _ACTIONS[key](db) or {}
                row.status, row.summary = "done", summary
            except Exception as e:  # noqa: BLE001 一步失败不拦住后面的
                db.rollback()
                from app.services.errors import error_headline
                row.status, row.note = "failed", error_headline(e)[:2000]
            row.finished_at = datetime.utcnow()
            try:
                db.commit()
            except Exception:  # noqa: BLE001
                db.rollback()
    finally:
        _running.clear()
        db.close()
