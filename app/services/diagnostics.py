"""端到端诊断留痕:线程本地记录器,把一次采集运行中"发生了什么"完整记下来供离线分析。

用法:采集批次外层 `with diagnostics.session(job_id):`,批次内任意深度调用 `record(...)`
即写入(无活跃会话则为空操作,零成本)。LLM 原始调用、粗筛/抽取/去重/建草稿的输入输出
都记入 run_trace 表,可整包下载分析。记录用独立 DB 会话,不干扰采集主事务。
"""
import threading
from contextlib import contextmanager

from app.db import SessionLocal
from app.models import RunTrace

_local = threading.local()

# detail 里超长字段(提示词/正文/返回)统一截断,防止单条爆量
_FIELD_CAP = 12000


def _truncate(v):
    if isinstance(v, str) and len(v) > _FIELD_CAP:
        return v[:_FIELD_CAP] + f"...(截断,共 {len(v)} 字)"
    if isinstance(v, dict):
        return {k: _truncate(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_truncate(x) for x in v[:50]]
    return v


class _Recorder:
    """并行处理时多线程共用一个记录器:内存缓冲 + 批量落库(每批用短会话,线程安全)。"""

    def __init__(self, job_id: int | None):
        self.job_id = job_id
        self.count = 0
        self._buf: list[dict] = []
        self._lock = threading.Lock()

    def add(self, kind: str, summary: str = "", ref: str | None = None, detail: dict | None = None):
        from datetime import datetime
        row = {"job_id": self.job_id, "kind": kind, "at": datetime.utcnow(),
               "ref": (ref or "")[:400] or None, "summary": (summary or "")[:2000] or None,
               "detail": _truncate(detail) if detail else None}
        with self._lock:
            self._buf.append(row)
            self.count += 1
            if len(self._buf) >= 100:
                self._flush_locked()

    def _flush_locked(self):
        if not self._buf:
            return
        rows, self._buf = self._buf, []
        db = SessionLocal()
        try:
            db.bulk_insert_mappings(RunTrace, rows)
            db.commit()
        except Exception:  # noqa: BLE001 诊断留痕绝不能影响主流程
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
        finally:
            db.close()

    def close(self):
        with self._lock:
            self._flush_locked()


@contextmanager
def session(job_id: int | None = None):
    """开启诊断会话。可安全嵌套(内层复用外层)。"""
    existing = getattr(_local, "rec", None)
    if existing is not None:
        yield existing
        return
    rec = _Recorder(job_id)
    _local.rec = rec
    try:
        yield rec
    finally:
        _local.rec = None
        rec.close()


def active() -> bool:
    return getattr(_local, "rec", None) is not None


def current():
    """返回本线程当前的记录器(供并行 worker 绑定复用);无则 None。"""
    return getattr(_local, "rec", None)


def bind(rec):
    """在 worker 线程里绑定主线程的记录器,使并行处理也能留痕。返回原绑定以便还原。"""
    prev = getattr(_local, "rec", None)
    _local.rec = rec
    _local.ref = None
    return prev


def set_ref(ref: str | None):
    """设置当前处理对象(文档 URL/事件号),后续 record 未显式给 ref 时自动附上(线程本地)。"""
    _local.ref = ref


def record(kind: str, summary: str = "", ref: str | None = None, detail: dict | None = None):
    """记一条诊断留痕。无活跃会话时为空操作。ref 缺省用本线程 set_ref 的值。"""
    rec = getattr(_local, "rec", None)
    if rec is not None:
        rec.add(kind, summary, ref or getattr(_local, "ref", None), detail)
