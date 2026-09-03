"""每日自动化:进程内轻量调度(无需 Celery/Redis)。

到配置时点自动跑一轮采集(复用后台采集任务,进度/日志/诊断一致),采集收尾自动出日报,
再按需邮件推送。只在应用进程存活时生效;适合单机部署。多副本部署请改用外部定时器调用
POST /crawl/run + /digest/run,避免重复触发。
"""
import threading
from datetime import datetime

from app.config import settings
from app.db import SessionLocal
from app.models import CrawlJob

_thread: threading.Thread | None = None
_stop = threading.Event()


def _already_ran_today(db, need_id: str, day) -> bool:
    """今天是否已经"自动"跑过。只看自动任务(triggered_by 为空):手动试跑不应顶掉当天的自动采集。"""
    start = datetime(day.year, day.month, day.day)
    return db.query(CrawlJob).filter(CrawlJob.need_id == need_id,
                                     CrawlJob.started_at >= start,
                                     CrawlJob.triggered_by.is_(None)).first() is not None


def _tick():
    """每分钟检查:到点就跑当天该跑的事(采集 / 源库自动运维),已跑过的不重复。"""
    now = datetime.utcnow()
    for need_id in daily_need_ids():
        _daily_crawl(need_id, now)
        _autopilot(need_id, now)


def daily_need_ids() -> list[str]:
    """每日自动跑哪些需求:DAILY_NEED_ID 可逗号分隔多个;缺省=平台默认需求。"""
    raw = str(getattr(settings, "daily_need_id", "") or "")
    ids = [x.strip() for x in raw.split(",") if x.strip()]
    if ids:
        return ids
    from app.services import need_ctx
    return [need_ctx.default_need_id()]


def _task_runnable(db, need_id: str) -> bool:
    """任务模式:暂停/结束/未到期的任务不进自动化。"""
    from app.models import NeedProfile
    from app.services import tasklib
    np = db.get(NeedProfile, need_id)
    return np is not None and np.active and tasklib.is_runnable(np.config)


def _daily_crawl(need_id: str, now: datetime):
    if not settings.daily_auto_enabled or now.hour != int(settings.daily_auto_hour):
        return
    db = SessionLocal()
    try:
        if not _task_runnable(db, need_id) or _already_ran_today(db, need_id, now.date()):
            return
    finally:
        db.close()
    from app.services import crawl_runner
    crawl_runner.start_job(need_id, settings.daily_auto_limit_sources, user_id=None)


def _autopilot(need_id: str, now: datetime):
    """源库自动运维:到点检查有哪些维护任务到期了,自己跑掉,不用人按按钮。

    整理查重 / 给根域源定位栏目 / 体检与复检恢复 / 主动找源 / 试运行源自动定级,
    各有各的周期(见 services/autopilot.TASKS),每步都落 AutoOpsRun 记录可事后核对。
    """
    if not getattr(settings, "autopilot_enabled", True):
        return
    if now.hour != int(getattr(settings, "autopilot_hour", 4) or 4):
        return
    from app.services import autopilot
    if autopilot.is_running():
        return
    db = SessionLocal()
    try:
        if not _task_runnable(db, need_id) or not autopilot.due_tasks(db, need_id):
            return                       # 任务未激活,或今天没有到期的维护任务
    finally:
        db.close()
    autopilot.start_async(need_id)


def _loop():
    while not _stop.wait(60):  # 每 60s 检查一次
        try:
            _tick()
        except Exception:  # noqa: BLE001 调度线程绝不能崩
            pass


def start():
    """应用启动时调用:起后台调度线程(幂等)。"""
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="daily-scheduler", daemon=True)
    _thread.start()


def stop():
    _stop.set()


# 日报邮件推送已移到 notify(底座);这里保留同名转发,老调用方不受影响
from app.services.notify import deliver_email  # noqa: E402,F401
