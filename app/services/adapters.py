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

    def search(self, query: str, time_filter: str | None = None, max_pages: int = 1) -> tuple[list[DiscoveredItem], bool]:
        """查询型:返回 (结果, 是否截断)。C2:截断必须上报。"""
        raise NotImplementedError


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
    """LLM 生成列表页解析模板(8.3②);模板缓存在 source.adapter_config。"""
    name = "generic_list"

    def discover(self) -> list[DiscoveredItem]:
        fr = fetcher.fetch(self.source.entry_url)
        if not fr.ok:
            return []
        template = self.config.get("list_template")
        if not template:
            template = get_llm().complete_json(*list_template_prompts(fr.html))
            self.config["list_template"] = template
            self.source.adapter_config = dict(self.config)
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
