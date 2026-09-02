"""通知底座:邮件推送(SMTP)。输出层(日报)与运行层(每日调度)都只依赖这里,互不引用。"""
from app.config import settings


def deliver_email(subject: str, body_md: str) -> tuple[bool, str]:
    """把 Markdown 作为纯文本邮件推送。未配置 SMTP 或收件人则跳过。"""
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
