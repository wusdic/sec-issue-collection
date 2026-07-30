"""采集适配器框架:BaseAdapter + 通用适配器(generic_rss/generic_list)+ 查询型适配器。

设计(详细设计 §6 / §8.3):
- 具体适配器只实现 discover()(发现文章 URL 列表)或 search()(查询型);
- 渲染、存档、去重、限速由流水线统一处理;
- 新源零适配器:generic_rss 自动探测 RSS;generic_list 用 LLM 生成解析模板;
- 未实现的站点专用适配器自动回退 generic 链(先 RSS 后 list),保证种子源全部可跑。
"""
import re
import time
from dataclasses import dataclass
from urllib.parse import quote, urljoin, urlparse

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
            for e in parsed.entries[:settings.rss_max_items] if e.get("link")
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
        for node in soup.select(template.get("item_selector", "a"))[:settings.list_max_items]:
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
        # 列表页默认 render="auto":httpx 抓到的正文过薄(政务站 JS 壳)时自动浏览器渲染
        fr = fetcher.fetch(url, render=self.config.get("render", "auto"))
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

    def _augment(self, query: str) -> str:
        """adapter_config.site 存在时 → 站内检索:借搜索引擎抓某站(直连抓不到的兜底)。"""
        site = self.config.get("site")
        return f"{query} site:{site}" if site else query

    def build_url(self, query: str, page: int, time_filter: str | None) -> str:
        return self.base_tpl.format(q=quote(self._augment(query)), page=page)

    # 搜索引擎自身的域名(结果里指向自己的链接除跳转链外都是导航,不算结果)
    own_hosts: tuple[str, ...] = ()
    # 反爬/验证页特征:命中即判定"这一页不是结果页",避免把验证页当成"0 条结果"
    _BLOCK_MARKERS = ("百度安全验证", "网络不给力", "请输入验证码", "安全验证", "滑动验证",
                      "unusual traffic", "verify you are human", "captcha", "拒绝访问",
                      "访问被拒绝", "请开启JavaScript", "为了您的账号安全",
                      # 搜狗:"此验证码用于确认这些请求是您的正常行为而不是自动程序发出的"
                      "VerifyCode", "确认这些请求是您的正常行为", "不是自动程序发出")

    def looks_blocked(self, html: str | None) -> bool:
        head = (html or "")[:8000]
        return any(m in head for m in self._BLOCK_MARKERS)

    def parse(self, html: str) -> list[DiscoveredItem]:
        """解析结果页。专用选择器优先,取不到就退回"通用抽链"。

        搜索引擎改版很勤(百度结果块已多次换类名、部分结果还由 JS 注入),写死选择器一旦
        失配就是静默返回 0 条——检索型源看起来在跑,实际什么都没采到。找源/检索只需要拿到
        结果链接,不依赖结果块结构,所以退回通用抽链完全够用,且改版免疫。
        """
        soup = BeautifulSoup(html or "", "lxml")
        out, seen = [], set()
        for a in soup.select(self.result_selector):
            href, title = a.get("href"), a.get_text(" ", strip=True)
            if href and title and href.startswith("http") and href not in seen:
                seen.add(href)
                out.append(DiscoveredItem(url=href, title=title[:200]))
        if out:
            return out
        return self._parse_generic(soup)

    # 页脚模板链:ICP 备案、增值电信业务许可证、公安网备、违法信息举报。
    # 搜索引擎自己的页面底部就有这一串,通用抽链一旦把它们当结果,就会出现
    # "300 页全部成功、900 条结果"而其实一条真结果都没有——实测必应正是如此。
    # 逐个域名去列举是打地鼠:上一版列了 beian.gov.cn,必应换成 beian.mps.gov.cn
    # (公安网备的新地址)就又漏了 300 条。改成认前缀:备案类站点一律叫 beian.*。
    _FOOTER_HOSTS = ("dxzhgl.miit.gov.cn", "jubao.cac.gov.cn", "12377.cn", "12321.cn",
                     "12318.gov.cn", "miitbeian.gov.cn", "gov.cn/icp")
    _FOOTER_PREFIX = ("beian.", "jubao.", "icp.")

    @classmethod
    def _is_footer_link(cls, href: str) -> bool:
        host = urlparse(href).netloc.lower().removeprefix("www.")
        if host.startswith(cls._FOOTER_PREFIX):
            return True
        return any(host == h or host.endswith("." + h) for h in cls._FOOTER_HOSTS)

    def _parse_generic(self, soup) -> list[DiscoveredItem]:
        """通用抽链兜底:整页取外链 + 本引擎的跳转链,滤掉导航/页脚/短文本。"""
        out, seen = [], set()
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if not href.startswith("http") or href in seen:
                continue
            if self._is_footer_link(href):
                continue                       # 备案/许可证:页脚模板,不是结果
            host = urlparse(href).netloc.lower()
            is_own = any(host == h or host.endswith("." + h) for h in self.own_hosts)
            if is_own and not url_tools.is_search_redirect(href):
                continue                       # 指向引擎自己的导航链,不是结果
            title = a.get_text(" ", strip=True)
            if len(title) < 6:
                continue                       # "下一页""登录"这类导航
            seen.add(href)
            out.append(DiscoveredItem(url=href, title=title[:200]))
            if len(out) >= settings.list_max_items:
                break
        return out

    def search_page(self, query: str, page: int, time_filter: str | None = None) -> list[DiscoveredItem] | None:
        """抓取单页结果。返回该页 items;None 表示抓取失败/无更多页(供流水线逐页早停)。

        adapter_config.render 可设 "auto"/True:搜索引擎对纯 httpx 常直接 403/返回验证页,
        开了浏览器渲染就能救回来(未开渲染时 auto 自动降级为 httpx,零成本)。
        """
        fr = fetcher.fetch(self.build_url(query, page, time_filter),
                           render=self.config.get("render", False))
        if not fr.ok:
            return None
        if self.looks_blocked(fr.html):
            # 验证页/反爬拦截:必须当成"抓取失败",否则通用兜底会把页面上的导航链接
            # 当成搜索结果吐出来——实测搜狗被拦一次就产生 139 条全是 sogou.com 的假结果
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
    own_hosts = ("baidu.com",)


