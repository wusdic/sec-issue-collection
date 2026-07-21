"""通用能力:发布主体信誉定级 + 转载溯源(领域无关,任何需求实例可复用)。

理念(通用信息搜索框架 · 质量模型的一部分):
- 一条记录的可信度,很大程度由"谁发布的"决定。把每个发布主体(公众号、发布机关、
  采购平台、媒体号…)登记进"主体信誉名录",按主体重定级,而非笼统给渠道级别。
- 二手加工内容(转载/转发)极常见,须识别并追溯原始出处,避免拿二手转述当一手。

需求画像声明自己的名录:
  sources:
    reputation_registry: config/wechat_accounts.yaml   # 主体→可信度
    repost_detection: true                             # 是否启用转载溯源

名录文件格式(accounts 或 subjects 均可,兼容公众号名录):
  subjects: { 主体名: {credibility: S1, category: 官方}, ... }
  blacklist: [ 垃圾主体名, ... ]   # 默认空;命中才丢弃,其余一律保留并定级
"""
import re

import yaml

_REG_CACHE: dict[str, dict] = {}


def normalize_subject(name: str | None) -> str:
    return (name or "").strip().strip("@").strip("「」『』\"' ")


def load_registry(path) -> dict:
    """加载并缓存一个需求的主体信誉名录。缺文件返回空名录(等价全部走渠道默认)。"""
    key = str(path)
    if key not in _REG_CACHE:
        try:
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except FileNotFoundError:
            data = {}
        raw = data.get("subjects") or data.get("accounts") or {}
        _REG_CACHE[key] = {
            "subjects": {normalize_subject(k): v for k, v in raw.items()},
            "blacklist": {normalize_subject(x) for x in (data.get("blacklist") or [])},
        }
    return _REG_CACHE[key]


def reload():
    _REG_CACHE.clear()


def subject_credibility(registry: dict | None, name: str | None, default: str = "S4") -> str:
    """按发布主体重定级:命中名录用其等级,否则回退渠道默认(默认保留,不丢)。"""
    rec = (registry or {}).get("subjects", {}).get(normalize_subject(name))
    return rec["credibility"] if rec and rec.get("credibility") else default


def is_blacklisted(registry: dict | None, name: str | None) -> bool:
    n = normalize_subject(name)
    return bool(n) and n in (registry or {}).get("blacklist", set())


# ---- 转载 / 引用溯源(通用文本模式,与领域无关) ----

_REPOST_PATTERNS = [
    re.compile(r"(?:本文)?(?:转载|转自|来源|文章来源|素材来源)[自：:\s]*[「『\"]?([^\s，,。;；\n「」『』\"]{2,30})"),
    re.compile(r"转载自(?:公众号|公号|微信公众号)?[「『\"]?([^\s，,。「」『』\"]{2,30})"),
    re.compile(r"以上内容(?:转载)?来自[「『\"]?([^\s，,。「」『』\"]{2,30})"),
]
_ORIGIN_LINK_RE = re.compile(r"原文(?:链接|地址)[：:\s]*(https?://[^\s\"'<>]+)")
_WECHAT_LINK_RE = re.compile(r"https?://mp\.weixin\.qq\.com/s[/?][^\s\"'<>]+")
_ORIGINAL_MARKERS = re.compile(r"原创声明|本号原创|未经授权(?:禁止|不得)转载|首发于本号")


def detect_repost(text: str) -> dict:
    """检测记录是否为转载并尽力提取原始出处。命中原创声明则判非转载(保守)。

    返回 {is_repost, original_subject, original_url, original_wechat_url}。
    """
    text = text or ""
    head = text[:1500] + "\n" + text[-1500:]  # 转载声明常在头尾
    result = {"is_repost": False, "original_subject": None,
              "original_url": None, "original_wechat_url": None}
    if _ORIGINAL_MARKERS.search(head):
        return result
    for pat in _REPOST_PATTERNS:
        m = pat.search(head)
        if m:
            subj = re.sub(r"^(?:公众号|公号|微信公众号)", "", normalize_subject(m.group(1))).strip("「」『』\"' ")
            if subj and subj not in ("网络", "互联网", "该公众号") and len(subj) >= 2:
                result["is_repost"] = True
                result["original_subject"] = subj
                break
    link = _ORIGIN_LINK_RE.search(head)
    if link:
        result["is_repost"] = True
        result["original_url"] = link.group(1)
    wl = _WECHAT_LINK_RE.findall(head)
    if wl:
        result["original_wechat_url"] = wl[0]
    return result


def registry_path_for(need_config: dict):
    """从需求画像取该需求的名录路径与转载检测开关。"""
    from app.config import BASE_DIR
    sources = need_config.get("sources") or {}
    reg = sources.get("reputation_registry")
    path = (BASE_DIR / reg) if reg else None
    return path, bool(sources.get("repost_detection", True))
