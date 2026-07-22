"""浏览器渲染 auto 回退 + 站内检索(site:)兜底。"""
import pytest

from app.api.routes import SourceIn, create_source, source_to_search_retry
from app.config import settings
from app.models import AppUser, Source
from app.services import fetcher
from app.services.adapters import BaiduSearchAdapter


@pytest.fixture()
def admin(db):
    u = db.query(AppUser).filter_by(role="admin").first()
    if not u:
        from app.auth import hash_password
        u = AppUser(username="admin_rr", display_name="admin_rr",
                    password_hash=hash_password("x"), role="admin")
        db.add(u); db.flush()
    return u


# ---------------- 渲染 ----------------

def test_thin_detection():
    assert fetcher._looks_thin("<html><body><div id='app'></div><script>x</script></body></html>")
    assert not fetcher._looks_thin("<p>" + "内容" * 200 + "</p>")


def test_render_false_never_renders(monkeypatch):
    called = {"render": 0}
    monkeypatch.setattr(fetcher, "_render_fetch", lambda *a: called.__setitem__("render", called["render"] + 1))
    monkeypatch.setattr(fetcher, "_httpx_fetch",
                        lambda *a: fetcher.FetchResult("u", "u", 200, "<html></html>"))
    fetcher.fetch("http://x", render=False)
    assert called["render"] == 0


def test_render_auto_triggers_on_thin(monkeypatch):
    monkeypatch.setattr(settings, "playwright_enabled", True)
    thin = fetcher.FetchResult("u", "u", 200, "<html><body><div id=app></div></body></html>")
    rich = fetcher.FetchResult("u", "u", 200, "<html><body>" + "真实正文" * 100 + "</body></html>",
                               headers={"x-rendered": "playwright"})
    monkeypatch.setattr(fetcher, "_httpx_fetch", lambda *a: thin)
    monkeypatch.setattr(fetcher, "_render_fetch", lambda *a: rich)
    out = fetcher.fetch("http://x", render="auto")
    assert out.rendered and "真实正文" in out.html


def test_render_auto_skips_when_httpx_rich(monkeypatch):
    monkeypatch.setattr(settings, "playwright_enabled", True)
    rich = fetcher.FetchResult("u", "u", 200, "<html><body>" + "已经很全" * 100 + "</body></html>")
    hits = {"render": 0}

    def _r(*a):
        hits["render"] += 1
        return None
    monkeypatch.setattr(fetcher, "_httpx_fetch", lambda *a: rich)
    monkeypatch.setattr(fetcher, "_render_fetch", _r)
    out = fetcher.fetch("http://x", render="auto")
    assert hits["render"] == 0 and out is rich


def test_render_auto_noop_when_playwright_off(monkeypatch):
    monkeypatch.setattr(settings, "playwright_enabled", False)
    thin = fetcher.FetchResult("u", "u", 200, "<html><body></body></html>")
    hits = {"render": 0}
    monkeypatch.setattr(fetcher, "_httpx_fetch", lambda *a: thin)
    monkeypatch.setattr(fetcher, "_render_fetch",
                        lambda *a: hits.__setitem__("render", hits["render"] + 1))
    out = fetcher.fetch("http://x", render="auto")
    assert hits["render"] == 0 and out is thin


# ---------------- 浏览器实例复用 ----------------

def _stub_session_browser(monkeypatch, launches, closes):
    """把浏览器启停替换成计数桩,不碰真实 Playwright。"""
    monkeypatch.setattr(settings, "playwright_enabled", True)
    monkeypatch.setattr(fetcher, "_playwright_available", lambda: True)

    class _FakeBrowser:
        def close(self):
            closes.append(1)

    def fake_ok(self):
        if self._browser is None:
            launches.append(1)
            self._browser = _FakeBrowser()
        return self._browser

    monkeypatch.setattr(fetcher._RenderSession, "_browser_ok", fake_ok)
    monkeypatch.setattr(fetcher, "_render_one",
                        lambda b, u, r, t: fetcher.FetchResult(u, u, 200, "<p>" + "内容" * 200 + "</p>",
                                                               headers={"x-rendered": "playwright"}))


