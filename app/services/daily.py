"""每日自动化:进程内轻量调度(无需 Celery/Redis)。

到配置时点自动跑一轮采集(复用后台采集任务,进度/日志/诊断一致),采集收尾自动出日报,
再按需邮件推送。只在应用进程存活时生效;适合单机部署。多副本部署请改用外部定时器调用
POST /crawl/run + /digest/run,避免重复触发。
"""
import threading
import time
from datetime import datetime

from app.config import settings
from app.db import SessionLocal
from app.models import CrawlJob

_thread: threading.Thread | None = None
_stop = threading.Event()


def _already_ran_today(db, need_id: str, day) -> bool:
    start = datetime(day.year, day.month, day.day)
    return db.query(CrawlJob).filter(CrawlJob.need_id == need_id,
                                     CrawlJob.started_at >= start).first() is not None


def _tick():
    """每分钟检查:到点且今天还没自动跑过 → 起一轮采集。"""
    if not settings.daily_auto_enabled:
        return
    now = datetime.utcnow()
    if now.hour != int(settings.daily_auto_hour):
        return
    need_id = settings.daily_need_id
    db = SessionLocal()
    try:
        if _already_ran_today(db, need_id, now.date()):
            return
    finally:
        db.close()
    from app.services import crawl_runner
    crawl_runner.start_job(need_id, settings.daily_auto_limit_sources, user_id=None)


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


# ---------------- 日报邮件推送(可选) ----------------

def deliver_email(subject: str, body_md: str) -> tuple[bool, str]:
    """把日报 Markdown 作为纯文本邮件推送。未配置 SMTP 或收件人则跳过。"""
    if not (settings.smtp_host and settings.digest_email_to):
        return False, "未配置 SMTP 或收件人,跳过邮件推送(日报仍可页面查看/下载)"
    import smtplib
    from email.mime.text import MIMEText
    to_list = [x.strip() for x in settings.digest_email_to.split(",") if x.strip()]
    msg = MIMEText(body_md, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = settings.smtp_from or settings.smtp_user
    msg["To"] = ", ".join(to_list)
    try:
        if int(settings.smtp_port) == 465:
            srv = smtplib.SMTP_SSL(settings.smtp_host, int(settings.smtp_port), timeout=20)
        else:
            srv = smtplib.SMTP(settings.smtp_host, int(settings.smtp_port), timeout=20)
            srv.starttls()
        try:
            if settings.smtp_user:
                srv.login(settings.smtp_user, settings.smtp_password)
            srv.sendmail(msg["From"], to_list, msg.as_string())
        finally:
            srv.quit()
        return True, f"已推送至 {len(to_list)} 个收件人"
    except Exception as e:  # noqa: BLE001
        return False, f"邮件推送失败:{e}"
