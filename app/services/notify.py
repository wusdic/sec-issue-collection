"""通知功能组件(底座):把一段文本按渠道送出去。渠道是插件,平台本身不关心"通知给谁、走哪条路"。

- 渠道注册表 CHANNELS:email(SMTP)/ feishu(飞书群机器人)/ webhook(通用 JSON POST,可接钉钉、企微、自建)。
  新渠道:写一个 fn(subject, body, cfg) -> (ok, note) 登记进 CHANNELS 即可。
- 用哪些渠道:画像 outputs.notify.channels(如 [email, feishu, {kind: webhook, url: …}]);
  没声明就用"运行时设置里配置齐全"的渠道(smtp_host+收件人 → email;feishu_webhook → feishu)。
- 只被输出层(日报/告警)与能力 notify.send 调用;它不是平台主线,平台主线是"找得快、找得全、真实、好用、分类存本地"。
"""
from __future__ import annotations

from app.config import settings


def deliver_email(subject: str, body_md: str, cfg: dict | None = None) -> tuple[bool, str]:
    """SMTP 纯文本邮件。未配置 SMTP 或收件人则跳过。"""
    cfg = cfg or {}
    host = cfg.get("host") or settings.smtp_host
    to = cfg.get("to") or settings.digest_email_to
    if not (host and to):
        return False, "未配置 SMTP 或收件人,跳过邮件推送(日报仍可页面查看/下载)"
    import smtplib
    from email.mime.text import MIMEText
    port = int(cfg.get("port") or settings.smtp_port)
    user = cfg.get("user") or settings.smtp_user
    password = cfg.get("password") or settings.smtp_password
    to_list = [x.strip() for x in str(to).split(",") if x.strip()]
    msg = MIMEText(body_md, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = cfg.get("from") or settings.smtp_from or user
    msg["To"] = ", ".join(to_list)
    try:
        if port == 465:
            srv = smtplib.SMTP_SSL(host, port, timeout=20)
        else:
            srv = smtplib.SMTP(host, port, timeout=20)
            srv.starttls()
        try:
            if user:
                srv.login(user, password)
            srv.sendmail(msg["From"], to_list, msg.as_string())
        finally:
            srv.quit()
        return True, f"已推送至 {len(to_list)} 个收件人"
    except Exception as e:  # noqa: BLE001
        return False, f"邮件推送失败:{e}"


def _post_json(url: str, payload: dict) -> tuple[bool, str]:
    try:
        import httpx
        r = httpx.post(url, json=payload, timeout=15)
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        code = data.get("code", data.get("errcode", data.get("StatusCode", 0)))
        ok = r.status_code < 300 and int(code or 0) == 0
        return ok, ("已推送" if ok else f"返回 {r.status_code} {str(data)[:120]}")
    except Exception as e:  # noqa: BLE001
        return False, f"推送失败:{e}"


def deliver_feishu(text: str, webhook: str | None = None, cfg: dict | None = None) -> tuple[bool, str]:
    """飞书群自定义机器人。未配置 webhook 则跳过。"""
    hook = webhook or (cfg or {}).get("url") or getattr(settings, "feishu_webhook", "")
    if not hook:
        return False, "未配置飞书 webhook,跳过"
    ok, note = _post_json(hook, {"msg_type": "text", "content": {"text": text[:4000]}})
    return ok, ("已推送飞书" if ok else f"飞书{note}")


def deliver_webhook(subject: str, body: str, cfg: dict | None = None) -> tuple[bool, str]:
    """通用 webhook:POST {subject, text, markdown};钉钉/企微机器人用 template 指定包体形状。"""
    cfg = cfg or {}
    url = cfg.get("url")
    if not url:
        return False, "webhook 渠道未配置 url,跳过"
    tpl = str(cfg.get("template") or "generic")
    text = f"{subject}\n\n{body}"[:4000]
    if tpl == "dingtalk":
        payload = {"msgtype": "text", "text": {"content": text}}
    elif tpl == "wecom":
        payload = {"msgtype": "text", "text": {"content": text}}
    else:
        payload = {"subject": subject, "text": body, "markdown": body}
    return _post_json(url, payload)


# 渠道注册表:名字 → fn(subject, body, cfg) -> (ok, note)
CHANNELS = {
    "email": lambda subject, body, cfg: deliver_email(subject, body, cfg),
    "feishu": lambda subject, body, cfg: deliver_feishu(f"{subject}\n\n{body}", cfg=cfg),
    "webhook": deliver_webhook,
}


def configured_channels() -> list[dict]:
    """运行时设置里配置齐全的渠道(画像没声明时的缺省)。"""
    out = []
    if settings.smtp_host and settings.digest_email_to:
        out.append({"kind": "email"})
    if getattr(settings, "feishu_webhook", ""):
        out.append({"kind": "feishu"})
    return out


def channels_for(ctx=None) -> list[dict]:
    """画像 outputs.notify.channels → [{kind, ...cfg}];未声明 → 已配置的渠道。"""
    declared = None
    if ctx is not None:
        declared = ((ctx.outputs.get("notify") or {}).get("channels")) if isinstance(ctx.outputs, dict) else None
    if not declared:
        return configured_channels()
    out = []
    for ch in declared:
        if isinstance(ch, str):
            out.append({"kind": ch})
        elif isinstance(ch, dict) and ch.get("kind"):
            out.append(dict(ch))
    return out


def send(subject: str, body: str, channels: list | None = None, ctx=None) -> dict:
    """按渠道列表(或画像/设置缺省)发送;返回每个渠道的结果。"""
    chs = channels if channels is not None else channels_for(ctx)
    chs = [({"kind": c} if isinstance(c, str) else dict(c)) for c in chs]
    results = {}
    for i, ch in enumerate(chs):
        kind = str(ch.get("kind") or "")
        fn = CHANNELS.get(kind)
        key = kind if kind not in results else f"{kind}#{i}"
        if fn is None:
            results[key] = {"ok": False, "note": f"未知渠道 {kind}"}
            continue
        try:
            ok, note = fn(subject, body, ch)
        except Exception as e:  # noqa: BLE001
            ok, note = False, f"渠道异常:{e}"
        results[key] = {"ok": bool(ok), "note": note}
    return results


def deliver_all(subject: str, body_md: str, ctx=None) -> dict:
    """兼容旧名:按画像/设置缺省渠道全部推一遍。"""
    return send(subject, body_md, None, ctx)
