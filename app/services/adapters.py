"""采集适配器框架:BaseAdapter + 通用适配器(generic_rss/generic_list)+ 查询型适配器。

设计(详细设计 §6 / §8.3):
- 具体适配器只实现 discover()(发现文章 URL 列表)或 search()(查询型);
- 渲染、存档、去重、限速由流水线统一处理;
- 新源零适配器:generic_rss 自动探测 RSS;generic_list 用 LLM 生成解析模板;
- 未实现的站点专用适配器自动回退 generic 链(先 RSS 后 list),保证种子源全部可跑。
"""
import time
from dataclasses import dataclass
from urllib.parse import quote, urljoin

try:
    import feedparser
except ImportError:  # feedparser 依赖 sgmllib3k 编译失败时降级:RSS 探测不可用,不影响其他适配器
    feedparser = None
from bs4 import BeautifulSoup

from app.config import settings
from app.services import fetcher, url_tools
from app.services.llm import get_llm
from app.services.prompts import list_template_prompts


@dataclass
class DiscoveredItem:
    url: str
    title: str | None = None
    published: str | None = None
    publisher: str | None = None
    wechat_account: str | None = None  # 公众号来源(源发现 D3)


class BaseAdapter:
    name = "base"
    kind = "page"  # page | query

    def __init__(self, source):
        self.source = source
        self.config = source.adapter_config or {}

    def discover(self) -> list[DiscoveredItem]:
        raise NotImplementedError

    def discover_page(self, page: int) -> list[DiscoveredItem] | None:
        """页面型逐页发现:page 从 0 起。返回该页 items;None 表示没有更多页。

        默认实现:只有第 0 页(即整页 discover() 的结果),之后没有分页。
        支持自动翻页的适配器(如 GenericListAdapter)覆写此方法自动跟随「下一页」。
        流水线用它实现增量翻页早停,不必子类各自处理早停逻辑。
        """
        return self.discover() if page == 0 else None

    def search(self, query: str, time_filter: str | None = None, max_pages: int = 1) -> tuple[list[DiscoveredItem], bool]:
        """查询型:返回 (结果, 是否截断)。C2:截断必须上报。"""
        raise NotImplementedError


# ---------------- 自动翻页探测 ----------------

# 「下一页」链接的常见文本/标记(中英),按出现频率排。自动识别,无需人工配模板。
_NEXT_TEXTS = ("下一页", "下页", "后一页", "下一頁", "next", "next page", "older", "older posts",
               "较早", "更早", "»", "›", ">", ">>")
_PREV_TEXTS = ("上一页", "上页", "前一页", "previous", "prev", "newer", "«", "‹", "<")


def find_next_page_url(soup, base_url: str) -> str | None:
    """从列表页 HTML 自动探测「下一页」的绝对 URL;探测不到返回 None。

    优先级:① <link rel="next"> / <a rel="next">(最可靠);② 锚文本/title/aria-label
    命中「下一页/next/»」等词且不是「上一页」;③ 分页控件里 class 含 next 的链接。
    纯自动,不依赖任何人工配置的模板。
    """
    # ① rel=next 语义标记
    for tag in soup.find_all(["link", "a"], rel=True):
        rels = tag.get("rel") or []
        rel = " ".join(rels).lower() if isinstance(rels, list) else str(rels).lower()
        if "next" in rel and tag.get("href"):
            return urljoin(base_url, tag["href"])

    def _looks_next(a) -> bool:
        blob = " ".join(filter(None, [
            a.get_text(" ", strip=True), a.get("title", ""), a.get("aria-label", ""),
            " ".join(a.get("class", []) if isinstance(a.get("class"), list) else []),
            a.get("rel") and " ".join(a.get("rel")) or "",
        ])).lower()
        if not blob:
            return False
        if any(p in blob for p in _PREV_TEXTS) and not any(
                n in blob for n in ("下一页", "下页", "next", "»", "›")):
            return False  # 明确是「上一页」
        return any(n in blob for n in _NEXT_TEXTS)

    # ② 锚文本/属性命中「下一页」
    for a in soup.find_all("a", href=True):
        if _looks_next(a):
            href = a["href"].strip()
            if href and not href.startswith(("#", "javascript:")):
                return urljoin(base_url, href)
    return None


# ---------------- 通用适配器 ----------------