class BingSearchAdapter(SearchEngineAdapter):
    name = "bing_search"
    base_tpl = "https://cn.bing.com/search?q={q}&first={page}1"
    result_selector = "li.b_algo h2 a"
    own_hosts = ("bing.com", "microsoft.com", "msn.com")


class BingRSSAdapter(SearchEngineAdapter):
    """必应的 RSS 结果口。

    必应网页版的结果块由 JS 注入,不开浏览器渲染时抓回来的 HTML 里根本没有结果链接
    ——实测 300 页"全部成功、900 条结果",样本却全是必应自己的页脚备案链。
    RSS 口是纯 XML、不依赖 JS、也不随网页版改版失配,作为找源主力比 HTML 版可靠得多。
    """
    name = "bing_rss"
    # cn.bing.com 不认 format=rss,照样返回 HTML 搜索页(实测样本就是必应网页版的页脚),
    # RSS 口在全局站点上。所以这里必须用 www.bing.com。
    base_tpl = "https://www.bing.com/search?q={q}&format=rss&first={page}1"
    own_hosts = ("bing.com", "microsoft.com", "msn.com")

    def search_page(self, query: str, page: int, time_filter: str | None = None):
        """拿回来的必须真是 feed。

        返回 HTML 时旧实现只会解析出 0 条,被记成"这一页成功但没结果"——于是 151 页
        全部"成功"、实际一条真结果都没有,连熔断都不会触发。这种情况要当抓取失败上报。
        """
        fr = fetcher.fetch(self.build_url(query, page, time_filter),
                           render=self.config.get("render", False))
        if not fr.ok:
            return None
        head = (fr.html or "")[:2000].lower()
        if "<rss" not in head and "<feed" not in head:
            return None          # 不是 feed:当抓取失败,别伪装成"没结果"
        return self.parse(fr.html) or []

    def parse(self, html: str) -> list[DiscoveredItem]:
        soup = BeautifulSoup(html or "", "xml")
        out, seen = [], set()
        for it in soup.find_all("item"):
            link = it.find("link")
            href = (link.get_text(strip=True) if link else "") or ""
            if not href.startswith("http") or href in seen or self._is_footer_link(href):
                continue
            seen.add(href)
            title = it.find("title")
            out.append(DiscoveredItem(url=href,
                                      title=(title.get_text(" ", strip=True) if title else "")[:200]))
        return out


class DuckDuckGoHTMLAdapter(SearchEngineAdapter):
    """DuckDuckGo 的 HTML 版结果页。

    百度/搜狗对这台机器已经完全拒绝(各 0 页 8 次失败),必应网页版只吐页脚。
    这个口不依赖 JS、结果链接是直链,是少数还能用纯 httpx 打通的通用引擎,
    中文查询也支持。留在候选池里由引擎自检决定要不要用。
    """
    name = "ddg_html"
    base_tpl = "https://html.duckduckgo.com/html/?q={q}&s={page}0"
    result_selector = "a.result__a"
    own_hosts = ("duckduckgo.com",)


