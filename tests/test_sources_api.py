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
    # identity_key = 采集目标(归一化 URL 栏目粒度);site_key = 站点身份(注册域)
    assert src.identity_key == url_tools.normalize_url("https://newsrc.example.com/news")
    assert src.site_key == "example.com"


def test_add_query_source_needs_no_url(db, admin):
    body = SourceIn(name="定题检索源", kind="query", credibility="S4")
    r = create_source(body, db, admin)
    assert db.get(Source, r["id"]).adapter == "baidu_search"  # 检索型 → 自动搜索引擎


def test_page_source_requires_url(db, admin):
    with pytest.raises(Exception):  # HTTPException 422
        create_source(SourceIn(name="没链接的页面源", kind="page"), db, admin)


def test_same_column_merges_but_different_columns_dont(db, admin):
    # 同一栏目(同 URL)→ 合并
    a = create_source(SourceIn(name="A 栏目", entry_url="https://colsite.com/a",
                               kind="page"), db, admin)
    a2 = create_source(SourceIn(name="A 栏目(重复)", entry_url="https://colsite.com/a",
                                kind="page"), db, admin)
    assert a2["merged"] is True and a2["id"] == a["id"]
    # 同站不同栏目 → 各算一条(不合并),但共享 site_key
    b = create_source(SourceIn(name="B 栏目", entry_url="https://colsite.com/b",
                               kind="page"), db, admin)
    assert b["merged"] is False and b["id"] != a["id"]
    assert db.get(Source, a["id"]).site_key == db.get(Source, b["id"]).site_key == "colsite.com"
    assert db.get(Source, a["id"]).identity_key != db.get(Source, b["id"]).identity_key


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


def test_batch_test_marks_and_auto_retires(db, admin, monkeypatch):
    """批量体检 mark=True:抓不到累加 fail_streak,达阈值自动停用;成功则清零。"""
    from app.config import settings
    from app.services import adapters
    from app.services.adapters import DiscoveredItem
    monkeypatch.setattr(settings, "source_auto_retire_fail_streak", 3)

    # 各源用不同注册域(identity_key 按 eTLD+1 归并,同域会合并)
    r = create_source(SourceIn(name="时好时坏源", entry_url="https://flakysrc.net/",
                               kind="page"), db, admin)
    sid = r["id"]

    class _Empty:
        kind = "page"
        def discover_page(self, page):
            return []                       # 抓不到任何条目 = 失败

    monkeypatch.setattr(adapters, "get_adapter", lambda s: _Empty())
    o1 = run_test_fetch(sid, mark=True, db=db, _=admin)
    assert o1["fail_streak"] == 1 and o1["retired"] is False
    o2 = run_test_fetch(sid, mark=True, db=db, _=admin)
    assert o2["fail_streak"] == 2 and o2["retired"] is False
    o3 = run_test_fetch(sid, mark=True, db=db, _=admin)
    assert o3["fail_streak"] == 3 and o3["retired"] is True
    assert db.get(Source, sid).lifecycle == "retired"

    # 手动单测 mark=False 不改状态
    r2 = create_source(SourceIn(name="纯探测源", entry_url="https://probesrc.net/",
                                kind="page"), db, admin)
    run_test_fetch(r2["id"], mark=False, db=db, _=admin)
    assert db.get(Source, r2["id"]).fail_streak == 0

    # 成功清零:先攒一次失败,再成功
    r3 = create_source(SourceIn(name="恢复源", entry_url="https://recoversrc.net/",
                                kind="page"), db, admin)
    run_test_fetch(r3["id"], mark=True, db=db, _=admin)
    assert db.get(Source, r3["id"]).fail_streak == 1
    monkeypatch.setattr(adapters, "get_adapter",
                        lambda s: type("_Ok", (), {"kind": "page",
                        "discover_page": lambda self, p: [DiscoveredItem(url="https://recoversrc.net/a", title="有货")]})())
    ok = run_test_fetch(r3["id"], mark=True, db=db, _=admin)
    assert ok["ok"] is True and ok["count"] == 1
    assert db.get(Source, r3["id"]).fail_streak == 0


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
    # 候选键 = 站点身份(site_key);采集目标 identity_key = 归一化入口 URL
    src = db.query(Source).filter_by(site_key="newsrc.auto.com").first()
    assert src and src.lifecycle == "trial" and src.credibility == "S4"


def test_duplicate_scan_groups_by_site_not_column(db, admin, need):
    from app.services import discovery
    create_source(SourceIn(name="栏目甲", entry_url="https://multicol.cn/a",
                           kind="page", need_id=need.id), db, admin)
    create_source(SourceIn(name="栏目乙", entry_url="https://multicol.cn/b",
                           kind="page", need_id=need.id), db, admin)
    groups = discovery.duplicate_groups(db, need.id)
    g = [x for x in groups if x["site_key"] == "multicol.cn"]
    assert g and len(g[0]["sources"]) == 2
    assert g[0]["has_exact_duplicate"] is False   # 不同栏目 → 非真重复


def test_recompute_keys_merges_duplicate_targets(db, need):
    """同一采集目标(root www 与非www)自动查重合并,不再触发唯一约束500。"""
    from app.services import discovery
    a = Source(name="站A(有文档)", kind="page", adapter="generic_rss", credibility="S1", tier="B",
               lifecycle="active", serves_needs=[need.id], entry_url="https://www.dupmerge.cn/",
               stat_docs_total=7, identity_key=None, site_key=None)
    b = Source(name="站B(无文档)", kind="page", adapter="generic_rss", credibility="S1", tier="B",
               lifecycle="active", serves_needs=["other_need"], entry_url="https://dupmerge.cn/",
               stat_docs_total=0, identity_key=None, site_key=None)
    db.add_all([a, b]); db.flush()
    res = discovery.recompute_keys(db)
    assert res["merged"] >= 1
    # 文档多者保留并拿到目标键;另一个转停用、目标键清空
    assert a.lifecycle == "active" and a.identity_key == "dupmerge.cn"
    assert b.lifecycle == "retired" and b.identity_key is None
    assert "other_need" in a.serves_needs   # 服务需求已并入保留者


def test_recompute_keys_backfills_legacy(db, admin, need):
    from app.services import discovery
    # 造一条"旧数据":无 site_key
    s = Source(name="旧源", kind="page", adapter="generic_rss", credibility="S3", tier="B",
               lifecycle="active", serves_needs=[need.id], entry_url="https://legacy.cn/col",
               identity_key=None, site_key=None)
    db.add(s); db.flush()
    discovery.recompute_keys(db)
    assert db.get(Source, s.id).site_key == "legacy.cn"
    assert db.get(Source, s.id).identity_key == url_tools.normalize_url("https://legacy.cn/col")


def test_discovery_skips_site_already_covered(db, need):
    from app.models import Source as S
    from app.models import SourceDiscoveryEvidence
    from app.services import discovery
    # 已有该站一个栏目源(site_key=covered.cn)
    db.add(S(name="已有栏目", kind="page", adapter="generic_rss", credibility="S3", tier="B",
             lifecycle="active", serves_needs=[need.id], entry_url="https://covered.cn/x",
             site_key="covered.cn", identity_key="https://covered.cn/x"))
    # 同站的候选证据
    db.add(SourceDiscoveryEvidence(identity_key="covered.cn", display_name="覆盖站",
                                   kind_guess="website", channel="event_search",
                                   evidence_url="https://covered.cn/y"))
    db.flush()
    res = discovery.evaluate_candidates(db, need.id)
    assert not any(c["identity_key"] == "covered.cn" for c in res)  # 站已覆盖 → 不再当候选


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
