"""公众号作为数据源:粘一条文章链接即可跟踪该号;检索真正限定到该号。"""
import pytest

from app.api.routes import SourceIn, create_source
from app.models import AppUser, Source
from app.services import url_tools, wechat

ARTICLE = "https://mp.weixin.qq.com/s/5XUyLv7701cgN0qfLxhEdQ?click_id=18173441"

_HTML = '''<html><head>
<meta property="og:title" content="某银行遭勒索攻击事件复盘" />
</head><body>
<a id="js_name">安全内参</a>
<script>var nickname = "安全内参"; var msg_title = "某银行遭勒索攻击事件复盘";</script>
</body></html>'''


@pytest.fixture()
def admin(db):
    u = db.query(AppUser).filter_by(role="admin").first()
    if not u:
        from app.auth import hash_password
        u = AppUser(username="admin_wx", display_name="admin_wx",
                    password_hash=hash_password("x"), role="admin")
        db.add(u); db.flush()
    return u


# ---------------- 链接识别与解析 ----------------

def test_is_wechat_article_url():
    assert wechat.is_wechat_article_url(ARTICLE)
    assert wechat.is_wechat_article_url("https://mp.weixin.qq.com/s?__biz=abc&idx=1")
    assert not wechat.is_wechat_article_url("https://www.freebuf.com/news/1.html")
    assert not wechat.is_wechat_article_url(None)


def test_tracking_params_stripped():
    """分享链的 click_id 等追踪参数必须剥掉,否则同一篇文章会反复入库。"""
    a = url_tools.normalize_url(ARTICLE)
    b = url_tools.normalize_url("https://mp.weixin.qq.com/s/5XUyLv7701cgN0qfLxhEdQ")
    assert a == b, (a, b)


def test_article_meta_parses_account_and_title():
    meta = wechat.article_meta(_HTML)
    assert meta["account"] == "安全内参"
    assert "勒索" in meta["title"]


def test_resolve_account_reports_failure_gracefully():
    class _Bad:
        ok = False
        error = "403"
    out = wechat.resolve_account(ARTICLE, fetch=lambda *a, **k: _Bad())
    assert out["ok"] is False and out["account"] is None and out["error"]


# ---------------- 建源 ----------------

def _ok_fetch(*a, **k):
    from app.services.fetcher import FetchResult
    return FetchResult(ARTICLE, ARTICLE, 200, _HTML)


def test_paste_article_url_creates_account_source(db, admin, monkeypatch):
    """粘一条公众号文章链接 → 建成"该公众号"的持续源,而不是只存这一篇。"""
    monkeypatch.setattr(wechat, "resolve_account",
                        lambda url, fetch=None: {"account": "安全内参", "title": "某银行遭勒索攻击事件复盘",
                                                 "ok": True, "error": None})
    r = create_source(SourceIn(name="", entry_url=ARTICLE), db, admin)
    assert r["account"] == "安全内参"
    src = db.get(Source, r["id"])
    assert src.kind == "query" and src.adapter == "sogou_wechat"
    assert src.adapter_config["account"] == "安全内参"
    assert src.identity_key == "mp:安全内参" and src.site_key == "mp:安全内参"
    assert src.name == "安全内参"      # 未填名称时用识别出的号名


def test_same_account_merges_not_duplicated(db, admin, monkeypatch):
    monkeypatch.setattr(wechat, "resolve_account",
                        lambda url, fetch=None: {"account": "网安寻路人", "title": "t", "ok": True, "error": None})
    a = create_source(SourceIn(name="", entry_url=ARTICLE), db, admin)
    b = create_source(SourceIn(name="网安寻路人", kind="wechat"), db, admin)   # 换种方式再加一次
    assert b["merged"] is True and b["id"] == a["id"]


def test_add_by_account_name(db, admin):
    r = create_source(SourceIn(name="安全牛", kind="wechat"), db, admin)
    src = db.get(Source, r["id"])
    assert src.adapter_config["account"] == "安全牛" and src.identity_key == "mp:安全牛"


# ---------------- 检索真正限定到该号 ----------------

def test_sogou_adapter_scopes_query_to_account(db, need):
    from app.services.adapters import SogouWechatAdapter
    src = Source(name="安全内参", kind="query", adapter="sogou_wechat", credibility="S3",
                 tier="B", lifecycle="active", serves_needs=[need.id],
                 adapter_config={"account": "安全内参"})
    db.add(src); db.flush()
    a = SogouWechatAdapter(src)
    assert a._augment("数据泄露") == "安全内参 数据泄露"   # 此前 account 完全没被使用
    from urllib.parse import unquote
    assert "安全内参" in unquote(a.build_url("数据泄露", 0, None))   # URL 里是百分号编码


def test_sogou_adapter_filters_other_accounts(db, need):
    """限定公众号后,搜狗返回的其他号结果要被过滤掉。"""
    from app.services.adapters import SogouWechatAdapter
    src = Source(name="安全内参", kind="query", adapter="sogou_wechat", credibility="S3",
                 tier="B", lifecycle="active", serves_needs=[need.id],
                 adapter_config={"account": "安全内参"})
    db.add(src); db.flush()
    html = '''<ul class="news-list">
      <li><h3><a href="/link?url=A">本号文章</a></h3><a class="account">安全内参</a></li>
      <li><h3><a href="/link?url=B">别人家的</a></h3><a class="account">其他号</a></li>
    </ul>'''
    items = SogouWechatAdapter(src).parse(html)
    assert len(items) == 1 and items[0].title == "本号文章"
    assert items[0].wechat_account == "安全内参"
