"""统一抓取器:httpx 为主,Playwright 可选(动态页渲染/截图);C3 跳转还原内置。

render 语义(应对政务站等 JS 动态页):
- False(默认):仅 httpx,零依赖零开销,行为与历史一致;
- "auto":httpx 先抓,若失败或正文过薄(疑似 JS 壳)且已开启浏览器渲染,则用 Playwright 重抓;
- True:直接用 Playwright 渲染(渲染不可用/失败再回退 httpx)。
浏览器渲染需在设置页开启「启用浏览器渲染/截图」且装了 Playwright,否则 auto/True 自动降级为 httpx。
"""
import re
from dataclasses import dataclass, field

import httpx

from app.config import settings
from app.services import url_tools

_TAG_RE = re.compile(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>")
_ANYTAG_RE = re.compile(r"(?s)<[^>]+>")


@dataclass
class FetchResult:
    url: str
    final_url: str
    status: int | None
    html: str | None
    error: str | None = None
    headers: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.error is None and self.status is not None and 200 <= self.status < 300 and bool(self.html)

    @property
    def rendered(self) -> bool:
        return self.headers.get("x-rendered") == "playwright"


def _visible_len(html: str | None) -> int:
    """去脚本/样式/标签后的可见文本长度,用于判断是否 JS 壳。"""
    if not html:
        return 0
    text = _ANYTAG_RE.sub(" ", _TAG_RE.sub(" ", html))
    return len(re.sub(r"\s+", " ", text).strip())


def _looks_thin(html: str | None) -> bool:
    """正文过薄(可见文本 < 200 字)→ 疑似需 JS 渲染的空壳页。"""
    return _visible_len(html) < 200


def _httpx_fetch(url: str, referer: str | None, timeout: float | None) -> FetchResult:
    headers = {"User-Agent": settings.fetch_user_agent}
    if referer:
        headers["Referer"] = referer
    try:
        with httpx.Client(follow_redirects=True, timeout=timeout or settings.fetch_timeout, headers=headers) as client:
            resp = client.get(url)
            final_url = str(resp.url)
            ctype = resp.headers.get("content-type", "text/html")
            # 只对文本/HTML/XML 类响应解码为文本;二进制(PDF/图片等)不当 html 处理
            html = resp.text if any(t in ctype for t in ("text", "html", "xml", "json")) else None
            # C8: 搜狗微信临时链 → 提取永久链再抓一次
            if "weixin.sogou.com" in final_url or ("sogou" in final_url and "mp.weixin" not in final_url):
                perm = url_tools.extract_wechat_permalink(html or "")
                if perm:
                    resp = client.get(perm)
                    final_url = str(resp.url)
                    html = resp.text
            return FetchResult(url=url, final_url=final_url, status=resp.status_code, html=html,
                              headers=dict(resp.headers))
    except Exception as e:  # noqa: BLE001 网络异常统一登记
        return FetchResult(url=url, final_url=url, status=None, html=None, error=str(e))


import threading
from contextlib import contextmanager

_render_local = threading.local()  # 当前线程的渲染会话(采集批次内复用浏览器)


def _playwright_available() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
        return True
    except ImportError:
        return False


def _render_one(browser, url: str, referer: str | None, timeout: float | None) -> FetchResult:
    """用已启动的浏览器渲染一页:每页独立 context/标签(隔离 cookie),抓完只关标签不关浏览器。"""
    to_ms = int((timeout or settings.fetch_timeout) * 1000)
    ctx = browser.new_context(user_agent=settings.fetch_user_agent)
    try:
        page = ctx.new_page()
        if referer:
            page.set_extra_http_headers({"Referer": referer})
        resp = page.goto(url, timeout=to_ms, wait_until="domcontentloaded")
        try:  # 尽量等到网络空闲拿到动态内容,超时不致命
            page.wait_for_load_state("networkidle", timeout=to_ms)
        except Exception:  # noqa: BLE001
            pass
        return FetchResult(url=url, final_url=page.url,
                           status=(resp.status if resp else 200),
                           html=page.content(), headers={"x-rendered": "playwright"})
    finally:
        ctx.close()  # 只关标签/上下文,浏览器留给后续页面复用


class _RenderSession:
    """采集批次内复用的浏览器实例:懒启动,一批只启一个浏览器,反复开关标签抓多页,摊薄启动开销。"""

    def __init__(self):
        self._pw = None
        self._browser = None
        self._count = 0

    def _browser_ok(self):
        from playwright.sync_api import sync_playwright
        if self._browser is None:
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch()
            self._count = 0
        return self._browser

    def render(self, url: str, referer: str | None, timeout: float | None) -> FetchResult | None:
        try:
            browser = self._browser_ok()
            res = _render_one(browser, url, referer, timeout)
        except Exception:  # noqa: BLE001 单页渲染失败不拖垮批次;浏览器可能已坏 → 回收待重启
            self.close()
            return None
        self._count += 1
        recycle = int(getattr(settings, "render_recycle_after", 0) or 0)
        if recycle and self._count >= recycle:
            self.close()  # 内存保护:渲染够多页后回收,下次 render 自动重启
        return res

    def close(self):
        try:
            if self._browser is not None:
                self._browser.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:  # noqa: BLE001
            pass
        self._browser = None
        self._pw = None


@contextmanager
def render_session():
    """在一次采集批次外层包一层:内部所有 render 复用同一浏览器,批次结束/异常统一关闭。

    可安全嵌套(内层复用外层会话,不重复启停)。未开启渲染/无页面需要渲染时零成本
    (只建一个轻量对象,浏览器懒启动,从不真正拉起进程)。
    """
    existing = getattr(_render_local, "session", None)
    if existing is not None:
        yield existing  # 复用外层会话,内层不新建不关闭
        return
    sess = _RenderSession()
    _render_local.session = sess
    try:
        yield sess
    finally:
        _render_local.session = None
        sess.close()


def _render_fetch(url: str, referer: str | None, timeout: float | None) -> FetchResult | None:
    """用 Playwright 渲染取 HTML。未开启/未安装/失败均返回 None(由调用方回退 httpx)。

    批次内有活跃 render_session 时复用其浏览器实例;否则一次性启停(单页试抓等场景)。
    """
    if not settings.playwright_enabled or not _playwright_available():
        return None
    sess = getattr(_render_local, "session", None)
    if sess is not None:
        return sess.render(url, referer, timeout)  # 复用批次浏览器,快
    try:  # 无会话:一次性启停(单页场景,不值得常驻)
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                return _render_one(browser, url, referer, timeout)
            finally:
                browser.close()
    except Exception:  # noqa: BLE001
        return None


def fetch(url: str, referer: str | None = None, timeout: float | None = None,
          render: bool | str = False) -> FetchResult:
    """抓取一个 URL。render 见模块 docstring:False=仅httpx、"auto"=薄则渲染、True=直接渲染。"""
    if render is True:
        r = _render_fetch(url, referer, timeout)
        if r is not None and r.ok:
            return r
        return _httpx_fetch(url, referer, timeout)

    result = _httpx_fetch(url, referer, timeout)
    if render == "auto" and settings.playwright_enabled and (not result.ok or _looks_thin(result.html)):
        r = _render_fetch(url, referer, timeout)
        # 渲染结果更充实才采用,否则保留原 httpx 结果(避免渲染反而更差)
        if r is not None and r.ok and _visible_len(r.html) > _visible_len(result.html):
            return r
    return result


def fetch_binary(url: str, referer: str | None = None, byte_cap: int | None = None) -> tuple[bytes | None, str | None]:
    headers = {"User-Agent": settings.fetch_user_agent}
    if referer:
        headers["Referer"] = referer
    cap = byte_cap or settings.archive_asset_byte_cap
    try:
        with httpx.Client(follow_redirects=True, timeout=settings.fetch_timeout, headers=headers) as client:
            with client.stream("GET", url) as resp:
                if resp.status_code >= 400:
                    return None, f"http {resp.status_code}"
                buf = b""
                for chunk in resp.iter_bytes():
                    buf += chunk
                    if len(buf) > cap:
                        return None, "byte cap exceeded"
                return buf, None
    except Exception as e:  # noqa: BLE001
        return None, str(e)


def screenshot_pages(url: str) -> list[bytes]:
    """L-C 分段截图:Playwright 可用时整页+分段;不可用返回空。"""
    if not settings.playwright_enabled:
        return []
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return []
    shots: list[bytes] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 1000})
        page.goto(url, timeout=int(settings.fetch_timeout * 1000))
        total = page.evaluate("document.body.scrollHeight") or 1000
        shots.append(page.screenshot(full_page=True))
        step = int(1000 * 0.9)  # 相邻段重叠 10%
        y = 0
        while y < total and len(shots) < 40:
            page.evaluate(f"window.scrollTo(0, {y})")
            shots.append(page.screenshot())
            y += step
        browser.close()
    return shots
