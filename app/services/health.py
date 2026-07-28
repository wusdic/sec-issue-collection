"""源体检:后台批量试抓 + 可查询进度。

此前体检在浏览器里逐个同步请求,源多时要跑几十分钟、切页就断、也看不到进度。
改为后台线程执行,进度存在进程内(切页/刷新都能查),可取消。
"""
import threading
from datetime import datetime

from app.config import settings
from app.db import SessionLocal
from app.models import Source

_lock = threading.Lock()
_state: dict = {"running": False}
_cancel = threading.Event()


def status() -> dict:
    with _lock:
        return dict(_state)


def cancel():
    _cancel.set()


def _set(**kw):
    with _lock:
        _state.update(kw)


def start(need_id: str) -> dict:
    """启动体检(幂等:已有任务在跑则直接返回其状态)。"""
    with _lock:
        if _state.get("running"):
            return dict(_state)
        _cancel.clear()
        _state.clear()
        _state.update({"running": True, "total": 0, "done": 0, "ok": 0, "fail": 0,
                       "retired": 0, "current": "", "need_id": need_id,
                       "started_at": datetime.utcnow().isoformat(timespec="seconds"),
                       "finished_at": None, "canceled": False, "results": []})
    threading.Thread(target=_run, args=(need_id,), daemon=True).start()
    return status()


def _run(need_id: str):
    db = SessionLocal()
    try:
        srcs = [s for s in db.query(Source).filter(Source.lifecycle.in_(["active", "trial"])).all()
                if need_id in (s.serves_needs or [])]
        _set(total=len(srcs))
        from app.api.routes import test_fetch_source
        for s in srcs:
            if _cancel.is_set():
                _set(canceled=True)
                break
            _set(current=s.name)
            try:
                r = test_fetch_source(s.id, q=None, mark=True, db=db, _=None)
                good = bool(r.get("ok")) and int(r.get("count") or 0) > 0
                with _lock:
                    _state["ok" if good else "fail"] += 1
                    if r.get("retired"):
                        _state["retired"] += 1
                    _state["results"].append({
                        "id": s.id, "name": s.name, "ok": good,
                        "count": r.get("count", 0), "retired": bool(r.get("retired")),
                        "hint": r.get("hint") or r.get("error") or ""})
            except Exception as e:  # noqa: BLE001 单源异常不终止体检
                with _lock:
                    _state["fail"] += 1
                    _state["results"].append({"id": s.id, "name": s.name, "ok": False,
                                              "count": 0, "retired": False,
                                              "hint": f"{type(e).__name__}: {e}"[:160]})
            with _lock:
                _state["done"] += 1
    except Exception as e:  # noqa: BLE001
        _set(error=str(e)[:300])
    finally:
        _set(running=False, current="",
             finished_at=datetime.utcnow().isoformat(timespec="seconds"))
        db.close()


def restore(db, source_id: int) -> Source:
    """恢复被(误)停用的源:重新启用并清零失败计数,避免刚恢复又被历史计数立刻停用。"""
    src = db.get(Source, source_id)
    if not src:
        return None
    src.lifecycle = "active"
    src.fail_streak = 0
    cfg = dict(src.adapter_config or {})
    cfg.pop("auto_merged", None)      # 人工恢复后不再被启动查重自动并掉
    cfg["manually_restored"] = True
    src.adapter_config = cfg
    if src.note and "[自动查重" in src.note:
        src.note = src.note.replace(" [自动查重:并入同采集目标的源]", "") or None
    db.flush()
    return src
