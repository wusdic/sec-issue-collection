"""批量「精准定位栏目」:把只填了网站根地址的源,后台逐个定位到具体栏目。

数据源必须精准到具体栏目(或能精准定位相关内容的页面集合),否则抓到的是首页要闻,
噪声极大。单个源可在列表里点「定位栏目」;源多时用这里的后台批量任务:
逐站抓根页 → 识别候选栏目 → 验证(篇数/结构一致性/内容相关度)→ 通过的落库为子源。
进度存在进程内,切页/刷新都能查,可取消。与体检任务同构。
"""
import threading
from datetime import datetime

from app.db import SessionLocal
from app.models import Source
from app.services import columns

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


def pending(db, need_id: str) -> list[Source]:
    """还没精准到栏目的源:页面型 + 入口只有根地址 + 尚无已识别栏目。"""
    out = []
    for s in db.query(Source).filter(Source.lifecycle.in_(["active", "trial"])).all():
        if need_id not in (s.serves_needs or []):
            continue
        if s.kind != "page" or not columns.is_root_only(s.entry_url):
            continue
        if columns.precision_of(s, db)["precise"]:
            continue
        out.append(s)
    return out


def start(need_id: str, force: bool = False) -> dict:
    """启动批量定位(幂等:已有任务在跑则直接返回其状态)。force=重新定位已定位过的源。"""
    with _lock:
        if _state.get("running"):
            return dict(_state)
        _cancel.clear()
        _state.clear()
        _state.update({"running": True, "total": 0, "done": 0, "located": 0, "failed": 0,
                       "columns": 0, "current": "", "need_id": need_id,
                       "started_at": datetime.utcnow().isoformat(timespec="seconds"),
                       "finished_at": None, "canceled": False, "results": []})
    threading.Thread(target=_run, args=(need_id, force), daemon=True).start()
    return status()


def _run(need_id: str, force: bool):
    db = SessionLocal()
    try:
        if force:
            srcs = [s for s in db.query(Source).filter(Source.lifecycle.in_(["active", "trial"])).all()
                    if need_id in (s.serves_needs or []) and s.kind == "page"
                    and columns.is_root_only(s.entry_url)]
        else:
            srcs = pending(db, need_id)
        _set(total=len(srcs))
        for s in srcs:
            if _cancel.is_set():
                _set(canceled=True)
                break
            _set(current=s.name)
            try:
                if force:      # 清掉 TTL 时间戳,强制重新识别
                    cfg = dict(s.adapter_config or {})
                    cfg.pop("columns_discovered_at", None)
                    s.adapter_config = cfg
                kids, _re = columns.discover_and_persist(db, s)
                with _lock:
                    _state["located" if kids else "failed"] += 1
                    _state["columns"] += len(kids)
                    _state["results"].append({
                        "id": s.id, "name": s.name, "count": len(kids),
                        "columns": [{"name": k.name, "url": k.entry_url} for k in kids[:10]],
                        "hint": "" if kids else "未识别到内容相关的栏目;采集时将按 site:域名 站内检索兜底"})
            except Exception as e:  # noqa: BLE001 单源异常不终止批次
                try:
                    db.rollback()
                except Exception:  # noqa: BLE001
                    pass
                with _lock:
                    _state["failed"] += 1
                    _state["results"].append({"id": s.id, "name": s.name, "count": 0,
                                              "columns": [],
                                              "hint": f"{type(e).__name__}: {e}"[:160]})
            with _lock:
                _state["done"] += 1
    except Exception as e:  # noqa: BLE001
        _set(error=str(e)[:300])
    finally:
        _set(running=False, current="",
             finished_at=datetime.utcnow().isoformat(timespec="seconds"))
        db.close()
