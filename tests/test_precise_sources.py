"""数据源精准度:栏目必须"内容相关"才算数,根域源不再抓首页而是精准定位。

用户诉求:"数据源需要精准到具体的栏目或者能够精准定位到相关内容的栏目或者页面集合"。
对应三条保证:
1) 栏目验证除结构一致性外还要过内容相关度(挡掉"要闻/领导活动"这类结构规整但不相关的栏目);
2) 根域源识别不到相关栏目时转站内检索(site:域名+关键词),不再抓首页要闻;
3) 源列表能看出每个源精不精准,可一键定位栏目并落库。
"""
import pytest

from app.config import settings
from app.models import Source
from app.services import columns

# 结构规整但内容全是"领导活动/要闻"——旧逻辑会当成有效栏目
_IRRELEVANT_HTML = """<html><body>
  <a href="/yw/2026-07/01/a.htm">习近平会见哈萨克斯坦总统托卡耶夫</a>
  <a href="/yw/2026-07/02/b.htm">李强主持召开国务院常务会议</a>
  <a href="/yw/2026-06/15/c.htm">中央经济工作会议在北京举行</a>
  <a href="/yw/2026-06/10/d.htm">全国两会代表委员热议高质量发展</a>
  <a href="/yw/2026-05/09/e.htm">领导同志赴基层调研指导工作</a>
  <a href="/yw/2026-05/08/f.htm">我国经济运行总体平稳</a>
</body></html>"""

_RELEVANT_HTML = """<html><body>
  <a href="/zhifa/2026-07/01/a.htm">关于某公司数据泄露事件的情况通报</a>
  <a href="/zhifa/2026-07/02/b.htm">某企业未履行网络安全保护义务被行政处罚</a>
  <a href="/zhifa/2026-06/15/c.htm">网络安全法执法典型案例公布</a>
  <a href="/zhifa/2026-06/10/d.htm">某平台违法违规收集个人信息被查处</a>
  <a href="/zhifa/2026-05/09/e.htm">关于防范新型勒索病毒攻击的风险提示</a>
</body></html>"""


def _stub_fetch(monkeypatch, mapping: dict, default: str = ""):
    def fake(url, **k):
        html = default
        for frag, h in mapping.items():
            if frag in url:
                html = h
                break
        return columns.fetcher.FetchResult(url, url, 200, html)
    monkeypatch.setattr(columns.fetcher, "fetch", fake)


# ---------------- ① 栏目内容相关度 ----------------

def test_relevance_terms_cover_security_words():
    columns.reset_terms_cache()
    terms = columns.relevance_terms()
    assert "数据泄露" in terms or "泄露" in terms
    assert "处罚" in terms and "通报" in terms


def test_validate_column_rejects_irrelevant_but_consistent(monkeypatch):
    """结构一致性满分、篇数够,但标题全是要闻 → 不该被当成"相关栏目"。"""
    monkeypatch.setattr(settings, "column_min_articles", 5)
    monkeypatch.setattr(settings, "column_consistency_min", 0.5)
    monkeypatch.setattr(settings, "column_relevance_min", 0.3)
    _stub_fetch(monkeypatch, {"/yw/": _IRRELEVANT_HTML}, _IRRELEVANT_HTML)
    v = columns.validate_column("https://g.cn/yw/")
    assert v["article_count"] >= 5 and v["consistency"] >= 0.5   # 结构上确实是个栏目
    assert v["relevance"] < 0.3 and v["valid"] is False          # 但内容不相关 → 拒
    assert "相关度" in v["reason"]


def test_validate_column_accepts_relevant(monkeypatch):
    monkeypatch.setattr(settings, "column_min_articles", 5)
    monkeypatch.setattr(settings, "column_consistency_min", 0.5)
    monkeypatch.setattr(settings, "column_relevance_min", 0.3)
    _stub_fetch(monkeypatch, {}, _RELEVANT_HTML)
    v = columns.validate_column("https://g.cn/zhifa/")
    assert v["valid"] is True and v["relevance"] >= 0.3


def test_validate_column_skips_relevance_when_no_titles(monkeypatch):
    """全是图片链接(无标题文字)→ 无法判相关度,只按结构判,不误杀。"""
    monkeypatch.setattr(settings, "column_min_articles", 5)
    monkeypatch.setattr(settings, "column_consistency_min", 0.5)
    monkeypatch.setattr(settings, "column_relevance_min", 0.9)
    html = "".join(f'<a href="/col/2026-07/{i}/a.htm"><img src="x.jpg"></a>' for i in range(6))
    _stub_fetch(monkeypatch, {}, html)
    v = columns.validate_column("https://g.cn/col/")
    assert v["titles_sampled"] == 0 and v["valid"] is True


