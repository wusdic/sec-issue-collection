"""数据源管理:手动添加(含自动适配器/同域合并)+ 删除(有文档转停用、无文档物理删)。"""
import pytest

from app.api.routes import SourceIn, create_source, delete_source, list_sources
from app.api.routes import test_fetch_source as run_test_fetch  # 别名:避免 pytest 误当测试收集
from app.models import AppUser, RawDocument, Source
from app.services import url_tools


@pytest.fixture()
def admin(db):
    u = db.query(AppUser).filter_by(role="admin").first()
    if not u:
        from app.auth import hash_password
        u = AppUser(username="admin1", display_name="admin1",
                    password_hash=hash_password("x"), role="admin")
        db.add(u); db.flush()
    return u


def test_add_page_source_auto_adapter(db, admin):
    body = SourceIn(name="某安全资讯", entry_url="https://newsrc.example.com/news",
                    kind="page", credibility="S3")
    r = create_source(body, db, admin)
    assert r["merged"] is False
    src = db.get(Source, r["id"])
    assert src.adapter == "generic_rss"        # 页面型留空 → 自动通用 RSS(带 list 回退)
    assert src.lifecycle == "active"
    assert src.discovered_from == "manual"
    assert src.identity_key == url_tools.identity_key_for("https://newsrc.example.com/news")


def test_add_query_source_needs_no_url(db, admin):
    body = SourceIn(name="定题检索源", kind="query", credibility="S4")
    r = create_source(body, db, admin)
    assert db.get(Source, r["id"]).adapter == "baidu_search"  # 检索型 → 自动搜索引擎


def test_page_source_requires_url(db, admin):
    with pytest.raises(Exception):  # HTTPException 422
        create_source(SourceIn(name="没链接的页面源", kind="page"), db, admin)


def test_same_domain_merges_not_duplicates(db, admin):
    a = create_source(SourceIn(name="A 栏目", entry_url="https://dup.example.com/a",
                               kind="page"), db, admin)
    b = create_source(SourceIn(name="B 栏目", entry_url="https://dup.example.com/b",
                               kind="page"), db, admin)
    assert b["merged"] is True
    assert b["id"] == a["id"]                 # 同注册域合并到同一源


def test_delete_source_without_docs_is_hard_deleted(db, admin):
    r = create_source(SourceIn(name="待删源", entry_url="https://del1.example.com/",
                               kind="page"), db, admin)
    out = delete_source(r["id"], db, admin)
    assert out["action"] == "deleted"
    assert db.get(Source, r["id"]) is None


def test_test_fetch_query_source(db, admin, monkeypatch):
    """一键试抓:检索型源用样本词抓一页,返回条目(不入库)。"""
    from app.services import adapters
    from app.services.adapters import BaiduSearchAdapter, DiscoveredItem

    r = create_source(SourceIn(name="试抓检索源", kind="query", credibility="S4"), db, admin)

    class _Fake(BaiduSearchAdapter):
        def search_page(self, query, page, time_filter=None):
            return [DiscoveredItem(url="https://a.example.com/1", title="某公司数据泄露"),
                    DiscoveredItem(url="https://b.example.com/2", title="勒索攻击")] if page == 0 else None

    monkeypatch.setattr(adapters, "get_adapter", lambda s: _Fake(s))
    out = run_test_fetch(r["id"], q="数据泄露", db=db, _=admin)
    assert out["ok"] is True
    assert out["count"] == 2
    assert out["query"] == "数据泄露"
    assert len(out["items"]) == 2 and out["items"][0]["url"] == "https://a.example.com/1"


def test_test_fetch_reports_adapter_error(db, admin, monkeypatch):
    from app.services import adapters
    r = create_source(SourceIn(name="报错源", entry_url="https://err.example.com/",
                               kind="page"), db, admin)

    class _Boom:
        kind = "page"
        def discover_page(self, page):
            raise RuntimeError("连接被拒绝")

    monkeypatch.setattr(adapters, "get_adapter", lambda s: _Boom())
    out = run_test_fetch(r["id"], db=db, _=admin)
    assert out["ok"] is False and "连接被拒绝" in out["error"]


def test_auto_trial_threshold_from_settings(db, need, monkeypatch):
    """新源自动入库阈值取运行时设置:调低后单渠道候选也能自动建 trial 源。"""
    from app.config import settings
    from app.models import SourceDiscoveryEvidence
    from app.services import discovery
    # 造一个只有单渠道证据的候选(评分约 2×1 通道 +新鲜度1 = 3)
    db.add(SourceDiscoveryEvidence(identity_key="newsrc.auto.com", display_name="自动源",
                                   kind_guess="website", channel="event_search",
                                   evidence_url="https://newsrc.auto.com/x"))
    db.flush()
    monkeypatch.setattr(settings, "discovery_auto_trial_threshold", 2.0)
    res = discovery.evaluate_candidates(db, need.id)
    auto = [c for c in res if c.get("auto_trial") and c["identity_key"] == "newsrc.auto.com"]
    assert auto, f"阈值调低后应自动入库: {res}"
    src = db.query(Source).filter_by(identity_key="newsrc.auto.com").first()
    assert src and src.lifecycle == "trial" and src.credibility == "S4"


def test_delete_source_with_docs_is_retired(db, admin, need):
    r = create_source(SourceIn(name="有文档源", entry_url="https://del2.example.com/",
                               kind="page"), db, admin)
    sid = r["id"]
    db.add(RawDocument(need_id=need.id, source_id=sid, url="https://del2.example.com/x",
                       url_normalized="https://del2.example.com/x", content_text="正文"))
    db.flush()
    out = delete_source(sid, db, admin)
    assert out["action"] == "retired"
    assert db.get(Source, sid).lifecycle == "retired"
    # 停用源默认不出现在生效列表里(前端按 lifecycle 过滤展示)
    active = [s for s in list_sources(None, db, admin) if s["id"] == sid]
    assert active and active[0]["lifecycle"] == "retired"
