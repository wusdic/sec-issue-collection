"""公众号数据源处理:按主体可信度重定级 + 转载溯源。

理念:公众号是"别人已做完筛选与事实梳理的二手加工品"——白捡人力,
但必须解决两件事:
  ① 按发布号主体给可信度定级(官方号可直接用,营销号丢弃);
  ② 识别转载并追回原始出处(否则拿的是二手转述,可信度/时效打折)。
"""
import re

import yaml

from app.config import settings

_ACCOUNTS: dict | None = None


def _load() -> dict:
    global _ACCOUNTS
    if _ACCOUNTS is None:
        path = settings.config_dir / "wechat_accounts.yaml"
        try:
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except FileNotFoundError:
            data = {}
        _ACCOUNTS = {
            "accounts": data.get("accounts", {}) or {},
            "blacklist": set(data.get("blacklist", []) or []),
        }
    return _ACCOUNTS


def reload_accounts():
    global _ACCOUNTS
    _ACCOUNTS = None


def normalize_account(name: str | None) -> str:
    return (name or "").strip().strip("@").strip()


def is_blacklisted(account: str | None) -> bool:
    acc = normalize_account(account)
    return bool(acc) and acc in _load()["blacklist"]


def account_credibility(account: str | None, channel_default: str = "S4") -> str:
    """按公众号主体重定级:命中名录用其等级,否则回退渠道默认(S4)。"""
    acc = normalize_account(account)
    rec = _load()["accounts"].get(acc)
    return rec["credibility"] if rec and rec.get("credibility") else channel_default


# 公众号转载/来源声明的常见句式(用于溯源到原始出处)
_REPOST_PATTERNS = [
    re.compile(r"(?:本文)?(?:转载|转自|来源|文章来源|素材来源)[自：:\s]*[「『\"]?([^\s，,。;；\n「」『』\"]{2,30})"),
    re.compile(r"转载自公众号[「『\"]?([^\s，,。「」『』\"]{2,30})"),
    re.compile(r"以上内容(?:转载)?来自[「『\"]?([^\s，,。「」『』\"]{2,30})"),
]
_ORIGIN_LINK_RE = re.compile(r"原文(?:链接|地址)[：:\s]*(https?://[^\s\"'<>]+)")
_WECHAT_LINK_RE = re.compile(r"https?://mp\.weixin\.qq\.com/s[/?][^\s\"'<>]+")

# 声明"原创"的信号(命中则基本可判为首发,不是转载)
_ORIGINAL_MARKERS = re.compile(r"原创声明|本号原创|未经授权(?:禁止|不得)转载|首发于本号")


def detect_repost(text: str) -> dict:
    """检测公众号文章是否为转载,尽力提取原始出处(账号名/原文链接)。

    返回 {is_repost, original_account, original_url, original_wechat_url}。
    判定保守:命中原创声明则直接判非转载。
    """
    text = text or ""
    head = text[:1500] + "\n" + text[-1500:]  # 转载声明常在开头或结尾
    result = {"is_repost": False, "original_account": None,
              "original_url": None, "original_wechat_url": None}
    if _ORIGINAL_MARKERS.search(head):
        return result
    for pat in _REPOST_PATTERNS:
        m = pat.search(head)
        if m:
            acc = normalize_account(m.group(1))
            acc = re.sub(r"^(?:公众号|公号|微信公众号)", "", acc).strip("「」『』\"' ")
            # 过滤明显不是来源名的词
            if acc and acc not in ("网络", "互联网", "该公众号") and len(acc) >= 2:
                result["is_repost"] = True
                result["original_account"] = acc
                break
    link = _ORIGIN_LINK_RE.search(head)
    if link:
        result["is_repost"] = True
        result["original_url"] = link.group(1)
    wl = _WECHAT_LINK_RE.findall(head)
    if wl:
        result["original_wechat_url"] = wl[0]
    return result


def is_wechat_source(source, doc_publisher: str | None = None) -> bool:
    """判断一篇文档是否来自公众号(用于是否走公众号定级/溯源)。"""
    if source is not None and getattr(source, "adapter", "") in ("sogou_wechat",):
        return True
    return bool(doc_publisher) and normalize_account(doc_publisher) in _load()["accounts"]
