"""Celery 装配:beat 调度对应 scheduler 纯函数(逻辑不依赖 Celery,可独立测试)。"""
from celery import Celery
from celery.schedules import crontab

from app.config import settings

celery = Celery("sec_collection", broker=settings.redis_url, backend=settings.redis_url)

celery.conf.beat_schedule = {
    "daily-main": {  # 每日主任务(B1 抓取+处理+候选评分+线索刷新)
        "task": "tasks.run_daily",
        "schedule": crontab(hour="1,7,13,19", minute=0),  # A 级源节奏由 due_sources 内部控制
        "args": ("sec_events",),
    },
    "verify-archives-monthly": {
        "task": "tasks.verify_archives",
        "schedule": crontab(day_of_month=1, hour=3, minute=0),
    },
}


@celery.task(name="tasks.run_daily")
def run_daily(need_id: str):
    from app.db import SessionLocal
    from app.services.scheduler import run_daily as _run
    db = SessionLocal()
    try:
        return _run(db, need_id)
    finally:
        db.close()


@celery.task(name="tasks.verify_archives")
def verify_archives():
    from datetime import datetime
    from app.db import SessionLocal
    from app.models import ArchiveManifest
    from app.services.archive import verify_snapshot
    db = SessionLocal()
    try:
        bad = []
        for r in db.query(ArchiveManifest).all():
            ok = verify_snapshot(r)
            r.last_verified_at = datetime.utcnow()
            r.verify_ok = ok
            if not ok:
                bad.append(r.snapshot_id)
        db.commit()
        return {"checked": True, "corrupted": bad}
    finally:
        db.close()