def test_discover_and_persist_only_keeps_relevant_columns(db, need, monkeypatch):
    monkeypatch.setattr(settings, "column_min_articles", 5)
    monkeypatch.setattr(settings, "column_consistency_min", 0.5)
    monkeypatch.setattr(settings, "column_relevance_min", 0.3)
    monkeypatch.setattr(settings, "auto_column_refresh_days", 7)
    root = Source(name="精准度测试站", kind="page", adapter="generic_rss", credibility="S1",
                  tier="B", lifecycle="active", serves_needs=[need.id],
                  entry_url="https://prec-x.cn/", site_key="prec-x.cn",
                  identity_key="prec-x.cn", adapter_config={})
    db.add(root); db.flush()
    root_html = ('<a href="/yw/index.htm">安全要闻</a>'
                 '<a href="/zhifa/index.htm">执法处罚</a>')
    _stub_fetch(monkeypatch, {"/yw/": _IRRELEVANT_HTML, "/zhifa/": _RELEVANT_HTML}, root_html)
    kids, recomputed = columns.discover_and_persist(db, root)
    assert recomputed is True
    urls = [k.entry_url for k in kids]
    assert any("zhifa" in u for u in urls)          # 相关栏目留下
    assert not any("/yw/" in u for u in urls)       # 要闻栏目被内容相关度挡掉


# ---------------- ② 根域源不再抓首页 ----------------

def test_root_source_falls_back_to_site_search_not_homepage(db, need, monkeypatch):
    """根域源识别不到栏目时:建站内检索兄弟源,且绝不调用 discover_page 抓首页。"""
    from app.services import pipeline
    monkeypatch.setattr(settings, "root_no_column_fallback", "search")
    root = Source(name="兜底测试站", kind="page", adapter="generic_rss", credibility="S3",
                  tier="B", lifecycle="active", serves_needs=[need.id],
                  entry_url="https://fallback-x.cn/", site_key="fallback-x.cn",
                  identity_key="fallback-x.cn", adapter_config={})
    db.add(root); db.flush()
    monkeypatch.setattr(columns, "discover_and_persist", lambda *a, **k: ([], True))

    called = {"discover": 0, "search": 0}

    class _A:
        kind = "page"
        def discover_page(self, page):
            called["discover"] += 1
            return None
        def search_page(self, q, page):
            called["search"] += 1
            return None

    monkeypatch.setattr(pipeline, "get_adapter", lambda s: _A())
    pipeline.crawl_source(db, need, root, queries=["数据泄露"], max_pages=1, do_archive=False)
    assert called["discover"] == 0            # 关键:没有去抓首页
    sib = db.query(Source).filter_by(identity_key="site:fallback-x.cn").one_or_none()
    assert sib is not None and sib.kind == "query"
    assert (sib.adapter_config or {}).get("site") == "fallback-x.cn"
    assert called["search"] >= 1              # 走的是站内检索


def test_root_fallback_skip_mode(db, need, monkeypatch):
    from app.services import pipeline
    monkeypatch.setattr(settings, "root_no_column_fallback", "skip")
    root = Source(name="跳过测试站", kind="page", adapter="generic_rss", credibility="S3",
                  tier="B", lifecycle="active", serves_needs=[need.id],
                  entry_url="https://skip-x.cn/", site_key="skip-x.cn",
                  identity_key="skip-x.cn", adapter_config={})
    db.add(root); db.flush()
    monkeypatch.setattr(columns, "discover_and_persist", lambda *a, **k: ([], True))
    hits = {"n": 0}

    class _A:
        kind = "page"
        def discover_page(self, page):
            hits["n"] += 1
            return None

    monkeypatch.setattr(pipeline, "get_adapter", lambda s: _A())
    pipeline.crawl_source(db, need, root, queries=["数据泄露"], max_pages=1, do_archive=False)
    assert hits["n"] == 0        # 既不抓首页
    assert db.query(Source).filter_by(identity_key="site:skip-x.cn").one_or_none() is None  # 也不建检索源


# ---------------- ③ 精准度可见 + 一键定位 ----------------

