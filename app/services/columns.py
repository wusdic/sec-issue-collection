"""站点栏目自动发现:根域页面型源不去抓首页要闻,而是自动找出与需求相关的栏目并抓栏目。

政务/机构站栏目多且会变动,人工补 URL 不现实也不准。这里从站点导航/首页链接里,按相关词
自动识别"执法处罚/网络安全通报/数据安全/漏洞预警"等栏目,交给通用列表适配器分别采集。
动态站每次采集重新识别,栏目变了也能感知。纯词法打分,不依赖 LLM,快且稳。
"""
import re
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


# ---------------- 栏目验证:文章一致性 ----------------

_NUM_SEG = re.compile(r"\d+")


def _article_links(html: str, base_url: str) -> list[str]:
    """栏目页里比栏目更深一层的同域文章链接(归一化去重)。"""
    soup = BeautifulSoup(html or "", "lxml")
    base_dom = url_tools.registered_domain(urlparse(base_url).netloc)
    base_depth = len([p for p in urlparse(base_url).path.split("/") if p])
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        full = url_tools.normalize_url(_abs(base_url, href))
        if not full.startswith("http") or full in seen:
            continue
        if url_tools.registered_domain(urlparse(full).netloc) != base_dom:
            continue
        depth = len([p for p in urlparse(full).path.split("/") if p])
        if depth <= base_depth:          # 只算比栏目更深的文章页
            continue
        seen.add(full)
        out.append(full)
    return out


def _signature(url: str) -> str:
    """文章 URL 结构签名:目录前缀 + 数字段掩码。同栏目文章签名高度一致。"""
    p = urlparse(url)
    parts = [seg for seg in p.path.split("/") if seg][:-1]      # 去掉文件名本身
    return "/".join(_NUM_SEG.sub("#", seg) for seg in parts)


def _consistency(urls: list[str]) -> float:
    """文章一致性 = 最主流结构签名的占比(0-1)。越高越像"同一个栏目的文章列表"。"""
    if not urls:
        return 0.0
    from collections import Counter
    c = Counter(_signature(u) for u in urls)
    return c.most_common(1)[0][1] / len(urls)


def validate_column(url: str, render_pref="auto") -> dict:
    """验证候选栏目:抓该页,看它是否列出一批"高度一致"的文章。

    有效标准:文章数≥column_min_articles 且 一致性≥column_consistency_min。
    "文章高度一致"即认定是一个真栏目(而非导航/杂链页)。返回校验明细。
    """
    fr = fetcher.fetch(url, render=render_pref)
    if not fr.ok:
        return {"url": url, "valid": False, "article_count": 0, "consistency": 0.0,
                "reason": "抓取失败"}
    arts = _article_links(fr.html, fr.final_url or url)
    cons = round(_consistency(arts), 2)
    valid = (len(arts) >= settings.column_min_articles
             and cons >= settings.column_consistency_min)
    return {"url": url, "valid": valid, "article_count": len(arts), "consistency": cons,
            "reason": "" if valid else f"文章{len(arts)}篇/一致性{cons},未达标",
            "sample": arts[:5]}


# ---------------- 栏目持久化(记录后不必每次重算) ----------------

def _children_of(db, parent_id: int) -> list:
    from app.models import Source
    return [s for s in db.query(Source).filter_by(discovered_from="column_auto").all()
            if (s.adapter_config or {}).get("parent_site_id") == parent_id]


def discover_and_persist(db, source, extra_terms: list[str] | None = None) -> tuple[list, bool]:
    """发现并持久化站点栏目为子源。TTL 内直接复用已记录的栏目、不重算;过期或首次才重新识别验证。

    返回 (子栏目源列表, 是否本次重新识别)。子源标 parent_site_id,不参与独立调度(经父源采集)。
    """
    from datetime import datetime

    from app.models import Source
    cfg = dict(source.adapter_config or {})
    ts = cfg.get("columns_discovered_at")
    existing = _children_of(db, source.id)
    fresh = False
    if ts:
        try:
            fresh = (datetime.utcnow() - datetime.fromisoformat(ts)).days < settings.auto_column_refresh_days
        except ValueError:
            fresh = False
    if fresh and existing:
        return existing, False   # 记录仍新鲜 → 直接复用,不重算

    render_pref = cfg.get("render", "auto")
    candidates = discover_columns(source, extra_terms)
    result = list(existing)
    known_ids = {c.identity_key for c in existing}
    for c in candidates:
        v = validate_column(c["url"], render_pref)   # 文章高度一致才确认为栏目
        if not v["valid"]:
            continue
        ik = url_tools.normalize_url(c["url"])
        if ik in known_ids:
            continue
        child = db.query(Source).filter_by(identity_key=ik).one_or_none()
        if child is None:
            child = Source(
                name=f"{source.name}·{c['anchor']}", entry_url=c["url"], kind="page",
                adapter="generic_list",
                adapter_config={"parent_site_id": source.id, "render": render_pref},
                credibility=source.credibility, tier=source.tier, lifecycle="active",
                serves_needs=list(source.serves_needs or []),
                identity_key=ik, site_key=source.site_key, discovered_from="column_auto",
                note=(f"自动栏目(相关度{c['score']}/文章{v['article_count']}"
                      f"/一致性{v['consistency']})"))
            db.add(child); db.flush()
            known_ids.add(ik)
            result.append(child)
    cfg["columns_discovered_at"] = datetime.utcnow().isoformat()
    source.adapter_config = cfg
    db.flush()
    return result, True
