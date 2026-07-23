"""URL 工具:归一化(C 能力 10.1)、跳转还原(C3)、identity_key 提取(eTLD+1 / 公众号)。"""
import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

# 常见跟踪参数,归一化时剔除
_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "spm", "from", "fr", "src", "share_token", "wxfrom", "scene", "chksm",
    "mpshare", "srcid", "ref", "_t", "timestamp",
}

# 简化版多段公共后缀(覆盖国内常见;生产可换 publicsuffix2 库)
_MULTI_SUFFIXES = {
    "com.cn", "net.cn", "org.cn", "gov.cn", "edu.cn", "ac.cn", "mil.cn",
    "com.hk", "org.hk", "co.jp", "co.uk",
}

_SEARCH_REDIRECT_HOSTS = {"www.baidu.com", "baidu.com", "link.zhihu.com", "weixin.sogou.com", "sogou.com"}


def normalize_url(url: str) -> str:
    """归一化:小写 host、去默认端口、剔跟踪参数、去 fragment、参数排序。"""
    url = url.strip()
    p = urlparse(url)
    scheme = (p.scheme or "http").lower()
    netloc = p.netloc.lower()
    for port in (":80", ":443"):
        if netloc.endswith(port):
            netloc = netloc[: -len(port)]
    query = urlencode(sorted(
        (k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
        if k.lower() not in _TRACKING_PARAMS
    ))
    path = p.path or "/"
    return urlunparse((scheme, netloc, path, "", query, ""))


def url_hash(url: str) -> str:
    return hashlib.sha256(normalize_url(url).encode()).hexdigest()


def registered_domain(host: str) -> str:
    """eTLD+1:源发现的 identity_key(网站)。"""
    host = host.lower().split(":")[0]
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    last2 = ".".join(parts[-2:])
    if last2 in _MULTI_SUFFIXES and len(parts) >= 3:
        return ".".join(parts[-3:])
    return last2


def identity_key_for(url: str, wechat_account: str | None = None) -> str:
    """站点/发布主体身份键(site_key):公众号用 mp:账号名,网站用注册域名。"""
    if wechat_account:
        return f"mp:{wechat_account.strip()}"
    host = urlparse(url).netloc
    return registered_domain(host)


def source_keys(kind: str, entry_url: str | None, adapter_config: dict | None = None,
                wechat_account: str | None = None) -> tuple[str | None, str | None]:
    """算一个源的 (site_key, identity_key)。

    site_key = 站点/主体身份(注册域 / mp:账号),同站不同栏目共享 → 可信度/发现/信誉按它算。
    identity_key = 采集目标唯一键(栏目粒度):公众号→mp:账号、站内检索(config.site)→site:域名、
                   页面型→归一化 entry_url。纯搜索引擎等无固定目标 → (None, None) 不参与去重。
    """
    cfg = adapter_config or {}
    acct = wechat_account or cfg.get("account")
    if acct:
        key = f"mp:{str(acct).strip()}"
        return key, key
    if cfg.get("site"):
        dom = str(cfg["site"]).strip()
        return dom, f"site:{dom}"
    if entry_url and entry_url.startswith("http"):
        dom = registered_domain(urlparse(entry_url).netloc)
        path = urlparse(entry_url).path or "/"
        # 根目录(无栏目路径):www/非www 都算"整站",目标键=站点键,避免把根当成不同栏目
        if path in ("", "/"):
            return dom, dom
        return dom, normalize_url(entry_url)
    return None, None


_URL_DATE_RES = [
    re.compile(r"/((?:19|20)\d{2})[-_/](\d{1,2})[-_/](\d{1,2})"),   # /2026-07/17 、/2026/07/17
    re.compile(r"/((?:19|20)\d{2})(\d{2})(\d{2})\d*/"),             # /20260722.../ 连写
    re.compile(r"[_-]((?:19|20)\d{2})(\d{2})(\d{2})"),              # art_20260722
]


def date_from_url(url: str):
    """从 URL 里提取发布日期(政务/新闻站常见 /YYYY-MM/DD/ 或 YYYYMMDD 连写)。取不到返回 None。"""
    from datetime import date as _date
    for rgx in _URL_DATE_RES:
        m = rgx.search(url or "")
        if m:
            try:
                y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
                if 1990 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31:
                    return _date(y, mo, d)
            except (ValueError, IndexError):
                continue
    return None


def is_search_redirect(url: str) -> bool:
    """是否为搜索引擎中间跳转链(C3:必须还原后再入库)。"""
    host = urlparse(url).netloc.lower()
    return any(host == h or host.endswith("." + h) for h in _SEARCH_REDIRECT_HOSTS) and (
        "/link" in url or "/url" in url or "url=" in url or "/lnk" in url
    )


_WECHAT_PERM_RE = re.compile(r"https?://mp\.weixin\.qq\.com/s[/?][^\s\"'<>]+")


def extract_wechat_permalink(html: str) -> str | None:
    """从页面中提取 mp.weixin.qq.com 永久链接(C8:搜狗临时链当刻永久化)。"""
    m = _WECHAT_PERM_RE.search(html or "")
    return m.group(0) if m else None


def query_hash(query: str) -> str:
    """C4 水位线的查询键:规范化(去多余空白)后哈希。"""
    normalized = re.sub(r"\s+", " ", query.strip().lower())
    return hashlib.sha256(normalized.encode()).hexdigest()
