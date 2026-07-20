"""统一抓取器:httpx 为主,Playwright 可选(动态页/截图);C3 跳转还原内置。"""
from dataclasses import dataclass, field

import httpx

from app.config import settings
from app.services import url_tools


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


def fetch(url: str, referer: str | None = None, timeout: float | None = None) -> FetchResult:
    headers = {"User-Agent": settings.fetch_user_agent}
    if referer:
        headers["Referer"] = referer
    try:
        with httpx.Client(follow_redirects=True, timeout=timeout or settings.fetch_timeout, headers=headers) as client:
            resp = client.get(url)
            final_url = str(resp.url)
            html = resp.text if "text" in resp.headers.get("content-type", "text/html") or True else None
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