class So360SearchAdapter(SearchEngineAdapter):
    """360 搜索。国内引擎里反爬相对宽松的一个,同样进候选池等自检裁决。"""
    name = "so360_search"
    base_tpl = "https://www.so.com/s?q={q}&pn={page}"
    result_selector = "h3.res-title a"
    own_hosts = ("so.com", "360.cn", "360.com")


class SogouWechatAdapter(SearchEngineAdapter):
    name = "sogou_wechat"
    base_tpl = "https://weixin.sogou.com/weixin?type=2&query={q}&page={page}"
    result_selector = "ul.news-list h3 a"
    own_hosts = ("sogou.com",)

    def _augment(self, query: str) -> str:
        """配了 account 就把检索限定到该公众号(此前 account 完全没被使用,
        导致"某公众号源"实际只是又跑了一遍全局关键词搜索)。"""
        acct = (self.config.get("account") or "").strip()
        if acct:
            return f"{acct} {query}" if query else acct
        return super()._augment(query)

    # 号名所在的链接:搜狗改过版,老版是 a.account,新版只给 id="..._account_0"。
    # 只认 a.account 时 732 条结果全都拿不到号名,只能逐条去还原跳转链——配额一超就整批白丢。
    _ACCT_SEL = "a.account, a[id*='_account_'], div.s-p a[href*='profile'], div.s-p > a"
    # 号名不会是这些控件文案;也不会是纯日期/数字
    _ACCT_BAD = ("更多", "展开", "收起", "查看", "全部", "微信", "订阅", "关注", "http",
                 "相关", "阅读", "分享")
    _DATE_LIKE = re.compile(r"^[\d\s\-/:年月日前天小时分钟秒]+$")

    @classmethod
    def _acct_ok(cls, t: str, title: str) -> bool:
        if not t or not (1 < len(t) <= 32) or " " in t.strip(" "):
            return False
        if t.startswith(cls._ACCT_BAD) or cls._DATE_LIKE.match(t):
            return False
        return t not in title            # 标题的片段不是号名

    @classmethod
    def _acct_text(cls, li, title: str = "") -> str | None:
        """从一条结果里取公众号名。

        先按选择器找;搜狗改版频繁,选择器全落空时退回"扫这条结果里所有短文本"——
        实测自检里搜狗把网警通报一条条搜回来了(标题完全对得上),却因为号名取不到,
        整批只能去还原跳转链、最后全折在 sogou.com 上。号名拿不到就等于这个引擎白跑,
        所以这里宁可用一个粗但兜得住的办法。
        """
        for a in li.select(cls._ACCT_SEL):
            t = a.get_text(" ", strip=True)
            if cls._acct_ok(t, title):
                return t
        # 兜底:结果块里除标题以外的短文本,第一个像号名的就是它
        for node in li.find_all(["a", "span", "em", "div"]):
            if node.find(["a", "span", "em", "div"]):
                continue                 # 只看叶子节点,避免整块文本
            t = node.get_text(" ", strip=True)
            if cls._acct_ok(t, title):
                return t
        return None

    def parse(self, html: str) -> list[DiscoveredItem]:
        soup = BeautifulSoup(html, "lxml")
        want = (self.config.get("account") or "").strip()
        out = []
        for li in soup.select("ul.news-list li"):
            a = li.select_one("h3 a")
            if a and a.get("href"):
                title = a.get_text(" ", strip=True)
                acct_name = self._acct_text(li, title)
                # 限定了公众号时,只保留确实来自该号的结果(搜狗会返回其他号的相近内容)
                if want and acct_name and acct_name != want:
                    continue
                out.append(DiscoveredItem(
                    url=urljoin("https://weixin.sogou.com/", a["href"]),
                    title=title[:200],
                    wechat_account=acct_name or (want or None),
                ))
        return out


class WeiboSearchAdapter(SearchEngineAdapter):
    name = "weibo_search"
    base_tpl = "https://s.weibo.com/weibo?q={q}&page={page}"
    result_selector = "div.card-wrap p.txt a"
    own_hosts = ("weibo.com", "weibo.cn", "sina.com.cn")


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
        BaiduSearchAdapter, BingSearchAdapter, BingRSSAdapter, SogouWechatAdapter,
        WeiboSearchAdapter, DuckDuckGoHTMLAdapter, So360SearchAdapter,
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