class GenericRSSAdapter(BaseAdapter):
    name = "generic_rss"

    RSS_CANDIDATES = ["/feed", "/rss.xml", "/rss", "/atom.xml", "/index.xml"]

    def _find_feed(self) -> str | None:
        if self.config.get("feed_url"):
            return self.config["feed_url"]
        base = self.source.entry_url
        fr = fetcher.fetch(base)
        if fr.ok:
            soup = BeautifulSoup(fr.html, "lxml")
            link = soup.find("link", rel="alternate", type=lambda t: t and "rss" in t or t and "atom" in t)
            if link and link.get("href"):
                return urljoin(fr.final_url, link["href"])
        for suffix in self.RSS_CANDIDATES:
            probe = base.rstrip("/") + suffix
            fr2 = fetcher.fetch(probe)
            if fr2.ok and ("<rss" in fr2.html[:2000] or "<feed" in fr2.html[:2000]):
                return probe
        return None

    def discover(self) -> list[DiscoveredItem]:
        if feedparser is None:
            return []
        feed_url = self._find_feed()
        if not feed_url:
            return []
        parsed = feedparser.parse(feed_url)
        return [
            DiscoveredItem(url=e.get("link"), title=e.get("title"), published=e.get("published"))
            for e in parsed.entries[:50] if e.get("link")
        ]


class GenericListAdapter(BaseAdapter):
    """LLM 生成列表页解析模板(8.3②);模板缓存在 source.adapter_config。

    自动翻页:discover_page 逐页抓取,页间通过 find_next_page_url 自动跟随「下一页」链接,
    零人工配置。可选 page_url_template(含 {page})作为翻页 URL 规律的兜底(极少数无「下一页」
    锚点的站点),不填则纯靠自动探测。
    """
    name = "generic_list"

    def _template(self, html: str) -> dict:
        template = self.config.get("list_template")
        if not template:
            template = get_llm().complete_json(*list_template_prompts(html))
            self.config["list_template"] = template
            self.source.adapter_config = dict(self.config)
        return template

    def _extract(self, fr) -> tuple[list[DiscoveredItem], str | None]:
        """解析一页:返回 (items, 下一页URL)。下一页 URL 自动探测。"""
        template = self._template(fr.html)
        soup = BeautifulSoup(fr.html, "lxml")
        items = []
        for node in soup.select(template.get("item_selector", "a"))[:80]:
            href = node.get("href") if node.name == "a" else (node.find("a") or {}).get("href")
            if not href:
                continue
            title = node.get_text(" ", strip=True)[:200]
            if not title or len(title) < 6:
                continue
            items.append(DiscoveredItem(url=urljoin(fr.final_url, href), title=title))
        return items, find_next_page_url(soup, fr.final_url)

    def _page_url(self, page: int) -> str | None:
        """第 page 页(0起)的 URL。第0页=入口页;之后优先用自动探测到的「下一页」,
        其次用可选 page_url_template 兜底。"""
        if page == 0:
            return self.source.entry_url
        nxt = getattr(self, "_next_url", None)
        if nxt:
            return nxt
        tpl = self.config.get("page_url_template")
        return tpl.format(page=page) if tpl else None

    def discover(self) -> list[DiscoveredItem]:
        items = self.discover_page(0)
        return items or []

    def discover_page(self, page: int) -> list[DiscoveredItem] | None:
        url = self._page_url(page)
        if not url:
            return None  # 无更多页(自动探测不到下一页且无模板)
        fr = fetcher.fetch(url)
        if not fr.ok:
            return None
        items, next_url = self._extract(fr)
        self._next_url = next_url  # 供下一次 discover_page 自动跟随
        return items


# ---------------- 查询型适配器(搜索引擎/平台) ----------------

