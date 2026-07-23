"""站点栏目自动发现:根域页面型源不去抓首页要闻,而是自动找出与需求相关的栏目并抓栏目。

政务/机构站栏目多且会变动,人工补 URL 不现实也不准。这里从站点导航/首页链接里,按相关词
自动识别"执法处罚/网络安全通报/数据安全/漏洞预警"等栏目,交给通用列表适配器分别采集。
动态站每次采集重新识别,栏目变了也能感知。纯词法打分,不依赖 LLM,快且稳。
"""
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from app.config import settings
from app.services import fetcher, url_tools

# 栏目相关词(命中越多越可能是目标栏目);与关键词矩阵的事件/后果词叠加
COLUMN_HINT_WORDS = [
    "执法", "处罚", "通报", "曝光", "案例", "网络安全", "数据安全", "信息安全", "个人信息",
    "漏洞", "预警", "情况通报", "违法违规", "查处", "打击", "净网", "监管", "处置", "事件",
    "安全", "泄露", "举报", "整治", "行政处罚", "监督管理", "风险提示", "安全通告", "公告",
]
# 明显无关的栏目词(直接排除)
COLUMN_STOP_WORDS = [
    "招聘", "关于我们", "联系", "网站地图", "版权", "登录", "注册", "English", "简介",
    "机构设置", "领导", "党建", "会议", "视频", "图片", "专题", "首页", "邮箱", "服务",
]


def is_root_only(url: str | None) -> bool:
    """入口链接是否只是站点根目录(无具体栏目路径)。"""
    if not url or not url.startswith("http"):
        return False
    path = (urlparse(url).path or "/").strip("/")
    return path == ""


def _score(anchor: str, href_path: str, extra_terms: list[str]) -> int:
    blob = anchor
    if any(w in blob for w in COLUMN_STOP_WORDS):
        return -1
    score = sum(1 for w in COLUMN_HINT_WORDS if w in blob)
    score += sum(1 for t in extra_terms if t and t in blob)
    # 路径里带 zhifa/chufa/tongbao/aqbao 等拼音/栏目段也加分(弱信号)
    if any(seg in href_path for seg in ("zhifa", "chufa", "tongbao", "aqfa", "wangan", "anquan")):
        score += 1
    return score


def find_columns(html: str, base_url: str, extra_terms: list[str] | None = None,
                 limit: int | None = None) -> list[dict]:
    """从页面 HTML 找同域相关栏目链接。返回 [{url, anchor, score}],按分降序,已按栏目URL去重。"""
    extra_terms = extra_terms or []
    limit = limit or settings.auto_column_max
    soup = BeautifulSoup(html or "", "lxml")
    base_dom = url_tools.registered_domain(urlparse(base_url).netloc)
    seen: dict[str, dict] = {}
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        full = url_tools.normalize_url(_abs(base_url, href))
        if not full.startswith("http"):
            continue
        if url_tools.registered_domain(urlparse(full).netloc) != base_dom:
            continue  # 只要本站栏目
        if is_root_only(full):
            continue  # 跳过根链接自身
        anchor = a.get_text(" ", strip=True)[:40]
        if not anchor or len(anchor) < 2:
            continue
        sc = _score(anchor, urlparse(full).path.lower(), extra_terms)
        if sc <= 0:
            continue
        prev = seen.get(full)
        if not prev or sc > prev["score"]:
            seen[full] = {"url": full, "anchor": anchor, "score": sc}
    ranked = sorted(seen.values(), key=lambda x: -x["score"])
    return ranked[:limit]


def _abs(base: str, href: str) -> str:
    from urllib.parse import urljoin
    return urljoin(base, href)


def discover_columns(source, extra_terms: list[str] | None = None) -> list[dict]:
    """抓根页 → 找相关栏目。返回栏目列表(可能为空)。"""
    if not source.entry_url:
        return []
    fr = fetcher.fetch(source.entry_url, render=(source.adapter_config or {}).get("render", "auto"))
    if not fr.ok:
        return []
    return find_columns(fr.html, fr.final_url or source.entry_url, extra_terms)