def test_render_session_reuses_one_browser(monkeypatch):
    launches, closes = [], []
    _stub_session_browser(monkeypatch, launches, closes)
    with fetcher.render_session():
        for i in range(6):
            r = fetcher._render_fetch(f"http://x/{i}", None, None)
            assert r is not None and r.rendered
    assert len(launches) == 1, f"整批只应启动 1 次浏览器,实际 {len(launches)}"
    assert len(closes) == 1, "批次结束应关闭浏览器 1 次"


def test_render_without_session_is_isolated(monkeypatch):
    """无会话(单页试抓):不复用,一次性启停(此处 Playwright 不可用→降级 None)。"""
    monkeypatch.setattr(settings, "playwright_enabled", True)
    monkeypatch.setattr(fetcher, "_playwright_available", lambda: False)
    assert fetcher._render_fetch("http://x", None, None) is None


def test_render_session_nesting_reuses(monkeypatch):
    launches, closes = [], []
    _stub_session_browser(monkeypatch, launches, closes)
    with fetcher.render_session() as outer:
        fetcher._render_fetch("http://a", None, None)
        with fetcher.render_session() as inner:      # 嵌套:复用外层,不新建
            assert inner is outer
            fetcher._render_fetch("http://b", None, None)
        # 内层退出不应关闭浏览器
        assert len(closes) == 0
        assert getattr(fetcher._render_local, "session", None) is outer
    assert len(launches) == 1 and len(closes) == 1
    assert getattr(fetcher._render_local, "session", None) is None


def test_render_session_recycles_after_threshold(monkeypatch):
    launches, closes = [], []
    _stub_session_browser(monkeypatch, launches, closes)
    monkeypatch.setattr(settings, "render_recycle_after", 3)
    with fetcher.render_session():
        for i in range(7):                            # 3 页回收一次 → 期间重启
            fetcher._render_fetch(f"http://x/{i}", None, None)
    # 7 页、阈值3:第3、6页各回收关闭一次,批次末再关一次 → 关闭≥2、启动≥2
    assert len(launches) >= 2 and len(closes) >= 2


# ---------------- 站内检索(site:) ----------------

def test_site_augments_query(db, need):
    src = Source(name="站内检索", kind="query", adapter="baidu_search", credibility="S4",
                 tier="B", lifecycle="active", serves_needs=[need.id],
                 adapter_config={"site": "cac.gov.cn"})
    db.add(src); db.flush()
    a = BaiduSearchAdapter(src)
    assert a._augment("数据泄露") == "数据泄露 site:cac.gov.cn"
    assert "site%3Acac.gov.cn" in a.build_url("数据泄露", 0, None)


def test_to_search_retry_creates_query_source(db, admin):
    r = create_source(SourceIn(name="某政务栏目", entry_url="https://www.somegov.gov.cn/news",
                               kind="page", credibility="S1"), db, admin)
    out = source_to_search_retry(r["id"], retire_original=True, db=db, _=admin)
    assert out["created"] is True and out["site"] == "somegov.gov.cn"
    # 原页面源被停用
    assert db.get(Source, r["id"]).lifecycle == "retired"
    # 新建的检索型源带 site 配置
    retry = db.query(Source).filter_by(identity_key="site:somegov.gov.cn").one()
    assert retry.kind == "query" and retry.adapter_config["site"] == "somegov.gov.cn"
    assert retry.adapter_config["list_order"] == "relevance" and retry.credibility == "S1"


def test_to_search_retry_rejects_query_source(db, admin):
    r = create_source(SourceIn(name="已经是检索型", kind="query"), db, admin)
    with pytest.raises(Exception):  # 422
        source_to_search_retry(r["id"], db=db, _=admin)