def test_precision_of_levels(db, need):
    col = Source(name="栏目源", kind="page", adapter="generic_list", entry_url="https://p.cn/col/",
                 credibility="S3", tier="B", lifecycle="active", serves_needs=[need.id])
    root = Source(name="根域源", kind="page", adapter="generic_rss", entry_url="https://p2.cn/",
                  credibility="S3", tier="B", lifecycle="active", serves_needs=[need.id])
    mp = Source(name="公众号源", kind="query", adapter="sogou_wechat", identity_key="mp:安全内参",
                credibility="S3", tier="B", lifecycle="active", serves_needs=[need.id],
                adapter_config={"account": "安全内参"})
    site = Source(name="站内检索", kind="query", adapter="baidu_search", identity_key="site:p3.cn",
                  credibility="S3", tier="B", lifecycle="active", serves_needs=[need.id],
                  adapter_config={"site": "p3.cn"})
    db.add_all([col, root, mp, site]); db.flush()
    assert columns.precision_of(col)["level"] == "column"
    assert columns.precision_of(mp)["level"] == "wechat"
    assert columns.precision_of(site)["level"] == "search"
    p = columns.precision_of(root, db)
    assert p["level"] == "root" and p["precise"] is False

    child = Source(name="根域源·执法", kind="page", adapter="generic_list",
                   entry_url="https://p2.cn/zhifa/", credibility="S3", tier="B",
                   lifecycle="active", serves_needs=[need.id], discovered_from="column_auto",
                   adapter_config={"parent_site_id": root.id})
    db.add(child); db.flush()
    assert columns.precision_of(root, db)["level"] == "resolved"   # 已定位到栏目 → 转精准


def test_discover_columns_endpoint_persists(db, need, monkeypatch, admin_user):
    from app.api.routes import source_discover_columns
    monkeypatch.setattr(settings, "column_min_articles", 5)
    monkeypatch.setattr(settings, "column_consistency_min", 0.5)
    monkeypatch.setattr(settings, "column_relevance_min", 0.3)
    root = Source(name="一键定位站", kind="page", adapter="generic_rss", credibility="S3",
                  tier="B", lifecycle="active", serves_needs=[need.id],
                  entry_url="https://locate-x.cn/", site_key="locate-x.cn",
                  identity_key="locate-x.cn", adapter_config={})
    db.add(root); db.flush()
    _stub_fetch(monkeypatch, {"/zhifa/": _RELEVANT_HTML}, '<a href="/zhifa/index.htm">执法处罚</a>')
    out = source_discover_columns(root.id, persist=True, db=db, _=admin_user)
    assert out["persisted"] is True and out["count"] == 1
    kid = db.query(Source).filter_by(entry_url=out["columns"][0]["url"]).one()
    assert (kid.adapter_config or {}).get("parent_site_id") == root.id
    assert columns.precision_of(root, db)["precise"] is True


def test_manually_deleted_column_not_resurrected(db, need, monkeypatch, admin_user):
    """人工删掉的自动栏目,下次栏目发现不该又把它拉回来抓。"""
    from app.api.routes import delete_source
    monkeypatch.setattr(settings, "column_min_articles", 5)
    monkeypatch.setattr(settings, "column_consistency_min", 0.5)
    monkeypatch.setattr(settings, "column_relevance_min", 0.3)
    root = Source(name="不复活站", kind="page", adapter="generic_rss", credibility="S3",
                  tier="B", lifecycle="active", serves_needs=[need.id],
                  entry_url="https://revive-x.cn/", site_key="revive-x.cn",
                  identity_key="revive-x.cn", adapter_config={})
    db.add(root); db.flush()
    _stub_fetch(monkeypatch, {"/zhifa/": _RELEVANT_HTML}, '<a href="/zhifa/index.htm">执法处罚</a>')
    kids, _ = columns.discover_and_persist(db, root)
    assert len(kids) == 1
    kid_id = kids[0].id
    from app.models import RawDocument
    db.add(RawDocument(source_id=kid_id, need_id=need.id, url="https://revive-x.cn/zhifa/x.htm",
                       url_normalized="https://revive-x.cn/zhifa/x.htm", title="t"))
    db.flush()
    assert delete_source(kid_id, db, admin_user)["action"] == "retired"   # 有文档 → 转停用
    cfg = dict(root.adapter_config or {})
    cfg.pop("columns_discovered_at", None)          # 强制重新识别
    root.adapter_config = cfg
    kids2, _ = columns.discover_and_persist(db, root)
    assert kid_id not in [k.id for k in kids2]      # 不复活
    assert db.get(Source, kid_id).lifecycle == "retired"


@pytest.fixture()
def admin_user(db):
    from app.models import AppUser
    u = db.query(AppUser).filter_by(role="admin").first()
    if not u:
        from app.auth import hash_password
        u = AppUser(username="admin_prec", display_name="admin_prec",
                    password_hash=hash_password("x"), role="admin")
        db.add(u); db.flush()
    return u
