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
