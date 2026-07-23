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
    def __init__(self, job_id: int | None):
        self.job_id = job_id
        self.ref: str | None = None       # 当前处理的文档 URL/事件号,自动附到后续记录
        self.count = 0
        self._db = SessionLocal()

    def add(self, kind: str, summary: str = "", ref: str | None = None, detail: dict | None = None):
        try:
            self._db.add(RunTrace(
                job_id=self.job_id, kind=kind,
                ref=(ref or self.ref or "")[:400] or None,
                summary=(summary or "")[:2000] or None,
                detail=_truncate(detail) if detail else None,
            ))
            self.count += 1
            if self.count % 20 == 0:
                self._db.commit()
        except Exception:  # noqa: BLE001 诊断留痕绝不能影响主流程
            try:
                self._db.rollback()
            except Exception:  # noqa: BLE001
                pass

    def close(self):
        try:
            self._db.commit()
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._db.close()


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


def set_ref(ref: str | None):
    """设置当前处理对象(文档 URL/事件号),后续 record 未显式给 ref 时自动附上。"""
    rec = getattr(_local, "rec", None)
    if rec is not None:
        rec.ref = ref


def record(kind: str, summary: str = "", ref: str | None = None, detail: dict | None = None):
    """记一条诊断留痕。无活跃会话时为空操作。"""
    rec = getattr(_local, "rec", None)
    if rec is not None:
        rec.add(kind, summary, ref, detail)
