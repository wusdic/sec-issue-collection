"""公众号数据源处理 —— 通用能力 reputation 的一个便捷封装(按默认需求的信誉名录)。

核心逻辑已泛化到 app/services/reputation.py(任何需求可复用:政策库按发布机关定级、
招标库按采购平台定级…)。本模块保留公众号语义的便捷入口与向后兼容 API。
默认策略:公众号一律保留并按主体定级,黑名单仅用于真正的垃圾号(默认空)。
"""
import re
from urllib.parse import urlparse

from app.config import settings
from app.services import reputation

_WECHAT_REGISTRY = settings.config_dir / "wechat_accounts_sec.yaml"

WECHAT_HOSTS = ("mp.weixin.qq.com",)


def is_wechat_article_url(url: str | None) -> bool:
    """是否是公众号文章链接(形如 https://mp.weixin.qq.com/s/XXXX 或 /s?__biz=...)。"""
    if not url or not url.startswith("http"):
        return False
    p = urlparse(url)
    return p.netloc.lower() in WECHAT_HOSTS and p.path.startswith("/s")


# 公众号名在文章页里的常见位置(不同版式/改版都覆盖一遍,取第一个命中的)
_ACCOUNT_PATTERNS = [
    re.compile(r'var\s+nickname\s*=\s*["\']([^"\']{1,64})["\']'),
    re.compile(r'var\s+user_name\s*=\s*["\']([^"\']{1,64})["\']'),
    re.compile(r'id="js_name"[^>]*>\s*([^<]{1,64}?)\s*<'),
    re.compile(r'property="og:article:author"\s+content="([^"]{1,64})"'),
    re.compile(r'"nickname"\s*:\s*"([^"]{1,64})"'),
    re.compile(r'rawNickname\s*=\s*["\']([^"\']{1,64})["\']'),
]
_TITLE_PATTERNS = [
    re.compile(r'property="og:title"\s+content="([^"]{1,200})"'),
    re.compile(r'id="activity-name"[^>]*>\s*([^<]{1,200}?)\s*<'),
    re.compile(r'var\s+msg_title\s*=\s*["\']([^"\']{1,200})["\']'),
]


def _first_match(patterns, html: str) -> str | None:
    for rgx in patterns:
        m = rgx.search(html or "")
        if m:
            val = (m.group(1) or "").strip()
            if val and val not in ("undefined", "null"):
                return val
    return None


def article_meta(html: str) -> dict:
    """从公众号文章 HTML 解析 {account, title}(解析不到为 None)。"""
    return {"account": _first_match(_ACCOUNT_PATTERNS, html),
            "title": _first_match(_TITLE_PATTERNS, html)}


def resolve_account(url: str, fetch=None) -> dict:
    """抓一篇公众号文章,解析出它属于哪个公众号。

    返回 {account, title, ok, error}。用于「粘贴一条公众号文章链接即可把该号加为数据源」。
    """
    from app.services import fetcher as _fetcher
    fetch = fetch or _fetcher.fetch
    # 公众号页面重前端渲染,允许自动降级到浏览器渲染(开了才生效)
    fr = fetch(url, render="auto")
    if not getattr(fr, "ok", False):
        return {"account": None, "title": None, "ok": False,
                "error": f"抓取失败:{getattr(fr, 'error', None) or getattr(fr, 'status', '')}"}
    meta = article_meta(fr.html or "")
    if not meta["account"]:
        return {**meta, "ok": False, "error": "未能从文章页解析出公众号名称(可能需开启浏览器渲染)"}
    return {**meta, "ok": True, "error": None}


def _reg() -> dict:
    return reputation.load_registry(_WECHAT_REGISTRY)


def reload_accounts():
    reputation.reload()


def normalize_account(name):
    return reputation.normalize_subject(name)


def is_blacklisted(account):
    return reputation.is_blacklisted(_reg(), account)


def account_credibility(account, channel_default="S4"):
    return reputation.subject_credibility(_reg(), account, channel_default)


def detect_repost(text):
    r = reputation.detect_repost(text)
    # 兼容旧字段名 original_account
    r["original_account"] = r.get("original_subject")
    return r


def is_wechat_source(source, doc_publisher=None):
    if source is not None and getattr(source, "adapter", "") in ("sogou_wechat",):
        return True
    return bool(doc_publisher) and normalize_account(doc_publisher) in _reg()["subjects"]