class SearchEngineAdapter(BaseAdapter):
    kind = "query"
    base_tpl = ""            # 子类给出查询 URL 模板 {q}=词 {page}=页码(0起)
    result_selector = "a"

    def build_url(self, query: str, page: int, time_filter: str | None) -> str:
        return self.base_tpl.format(q=quote(query), page=page)

    def parse(self, html: str) -> list[DiscoveredItem]:
        soup = BeautifulSoup(html, "lxml")
        out = []
        for a in soup.select(self.result_selector):
            href = a.get("href")
            title = a.get_text(" ", strip=True)
            if href and title and href.startswith("http"):
                out.append(DiscoveredItem(url=href, title=title[:200]))
        return out

    def search_page(self, query: str, page: int, time_filter: str | None = None) -> list[DiscoveredItem] | None:
        """抓取单页结果。返回该页 items;None 表示抓取失败/无更多页(供流水线逐页早停)。"""
        fr = fetcher.fetch(self.build_url(query, page, time_filter))
        if not fr.ok:
            return None
        return self.parse(fr.html) or []

    def search(self, query: str, time_filter: str | None = None, max_pages: int = 1):
        """一次性抓多页(兼容旧调用)。逐页早停由流水线用 search_page 实现。"""
        items: list[DiscoveredItem] = []
        truncated = False
        for page in range(max_pages):
            page_items = self.search_page(query, page, time_filter)
            if not page_items:
                break
            items.extend(page_items)
            if page == max_pages - 1 and len(page_items) >= 8:
                truncated = True  # 最后一页仍然饱和 → 截断上报
            time.sleep(settings.crawl_delay_seconds)
        return items, truncated


class BaiduSearchAdapter(SearchEngineAdapter):
    name = "baidu_search"
    base_tpl = "https://www.baidu.com/s?wd={q}&pn={page}0"
    result_selector = "h3 a"


class BingSearchAdapter(SearchEngineAdapter):
    name = "bing_search"
    base_tpl = "https://cn.bing.com/search?q={q}&first={page}1"
    result_selector = "li.b_algo h2 a"


class SogouWechatAdapter(SearchEngineAdapter):
    name = "sogou_wechat"
    base_tpl = "https://weixin.sogou.com/weixin?type=2&query={q}&page={page}"
    result_selector = "ul.news-list h3 a"

    def parse(self, html: str) -> list[DiscoveredItem]:
        soup = BeautifulSoup(html, "lxml")
        out = []
        for li in soup.select("ul.news-list li"):
            a = li.select_one("h3 a")
            account = li.select_one("a.account")
            if a and a.get("href"):
                out.append(DiscoveredItem(
                    url=urljoin("https://weixin.sogou.com/", a["href"]),
                    title=a.get_text(" ", strip=True)[:200],
                    wechat_account=account.get_text(strip=True) if account else None,
                ))
        return out


class WeiboSearchAdapter(SearchEngineAdapter):
    name = "weibo_search"
    base_tpl = "https://s.weibo.com/weibo?q={q}&page={page}"
    result_selector = "div.card-wrap p.txt a"


class RansomwareLiveAdapter(BaseAdapter):
    """勒索组织列名监测:公开 API,过滤中国受害者;仅记录列名事实。"""
    name = "ransomware_live"

    API = "https://api.ransomware.live/v2/recentvictims"
    CN_MARKERS = ("china", ".cn", "chinese")

    def discover(self) -> list[DiscoveredItem]:
        import httpx
        try:
            resp = httpx.get(self.API, timeout=settings.fetch_timeout,
                             headers={"User-Agent": settings.fetch_user_agent})
            resp.raise_for_status()
            data = resp.json()
        except Exception:  # noqa: BLE001
            return []
        items = []
        for v in data if isinstance(data, list) else []:
            blob = str(v).lower()
            if any(m in blob for m in self.CN_MARKERS):
                victim = v.get("victim") or v.get("post_title") or "unknown"
                items.append(DiscoveredItem(
                    url=v.get("url") or f"https://www.ransomware.live/#{victim}",
                    title=f"[leak-site] {v.get('group_name','?')} 列名 {victim}",
                    published=v.get("discovered"),
                ))
        return items


_REGISTRY: dict[str, type[BaseAdapter]] = {
    a.name: a for a in [
        GenericRSSAdapter, GenericListAdapter,
        BaiduSearchAdapter, BingSearchAdapter, SogouWechatAdapter, WeiboSearchAdapter,
        RansomwareLiveAdapter,
    ]
}


def get_adapter(source) -> BaseAdapter:
    """站点专用适配器未实现时,回退 generic 链(8.3):query→百度模板检索,page→RSS→list。"""
    cls = _REGISTRY.get(source.adapter)
    if cls:
        return cls(source)
    if source.kind == "query":
        return BaiduSearchAdapter(source)
    rss = GenericRSSAdapter(source)
    return rss if rss._find_feed() else GenericListAdapter(source)
