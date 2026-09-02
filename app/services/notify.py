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


def deliver_feishu(text: str, webhook: str | None = None) -> tuple[bool, str]:
    """飞书群自定义机器人:把文本推到群里。未配置 webhook 则跳过。"""
    hook = webhook or getattr(settings, "feishu_webhook", "")
    if not hook:
        return False, "未配置飞书 webhook,跳过"
    try:
        import httpx
        r = httpx.post(hook, json={"msg_type": "text", "content": {"text": text[:4000]}}, timeout=15)
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        ok = r.status_code == 200 and int(data.get("code", data.get("StatusCode", 0)) or 0) == 0
        return ok, ("已推送飞书" if ok else f"飞书返回 {r.status_code} {str(data)[:120]}")
    except Exception as e:  # noqa: BLE001
        return False, f"飞书推送失败:{e}"


def deliver_all(subject: str, body_md: str) -> dict:
    """按已配置的渠道全部推一遍(邮件 + 飞书),返回各渠道结果。"""
    out = {}
    ok, msg = deliver_email(subject, body_md)
    out["email"] = {"ok": ok, "note": msg}
    ok2, msg2 = deliver_feishu(f"{subject}\n\n{body_md}")
    out["feishu"] = {"ok": ok2, "note": msg2}
    return out
