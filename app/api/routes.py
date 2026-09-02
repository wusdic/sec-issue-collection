"""REST API(详细设计 §5):/api/v1 全部端点。"""
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth import create_token, current_user, require_roles, verify_password
from app.config import settings
from app.db import get_session
from app.models import (
    AppUser, ArchiveManifest, AuditLog, Event, EventChangeLog, FollowupTask, Lead,
    NeedProfile, RawDocument, ReviewTask, Source, SourceDiscoveryEvidence, WatchTarget,
)
from app.services import discovery as discovery_svc
from app.services import followup as followup_svc
from app.services import kpi as kpi_svc
from app.services import leads as leads_svc
from app.services import review as review_svc
from app.services import url_tools
from app.services import wechat
from app.services.errors import error_headline
from app.services.events import PublishError, log_change, update_payload
from app.services.extraction import load_record_schema
from app.services import need_ctx
from app.services.profiles import get_active_profile

api = APIRouter(prefix="/api/v1")


def _record_schema(db: Session, need_id: str) -> dict:
    return need_ctx.for_need(get_active_profile(db, need_id)).record_schema()


def _confirm_allowed(db: Session, need_id: str) -> list[str]:
    return need_ctx.for_need(get_active_profile(db, need_id)).confirm_allowed


def need_id_param(need_id: str | None = Query(None)) -> str:
    """查询参数 need_id 缺省 = 平台默认需求(设置项 default_need_id)。"""
    return need_id or need_ctx.default_need_id()


def _nid(v: str | None) -> str:
    return v or need_ctx.default_need_id()


# ---------- 认证 ----------

class LoginIn(BaseModel):
    username: str
    password: str


@api.post("/auth/login")
def login(body: LoginIn, db: Session = Depends(get_session)):
    user = db.query(AppUser).filter_by(username=body.username).one_or_none()
    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(401, "用户名或口令错误")
    return {"token": create_token(user), "role": user.role}


# ---------- 源管理 M1 ----------

@api.get("/sources")
def list_sources(lifecycle: str | None = None, db: Session = Depends(get_session),
                 _: AppUser = Depends(current_user)):
    from app.services import columns as columns_svc
    q = db.query(Source)
    if lifecycle:
        q = q.filter_by(lifecycle=lifecycle)
    rows = q.order_by(Source.id).all()
    # 精准度:一次性统计各根域源已定位到几个栏目,避免逐源查库
    child_n: dict[int, int] = {}
    for s in rows:
        pid = (s.adapter_config or {}).get("parent_site_id")
        if pid and s.discovered_from == "column_auto" and s.lifecycle != "retired":
            child_n[pid] = child_n.get(pid, 0) + 1
    out = []
    for s in rows:
        p = columns_svc.precision_of(s)
        if p["level"] == "root" and child_n.get(s.id):
            n = child_n[s.id]
            p = {"level": "resolved", "precise": True, "label": f"已定位{n}个栏目",
                 "hint": "根域入口,但采集时按已识别的相关栏目分别抓"}
        out.append({"id": s.id, "name": s.name, "kind": s.kind, "adapter": s.adapter,
                    "entry_url": s.entry_url, "note": s.note,
                    "credibility": s.credibility, "tier": s.tier, "lifecycle": s.lifecycle,
                    "identity_key": s.identity_key, "site_key": s.site_key,
                    "discovery_score": s.discovery_score,
                    "manual_assist": s.manual_assist, "docs_total": s.stat_docs_total,
                    "fail_streak": s.fail_streak, "discovered_from": s.discovered_from,
                    "parent_site_id": (s.adapter_config or {}).get("parent_site_id"),
                    "watching": bool((s.adapter_config or {}).get("watch_since")),
                    "auto_retired": bool((s.adapter_config or {}).get("auto_retired_at")),
                    "precision": p["level"], "precise": p["precise"],
                    "precision_label": p["label"], "precision_hint": p["hint"],
                    "last_crawled": s.last_success_at.isoformat() if s.last_success_at else None})
    return out


class SourceIn(BaseModel):
    name: str
    entry_url: str | None = None
    kind: str = "page"                 # page(栏目/RSS 抓取) / query(关键词检索) / wechat(公众号)
    account: str | None = None         # 公众号名(kind=wechat 时用;也可粘文章链接自动解析)
    adapter: str | None = None         # 留空自动:page→generic_rss/list,query→baidu_search
    credibility: str = "S3"
    tier: str = "B"
    note: str | None = None
    need_id: str | None = None


@api.post("/sources", status_code=201)
def create_source(body: SourceIn, db: Session = Depends(get_session),
                  _: AppUser = Depends(require_roles("analyst"))):
    """手动添加数据源。零适配器:留空 adapter 时按类型自动选通用适配器(RSS/列表/搜索)。

    公众号:入口填一条公众号文章链接(mp.weixin.qq.com/s/...)即可——系统自动解析出它属于
    哪个公众号,并把「该公众号」建成源持续跟踪(而不是只收藏这一篇文章)。
    也可直接在名称处填公众号名并把 kind 设为 wechat。
    """
    kind = body.kind if body.kind in ("page", "query", "wechat") else "page"
    if body.credibility not in ("S1", "S2", "S3", "S4"):
        raise HTTPException(422, "可信度须为 S1-S4")
    entry = (body.entry_url or "").strip() or None

    # ① 粘贴公众号文章链接 → 解析出公众号名,建成"该公众号"的持续源
    account = (body.account or "").strip() or None
    if entry and wechat.is_wechat_article_url(entry):
        info = wechat.resolve_account(entry)
        if not info["ok"]:
            raise HTTPException(422, f"公众号文章解析失败:{info['error']}")
        account, kind = info["account"], "wechat"
        if not (body.name or "").strip():
            body.name = info["account"]
    elif kind == "wechat" and not account:
        account = (body.name or "").strip()   # 直接按公众号名添加

    # ② 公众号源:目标键为 mp:账号名,用搜狗微信按该号检索
    if kind == "wechat":
        if not account:
            raise HTTPException(422, "公众号源需要公众号名称,或粘贴一条该号的文章链接")
        ident = f"mp:{account}"
        if dup := db.query(Source).filter_by(identity_key=ident).one_or_none():
            dup.serves_needs = sorted(set(dup.serves_needs or []) | {_nid(body.need_id)})
            if dup.lifecycle == "retired":
                dup.lifecycle = "active"
            db.commit()
            return {"id": dup.id, "merged": True, "name": dup.name, "account": account}
        src = Source(name=(body.name or account).strip(), entry_url=None, kind="query",
                     adapter="sogou_wechat",
                     adapter_config={"account": account, "list_order": "relevance"},
                     credibility=body.credibility, tier=body.tier, lifecycle="active",
                     serves_needs=[_nid(body.need_id)], identity_key=ident, site_key=ident,
                     manual_assist=False, note=body.note or f"公众号:{account}",
                     discovered_from="manual")
        db.add(src)
        db.commit()
        return {"id": src.id, "merged": False, "name": src.name, "account": account}

    if kind == "page" and not entry:
        raise HTTPException(422, "页面型源必须填入口链接(栏目页或 RSS 地址)")
    adapter = (body.adapter or "").strip() or ("baidu_search" if kind == "query" else "generic_rss")
    site_key, ident = url_tools.source_keys(kind, entry)
    # 只在"同一栏目"(identity_key 相同)时合并;同站不同栏目(site_key 同、identity_key 异)各算一条
    if ident and (dup := db.query(Source).filter_by(identity_key=ident).one_or_none()):
        dup.serves_needs = sorted(set(dup.serves_needs or []) | {_nid(body.need_id)})
        if dup.lifecycle == "retired":
            dup.lifecycle = "active"
        db.commit()
        return {"id": dup.id, "merged": True, "name": dup.name}
    src = Source(name=body.name.strip(), entry_url=entry, kind=kind, adapter=adapter,
                 adapter_config={}, credibility=body.credibility, tier=body.tier,
                 lifecycle="active", serves_needs=[_nid(body.need_id)],
                 identity_key=ident, site_key=site_key, manual_assist=False, note=body.note,
                 discovered_from="manual")
    db.add(src)
    db.commit()
    # 只填了根地址 → 提示这不是"精准源",引导去定位栏目(采集时也会自动定位)
    from app.services import columns as columns_svc
    p = columns_svc.precision_of(src, db)
    return {"id": src.id, "merged": False, "name": src.name,
            "precision": p["level"], "precise": p["precise"], "precision_hint": p["hint"]}


@api.delete("/sources/{source_id}")
def delete_source(source_id: int, db: Session = Depends(get_session),
                  _: AppUser = Depends(require_roles("analyst"))):
    """删除数据源:已采过文档的源转『停用』(保留历史与外键完整);无文档的源直接物理删除。"""
    src = db.get(Source, source_id)
    if not src:
        raise HTTPException(404, "源不存在")
    # 打上人工停用标记:自动栏目发现下次不再把它拉回来抓(尊重人工判断)
    cfg = dict(src.adapter_config or {})
    cfg["manually_retired"] = True
    src.adapter_config = cfg
    has_docs = db.query(RawDocument.id).filter_by(source_id=source_id).first() is not None
    if has_docs:
        src.lifecycle = "retired"
        db.commit()
        return {"id": source_id, "action": "retired",
                "note": "该源已有采集文档,转为停用(不再采集,历史保留)"}
    # 无文档才物理删:先清掉仅与该源绑定的记账行(采集记录/关键词记录/水位/日指标),
    # 否则外键约束会导致删除 500。
    from app.models import CrawlRun, KeywordRun, SearchWatermark, SourceMetricDaily
    try:
        for M in (CrawlRun, KeywordRun, SearchWatermark, SourceMetricDaily):
            db.query(M).filter_by(source_id=source_id).delete(synchronize_session=False)
        db.delete(src)
        db.commit()
        return {"id": source_id, "action": "deleted"}
    except Exception:  # noqa: BLE001 兜底:仍删不掉就停用,避免 500
        db.rollback()
        src = db.get(Source, source_id)
        src.lifecycle = "retired"
        db.commit()
        return {"id": source_id, "action": "retired",
                "note": "存在关联记录无法物理删除,已改为停用"}


@api.post("/sources/{source_id}/restore")
def restore_source(source_id: int, db: Session = Depends(get_session),
                   _: AppUser = Depends(require_roles("analyst"))):
    """恢复被(误)停用的源:重新启用并清零失败计数。

    自动停用会有误判(临时网络问题、站点短暂改版),此前停用后页面上没有任何恢复入口。
    """
    from app.services import health
    src = health.restore(db, source_id)
    if not src:
        raise HTTPException(404, "源不存在")
    db.commit()
    return {"id": src.id, "lifecycle": src.lifecycle, "fail_streak": src.fail_streak,
            "name": src.name}


@api.post("/sources/health-check")
def start_health_check(need_id: str = Depends(need_id_param),
                       _: AppUser = Depends(require_roles("analyst"))):
    """启动"一键体检"后台任务(立即返回)。进度用 GET /sources/health-check 查询。"""
    from app.services import health
    return health.start(need_id)


@api.get("/sources/health-check")
def health_check_status(_: AppUser = Depends(current_user)):
    """体检进度(切页/刷新都能查到)。"""
    from app.services import health
    return health.status()


@api.post("/sources/health-check/cancel")
def cancel_health_check(_: AppUser = Depends(require_roles("analyst"))):
    from app.services import health
    health.cancel()
    return {"ok": True, "note": "已请求取消,当前源测完即停"}


@api.post("/sources/locate-columns")
def start_locate_columns(need_id: str = Depends(need_id_param), force: bool = False,
                         _: AppUser = Depends(require_roles("analyst"))):
    """启动"批量精准定位栏目"后台任务:把只填了根地址的源逐个定位到具体栏目(立即返回)。"""
    from app.services import locate
    return locate.start(need_id, force)


@api.get("/sources/locate-columns")
def locate_columns_status(need_id: str = Depends(need_id_param), db: Session = Depends(get_session),
                          _: AppUser = Depends(current_user)):
    """定位进度 + 当前还有多少源没精准到栏目。"""
    from app.services import locate
    st = locate.status()
    if not st.get("running"):
        st = {**st, "pending": len(locate.pending(db, need_id))}
    return st


@api.post("/sources/locate-columns/cancel")
def cancel_locate_columns(_: AppUser = Depends(require_roles("analyst"))):
    from app.services import locate
    locate.cancel()
    return {"ok": True, "note": "已请求取消,当前源定位完即停"}


@api.post("/sources/prospect")
def start_prospect(need_id: str = Depends(need_id_param),
                   _: AppUser = Depends(require_roles("analyst"))):
    """启动"主动找源"后台任务:用找源专用检索词去搜索引擎捞新渠道 → LLM 相关度初评 → 评分自动入库。

    与被动发现(顺着已采文章的引用)互补:没被任何文章引用过的好渠道也能被找到。
    """
    from app.services import prospect
    return prospect.start(need_id)


@api.get("/sources/prospect")
def prospect_status(_: AppUser = Depends(current_user)):
    from app.services import prospect
    return prospect.status()


@api.post("/sources/prospect/cancel")
def cancel_prospect(_: AppUser = Depends(require_roles("analyst"))):
    from app.services import prospect
    prospect.cancel()
    return {"ok": True, "note": "已请求取消,当前检索词跑完即停"}


@api.post("/sources/prospect/selftest")
def prospect_selftest(query: str | None = None, apply: bool = True,
                      need_id: str = Depends(need_id_param),
                      _: AppUser = Depends(require_roles("analyst"))):
    """启动找源路径自检(后台跑,立即返回)。

    开了渲染后一个引擎要十几秒、整池要一两分钟,同步等在页面上必然"点完切页就丢"。
    结果落 AutoOpsRun,切页/刷新回来用 GET 同一路径就能看到跑到哪、上次结论是什么。
    apply=True(默认)时结论直接落到引擎列表——已经算出来的结论不该再丢回给人抄一遍。
    """
    from app.services import prospect
    return prospect.selftest_start(need_id, query, apply=apply)


@api.get("/sources/prospect/selftest")
def prospect_selftest_status(db: Session = Depends(get_session),
                            _: AppUser = Depends(current_user)):
    """自检进度 / 上一次自检结果(切页回来接着看)。"""
    from app.services import prospect
    return prospect.selftest_status(db)


@api.get("/sources/prospect/queries")
def prospect_queries(need_id: str = Depends(need_id_param), db: Session = Depends(get_session),
                     _: AppUser = Depends(current_user)):
    """本轮会用的找源检索词 = 人工维护的基础词 + 覆盖空白自动生成的方向词。"""
    from app.services import coverage, prospect
    ctx = need_ctx.get(db, need_id)
    base = prospect.base_queries(ctx)
    auto = coverage.prospect_queries(db, need_id, ctx=ctx)
    return {"base": base, "from_coverage": auto, "total": len(prospect.build_queries(db, need_id))}


@api.get("/coverage")
def coverage_summary(need_id: str = Depends(need_id_param), days: int | None = None,
                     db: Session = Depends(get_session),
                     _: AppUser = Depends(current_user)):
    """覆盖度盘点:哪些行业近 N 天一条事件都没有(=该去找源的方向)、哪些源在空跑。"""
    from app.services import coverage
    return coverage.summary(db, need_id, days)


# ---------- 系统动作日志(分类分级 + 高级别优先提示) ----------

@api.get("/actions")
def list_actions(need_id: str | None = None, module: str | None = None,
                 min_level: int = 1, unacked: bool | None = None, days: int = 30,
                 limit: int = 100, db: Session = Depends(get_session),
                 _: AppUser = Depends(current_user)):
    """系统动作日志:每一个有后果的动作,按模块分类、按影响分级(1一般/2关注/3重要/4紧急)。"""
    from app.services import actions
    return {"levels": actions.LEVEL_NAME, "modules": actions.MODULE_NAME,
            "notify_level": actions.NOTIFY_LEVEL,
            "items": actions.feed(db, need_id, module, min_level, unacked, days, limit)}


@api.get("/actions/summary")
def actions_summary(need_id: str | None = None, days: int = 7,
                    db: Session = Depends(get_session),
                    _: AppUser = Depends(current_user)):
    """高级别未确认动作汇总:导航角标与各模块顶部提示都用这一份。"""
    from app.services import actions
    return actions.summary(db, need_id, days)


@api.get("/actions/alerts")
def actions_alerts(need_id: str | None = None, module: str | None = None,
                   db: Session = Depends(get_session), _: AppUser = Depends(current_user)):
    """某个模块的优先提示(高级别 + 未确认)。没有就返回空,完全不打扰。"""
    from app.services import actions
    return actions.alerts(db, need_id, module)


class AckIn(BaseModel):
    ids: list[int] | None = None
    module: str | None = None
    all: bool = False
    need_id: str | None = None


@api.post("/actions/ack")
def ack_actions(body: AckIn, db: Session = Depends(get_session),
                user: AppUser = Depends(require_roles("analyst"))):
    """确认已读:高级别动作看过就不再顶在页面上(日志仍完整保留)。"""
    from app.services import actions
    n = (actions.ack_all(db, _nid(body.need_id), body.module, user.id) if body.all
         else actions.ack(db, body.ids or [], user.id))
    db.commit()
    return {"acked": n}


@api.get("/autopilot")
def autopilot_state(need_id: str = Depends(need_id_param), db: Session = Depends(get_session),
                    _: AppUser = Depends(current_user)):
    """源库自动运维总览:各维护任务的周期/上次跑/下次跑、最近执行记录、还剩什么要人处理。"""
    from app.services import autopilot
    return {"enabled": bool(getattr(settings, "autopilot_enabled", True)),
            "hour_utc": int(getattr(settings, "autopilot_hour", 4) or 4),
            "running": autopilot.is_running(),
            "plan": autopilot.plan(db, need_id),
            "recent": autopilot.recent(db, need_id),
            "human_todo": autopilot.human_todo(db, need_id)}


# ---------- 首次部署流程 ----------

@api.get("/bootstrap")
def bootstrap_status(need_id: str = Depends(need_id_param), db: Session = Depends(get_session),
                     _: AppUser = Depends(current_user)):
    """首次流程进度/结果 + 前置检查。切页刷新都能接着看。"""
    from app.services import bootstrap
    return bootstrap.status(db, need_id)


@api.post("/bootstrap/run")
def bootstrap_run(need_id: str = Depends(need_id_param), skip_crawl: bool = False,
                  _: AppUser = Depends(require_roles("analyst"))):
    """一键跑首次部署该做的事(后台,按依赖顺序:整理源 → 先采一轮 → 再找源)。"""
    from app.services import bootstrap
    return bootstrap.start(need_id, skip_crawl=skip_crawl)


@api.post("/bootstrap/cancel")
def bootstrap_cancel(_: AppUser = Depends(require_roles("analyst"))):
    from app.services import bootstrap
    bootstrap.cancel()
    return {"canceled": True}


@api.post("/autopilot/run")
def autopilot_run(need_id: str = Depends(need_id_param), force: bool = False,
                  _: AppUser = Depends(require_roles("analyst"))):
    """立刻跑一轮自动运维(force=不管周期,全部任务都跑一遍)。后台执行,立即返回。"""
    from app.services import autopilot
    return autopilot.start_async(need_id, force)


@api.get("/autopilot/grading-preview")
def grading_preview(need_id: str = Depends(need_id_param), db: Session = Depends(get_session),
                    _: AppUser = Depends(current_user)):
    """自动定级会怎么判(不落库),用来确认规则符合预期再放手交给它。"""
    from app.services import grading
    return grading.auto_grade(db, need_id, dry_run=True)


class GradeIn(BaseModel):
    credibility: str
    accept_suggestion: bool = False


@api.post("/sources/{source_id}/grade")
def grade_source(source_id: int, body: GradeIn, db: Session = Depends(get_session),
                 user: AppUser = Depends(require_roles("analyst"))):
    """人工定级(自动定级判不了的少数情况:主要是该不该给 S2——S2 能支撑已确认金额,属红线)。"""
    if body.credibility not in ("S1", "S2", "S3", "S4"):
        raise HTTPException(422, "可信度须为 S1-S4")
    src = db.get(Source, source_id)
    if not src:
        raise HTTPException(404, "源不存在")
    discovery_svc.promote(db, source_id, body.credibility, user.id)
    cfg = dict(src.adapter_config or {})
    cfg.pop("suggest_credibility", None)
    cfg.pop("suggest_reason", None)
    src.adapter_config = cfg
    db.add(AuditLog(user_id=user.id, action="source.grade", target=str(source_id),
                    detail={"credibility": body.credibility}))
    db.commit()
    return {"id": src.id, "name": src.name, "credibility": src.credibility,
            "lifecycle": src.lifecycle}


@api.get("/sources/duplicates")
def source_duplicates(need_id: str | None = None, db: Session = Depends(get_session),
                      _: AppUser = Depends(current_user)):
    """扫描同站多源:按站点身份(site_key)分组列出。同站不同栏目属正常(各自采集),
    仅 has_exact_duplicate=true 的是真重复(同栏目多条),建议删多余。"""
    return discovery_svc.duplicate_groups(db, need_id)


@api.post("/sources/recompute-keys")
def sources_recompute_keys(db: Session = Depends(get_session),
                           _: AppUser = Depends(require_roles("analyst"))):
    """校正所有源的站点身份键与采集目标键并自动查重合并(同采集目标的重复源并一)。"""
    try:
        res = discovery_svc.recompute_keys(db)
        db.commit()
        return res
    except Exception as e:  # noqa: BLE001 兜底成 400,避免 500 白屏
        db.rollback()
        raise HTTPException(400, f"整理源键失败:{type(e).__name__}: {e}"[:200])


@api.post("/sources/{source_id}/discover-columns")
def source_discover_columns(source_id: int, persist: bool = False,
                            db: Session = Depends(get_session),
                            _: AppUser = Depends(require_roles("analyst"))):
    """把根域源精准定位到具体栏目。

    persist=false:仅预览识别结果(校验明细含文章数/结构一致性/内容相关度);
    persist=true :把校验通过的栏目落库为子源(该站从此按栏目采,不再抓首页)。
    """
    from app.services import columns
    src = db.get(Source, source_id)
    if not src:
        raise HTTPException(404, "源不存在")
    if not columns.is_root_only(src.entry_url):
        return {"root_only": False, "note": "该源已是具体栏目/或非根域,采集时直接抓其自身",
                "columns": []}
    render_pref = (src.adapter_config or {}).get("render", "auto")
    terms = columns.relevance_terms(db, (src.serves_needs or [need_ctx.default_need_id()])[0])
    if persist:
        # 强制重算(用户点「定位栏目」就是要现在定位一次),再按记录返回
        cfg = dict(src.adapter_config or {})
        cfg.pop("columns_discovered_at", None)
        src.adapter_config = cfg
        kids, _re = columns.discover_and_persist(db, src)
        return {"root_only": True, "persisted": True, "count": len(kids), "valid": len(kids),
                "columns": [{"url": k.entry_url, "anchor": k.name, "valid": True,
                             "note": k.note, "source_id": k.id} for k in kids]}
    cands = columns.discover_columns(src)
    out = []
    for c in cands:
        v = columns.validate_column(c["url"], render_pref, terms)   # 一致性 + 内容相关度
        out.append({**c, "valid": v["valid"], "article_count": v["article_count"],
                    "consistency": v["consistency"], "relevance": v.get("relevance", 0.0),
                    "reason": v.get("reason", "")})
    valid_n = sum(1 for c in out if c["valid"])
    return {"root_only": True, "persisted": False, "count": len(out), "valid": valid_n,
            "columns": out}


@api.post("/sources/{source_id}/to-search-retry")
def source_to_search_retry(source_id: int, retire_original: bool = True,
                           db: Session = Depends(get_session),
                           _: AppUser = Depends(require_roles("analyst"))):
    """低成本兜底:把直连抓不到的页面型源改造成『站内检索』——借搜索引擎按 site:域名 抓它。

    平时不用点:体检自动停用一个页面源时会顺手做这件事(见 health.register_failure)。
    这里留作定向补救的入口。
    """
    from app.services import health
    src = db.get(Source, source_id)
    if not src:
        raise HTTPException(404, "源不存在")
    r = health.convert_to_site_search(db, src, retire_original=retire_original)
    if not r.get("ok"):
        raise HTTPException(422, r.get("note") or "该源不适合转站内检索")
    db.commit()
    return {"id": r["id"], "created": r["created"], "site": r["site"],
            "retired_original": retire_original}


class PromoteIn(BaseModel):
    credibility: str


@api.post("/sources/{source_id}/promote")
def promote_source(source_id: int, body: PromoteIn, db: Session = Depends(get_session),
                   user: AppUser = Depends(require_roles("analyst"))):
    if body.credibility not in ("S1", "S2", "S3", "S4"):
        raise HTTPException(422, "可信度须为 S1-S4")
    src = discovery_svc.promote(db, source_id, body.credibility, user.id)
    db.commit()
    return {"id": src.id, "lifecycle": src.lifecycle, "credibility": src.credibility}


@api.get("/sources/{source_id}/trial-report")
def source_trial_report(source_id: int, db: Session = Depends(get_session),
                        _: AppUser = Depends(current_user)):
    return discovery_svc.trial_report(db, source_id)


@api.post("/sources/{source_id}/test-fetch")
def test_fetch_source(source_id: int, q: str | None = None, mark: bool = False,
                      db: Session = Depends(get_session),
                      _: AppUser = Depends(require_roles("analyst"))):
    """一键试抓:实时抓该源第一页,返回发现的条目(不入库、不存档),用来判断源是否能出数据。

    页面型→discover_page(0);检索型→用一个代表关键词 search_page(0)。仅取前 20 条。
    mark=True(批量体检用):把本次成败计入源健康——成功清零 fail_streak,失败累加;连续失败
    达到 source_auto_retire_fail_streak 即自动标记停用(retired)。单源手动测试默认 mark=False,
    纯探测不改状态。
    """
    from app.services import health as health_svc
    src = db.get(Source, source_id)
    if not src:
        raise HTTPException(404, "源不存在")
    return health_svc.probe_source(db, src, q=q, mark=mark)


# ---------- 找源词表现 / 关键词进化 ----------

@api.get("/query-evolution")
def query_evolution_report(need_id: str = Depends(need_id_param), top: int = 12,
                           db: Session = Depends(get_session),
                           _: AppUser = Depends(current_user)):
    """哪些找源词在干活、哪些限定词在拖后腿、词表正在怎么长。"""
    from app.services import query_evolution
    return query_evolution.report(db, need_id, top=top)


@api.post("/query-evolution/run")
def query_evolution_run(need_id: str = Depends(need_id_param), db: Session = Depends(get_session),
                        user: AppUser = Depends(require_roles("analyst"))):
    """立刻跑一轮词表进化(平时由自动运维按周期跑)。"""
    from app.services import query_evolution
    r = query_evolution.evolve(db, need_id)
    db.add(AuditLog(user_id=user.id, action="query.evolve", target=need_id))
    db.commit()
    return r


# ---------- 候选源池 M10 ----------

@api.get("/source-candidates")
def source_candidates(min_score: float = 0, db: Session = Depends(get_session),
                      _: AppUser = Depends(current_user)):
    from app.models import SourceProbe
    threshold = float(getattr(settings, "discovery_auto_trial_threshold", 0) or 8.0)
    probe_pass = float(getattr(settings, "discovery_probe_pass", 0) or 0)
    keys = {r.identity_key for r in db.query(SourceDiscoveryEvidence).all()}
    out = []
    for key in keys:
        if db.query(Source).filter_by(site_key=key).first():
            continue                       # 已经建成源的不再列在候选池
        probe = db.get(SourceProbe, key)
        rel = float(probe.relevance) if (probe and probe.ok) else 0.0
        score = discovery_svc.candidate_score(db, key, rel)   # 含 LLM 内容相关度,不再恒为 0
        if score < min_score:
            continue
        evs = db.query(SourceDiscoveryEvidence).filter_by(identity_key=key).all()
        chans = sorted({e.channel for e in evs})
        multi = (len(chans) >= 2 or any(e.was_cluster_primary for e in evs)
                 or (probe_pass > 0 and rel >= probe_pass and "source_search" in chans))
        # 没自动入库的,必须说得出为什么——否则候选池里一堆东西不知道卡在哪
        if score < threshold and not multi:
            blocked = f"评分 {score} 未达 {threshold},且只有 {len(chans)} 个发现通道"
        elif score < threshold:
            blocked = f"评分 {score} 未达自动入库阈值 {threshold}"
        elif not multi:
            blocked = (f"只有 1 个发现通道且 LLM 初评{'未达 %s' % probe_pass if probe else '尚未进行'},"
                       "按多通道闸门暂不自动入库")
        else:
            blocked = ""
        out.append({"identity_key": key, "name": discovery_svc.candidate_name(db, key),
                    "kind": ("公众号" if key.startswith("mp:") else
                             "百家号" if key.startswith("bjh:") else
                             "微博号" if key.startswith("wb:") else "网站"),
                    "score": score, "threshold": threshold,
                    "channels": chans, "hits": sum(e.hit_count for e in evs),
                    "first_seen": min(e.first_seen for e in evs).isoformat(timespec="seconds"),
                    "last_seen": max(e.last_seen for e in evs).isoformat(timespec="seconds"),
                    "llm_relevance": rel if probe else None,
                    "llm_reason": (probe.reason if probe else None),
                    "probed": bool(probe), "blocked_reason": blocked})
    return sorted(out, key=lambda x: -x["score"])


@api.post("/source-candidates/{identity_key:path}/admit")
def admit_candidate(identity_key: str, need_id: str = Depends(need_id_param),
                    db: Session = Depends(get_session),
                    user: AppUser = Depends(require_roles("analyst"))):
    """人工"收下"一个候选:直接建成 S4 试运行源(不必等它攒够自动入库分数)。"""
    if db.query(Source).filter_by(site_key=identity_key).first():
        raise HTTPException(409, "该候选已经建成源了")
    if not db.query(SourceDiscoveryEvidence).filter_by(identity_key=identity_key).first():
        raise HTTPException(404, "候选不存在")
    src = discovery_svc.create_from_candidate(db, identity_key, need_id)
    db.add(AuditLog(user_id=user.id, action="candidate.admit", target=identity_key))
    db.commit()
    return {"id": src.id, "name": src.name, "kind": src.kind, "entry_url": src.entry_url}


class BlacklistIn(BaseModel):
    reason: str


@api.post("/source-candidates/{identity_key}/blacklist")
def blacklist_candidate(identity_key: str, body: BlacklistIn, db: Session = Depends(get_session),
                        user: AppUser = Depends(require_roles("analyst"))):
    discovery_svc.blacklist(db, identity_key, body.reason, user.id)
    db.commit()
    return {"blacklisted": identity_key}


# ---------- 文档与存档 M2/M11 ----------

@api.get("/documents")
def list_documents(need_id: str, status: str | None = None, relevant: bool = False,
                   limit: int = 100, db: Session = Depends(get_session),
                   _: AppUser = Depends(current_user)):
    q = db.query(RawDocument).filter_by(need_id=need_id)
    if relevant:  # 只看相关:粗筛入选(不含被过滤的不相干内容)
        q = q.filter(RawDocument.screen_status.in_(["screened_in", "manual_queue"]))
    elif status:
        q = q.filter_by(screen_status=status)
    return [{"id": d.id, "title": d.title, "url": d.final_url or d.url, "publisher": d.publisher,
             "screen_status": d.screen_status, "screen_score": d.screen_score,
             "screen_reason": d.screen_reason, "is_primary": d.is_primary,
             "snapshot_id": d.snapshot_id,
             "fetched_at": d.fetched_at.isoformat() if d.fetched_at else None}
            for d in q.order_by(RawDocument.id.desc()).limit(limit).all()]


class ResolveDocIn(BaseModel):
    action: str                    # discard(判为不相干) / requeue(重新处理) / attach(并入已有事件)
    event_id: str | None = None    # action=attach 时的目标事件
    note: str | None = None


@api.post("/documents/{doc_id}/resolve")
def resolve_document(doc_id: int, body: ResolveDocIn, db: Session = Depends(get_session),
                     user: AppUser = Depends(require_roles("analyst", "editor"))):
    """处理「待人工」文档,给它一个出口。

    此前 manual_queue(粗筛存疑/疑似同事件/抽取为空/处理异常)只能看不能动,是只进不出的黑洞。
    - discard:判为不相干,移出队列;
    - requeue:置回待处理,下轮重新粗筛+抽取(适合当时大模型超时的);
    - attach:确认与某已有事件为同一事件,把本文档作为该事件的补充来源挂上去。
    """
    from app.models import EventSource
    d = db.get(RawDocument, doc_id)
    if not d:
        raise HTTPException(404, "文档不存在")
    act = (body.action or "").strip()
    if act == "discard":
        d.screen_status = "screened_out"
        d.screen_reason = f"人工判定不相干:{body.note or ''}".strip()
    elif act == "requeue":
        d.screen_status = "pending"
        d.screen_reason = "人工退回重新处理"
    elif act == "attach":
        ev = db.get(Event, (body.event_id or "").strip())
        if not ev:
            raise HTTPException(422, "目标事件不存在")
        exists = db.query(EventSource).filter_by(event_id=ev.event_id, doc_id=d.id).first()
        if not exists:
            src = db.get(Source, d.source_id)
            ref = f"SRC-M{d.id}"
            db.add(EventSource(event_id=ev.event_id, ref_id=ref, doc_id=d.id,
                               snapshot_id=d.snapshot_id,
                               credibility=(src.credibility if src else "S4"),
                               supports_fields=["*"]))
            payload = dict(ev.payload or {})
            payload.setdefault("sources", [])
            payload["sources"] = list(payload["sources"]) + [{
                "ref_id": ref, "url_or_doc_number": d.final_url or d.url,
                "title": d.title or "", "publisher": d.publisher or "",
                "credibility": (src.credibility if src else "S4"),
                "snapshot_id": d.snapshot_id or "",
            }]
            ev.payload = payload
            log_change(db, ev.event_id, "sources", None, ref, user.id, source_ref=ref)
        d.screen_status = "screened_out"
        d.screen_reason = f"人工确认并入事件 {ev.event_id}"
    else:
        raise HTTPException(422, "action 须为 discard / requeue / attach")
    db.commit()
    return {"id": d.id, "screen_status": d.screen_status, "reason": d.screen_reason}


@api.get("/documents/{doc_id}")
def get_document(doc_id: int, db: Session = Depends(get_session), _: AppUser = Depends(current_user)):
    """文档详情:抓回的正文全文 + 元数据(点标题查看原文内容)。"""
    d = db.get(RawDocument, doc_id)
    if not d:
        raise HTTPException(404, "文档不存在")
    return {"id": d.id, "title": d.title, "url": d.url, "final_url": d.final_url,
            "publisher": d.publisher, "screen_status": d.screen_status,
            "screen_score": d.screen_score, "screen_reason": d.screen_reason,
            "is_primary": d.is_primary, "snapshot_id": d.snapshot_id,
            "http_status": d.http_status,
            "published_at": d.published_at.isoformat() if d.published_at else None,
            "fetched_at": d.fetched_at.isoformat() if d.fetched_at else None,
            "content_text": d.content_text or ""}


@api.get("/archives/{snapshot_id}")
def get_archive(snapshot_id: str, db: Session = Depends(get_session),
                _: AppUser = Depends(current_user)):
    rec = db.get(ArchiveManifest, snapshot_id)
    if not rec:
        raise HTTPException(404, "快照不存在")
    return {"snapshot_id": rec.snapshot_id, "status": rec.status, "final_url": rec.final_url,
            "captured_at": rec.captured_at.isoformat(), "storage_path": rec.storage_path,
            "image_count": rec.image_count, "attachment_count": rec.attachment_count,
            "screenshot_pages": rec.screenshot_pages, "manifest_sha256": rec.manifest_sha256}


# ---------- 事件 M4 ----------

@api.get("/events")
def list_events(need_id: str, status: str | None = None, industry: str | None = None,
                province: str | None = None, severity: str | None = None,
                record_type: str | None = None,
                dim1: str | None = None, dim2: str | None = None, region: str | None = None,
                grade: str | None = None, subject: str | None = None,
                limit: int = Query(50, le=500), db: Session = Depends(get_session),
                _: AppUser = Depends(current_user)):
    """记录列表。筛选既接受角色名(dim1/grade/region/subject,任何需求通用),也兼容旧参数
    (industry/severity/province)。返回同时带角色键与旧键。"""
    from app.services.need_ctx import ROLE_COLUMNS
    q = db.query(Event).filter_by(need_id=need_id)
    if status == "live":   # 已发布口径 = 已发布 + 跟踪中 + 已关闭(与 KPI 一致)
        q = q.filter(Event.status.in_(["published", "monitoring", "closed"]))
    elif status:
        q = q.filter_by(status=status)
    for role, val in (("dim1", dim1 or industry), ("dim2", dim2), ("region", region or province),
                      ("grade", grade or severity), ("subject", subject), ("record_type", record_type)):
        if val:
            q = q.filter(getattr(Event, ROLE_COLUMNS[role]) == val)
    rows = q.order_by(Event.event_id.desc()).limit(limit).all()
    out = []
    for e in rows:
        row = {"event_id": e.event_id, "title": (e.payload or {}).get("title"), "status": e.status,
               "record_type": e.record_type, "completeness": e.completeness_score,
               "occurred_date": str(e.occurred_date or ""), "disclosed_date": str(e.disclosed_date or "")}
        for role, col in ROLE_COLUMNS.items():
            if role not in ("occurred_date", "disclosed_date", "record_type"):
                row[role] = getattr(e, col, None)
        out.append(row)
    return out


@api.get("/events/{event_id}")
def get_event(event_id: str, db: Session = Depends(get_session), _: AppUser = Depends(current_user)):
    ev = db.get(Event, event_id)
    if not ev:
        raise HTTPException(404, "事件不存在")
    return {"event_id": ev.event_id, "need_id": ev.need_id, "status": ev.status,
            "payload": ev.payload, "completeness": ev.completeness_score}


class PayloadIn(BaseModel):
    payload: dict
    source_ref: str | None = None


@api.put("/events/{event_id}")
def put_event(event_id: str, body: PayloadIn, db: Session = Depends(get_session),
              user: AppUser = Depends(require_roles("editor", "reviewer", "analyst"))):
    ev = db.get(Event, event_id)
    if not ev:
        raise HTTPException(404, "事件不存在")
    from app.services.money_guard import apply_guard
    guard = apply_guard(dict(body.payload), ctx=need_ctx.get(db, ev.need_id))
    update_payload(db, ev, guard.payload, by_user=user.id, source_ref=body.source_ref)
    db.commit()
    return {"event_id": event_id, "guard_violations": guard.violations}


@api.get("/events/{event_id}/relations")
def event_relations(event_id: str, db: Session = Depends(get_session), _: AppUser = Depends(current_user)):
    """记录的上下游关系(废止/替代/修订/依据)。"""
    from app.services import relations
    return relations.for_event(db, event_id)


@api.get("/kpi/scorecard")
def kpi_scorecard(need_id: str = Depends(need_id_param), days: int | None = None,
                  db: Session = Depends(get_session), _: AppUser = Depends(current_user)):
    return kpi_svc.quality_scorecard(db, need_id, days)


@api.get("/events/{event_id}/changelog")
def event_changelog(event_id: str, db: Session = Depends(get_session),
                    _: AppUser = Depends(current_user)):
    return [{"at": c.at.isoformat(), "field": c.field, "old": c.old_value, "new": c.new_value,
             "source_ref": c.source_ref}
            for c in db.query(EventChangeLog).filter_by(event_id=event_id)
            .order_by(EventChangeLog.at.desc()).limit(200).all()]


# ---------- 复核 M5 ----------

@api.get("/review/queue")
def review_queue(stage: str = "pending", db: Session = Depends(get_session),
                 _: AppUser = Depends(current_user)):
    q = db.query(ReviewTask)
    if stage == "pending":   # 全部待复核(新抽取+待一审+待二审),与仪表盘"待复核"口径一致
        q = q.filter(ReviewTask.stage.in_(["extracted", "first_review", "second_review"]))
    else:
        q = q.filter_by(stage=stage)
    rows = q.order_by(ReviewTask.updated_at).all()
    return [{"task_id": t.id, "event_id": t.event_id, "stage": t.stage,
             "needs_double": t.needs_double} for t in rows]


@api.post("/review/{event_id}/submit")
def review_submit(event_id: str, db: Session = Depends(get_session),
                  user: AppUser = Depends(require_roles("editor", "reviewer"))):
    t = review_svc.submit_for_review(db, event_id, user.id)
    db.commit()
    return {"event_id": event_id, "stage": t.stage}


@api.post("/review/{event_id}/approve")
def review_approve(event_id: str, db: Session = Depends(get_session),
                   user: AppUser = Depends(require_roles("reviewer"))):
    ev = db.get(Event, event_id)
    if not ev:
        raise HTTPException(404, "事件不存在")
    try:
        t = review_svc.approve(db, event_id, user.id, _record_schema(db, ev.need_id),
                               _confirm_allowed(db, ev.need_id))
    except (PublishError, review_svc.ReviewError) as e:
        db.rollback()
        raise HTTPException(422, str(e)) from e
    if t.stage == "published":
        followup_svc.schedule_followups(db, ev)
        leads_svc.generate_leads(db, ev)
    db.commit()
    return {"event_id": event_id, "stage": t.stage}


class RejectIn(BaseModel):
    reason: str


@api.post("/review/{event_id}/reject")
def review_reject(event_id: str, body: RejectIn, db: Session = Depends(get_session),
                  user: AppUser = Depends(require_roles("reviewer"))):
    t = review_svc.reject(db, event_id, user.id, body.reason)
    db.commit()
    return {"event_id": event_id, "stage": t.stage}


# ---------- 回访 M6 ----------

@api.get("/followups")
def followups(due: str | None = None, db: Session = Depends(get_session),
              _: AppUser = Depends(current_user)):
    on = date.fromisoformat(due) if due else date.today()
    return [{"id": t.id, "event_id": t.event_id, "kind": t.kind, "due": str(t.due_date),
             "reason": t.reason} for t in followup_svc.due_tasks(db, on)]


@api.get("/followups/{task_id}/search-pack")
def followup_pack(task_id: int, db: Session = Depends(get_session), _: AppUser = Depends(current_user)):
    t = db.get(FollowupTask, task_id)
    if not t:
        raise HTTPException(404)
    return t.search_pack or {}


class CompleteIn(BaseModel):
    findings: str = ""


@api.post("/followups/{task_id}/complete")
def followup_complete(task_id: int, body: CompleteIn, db: Session = Depends(get_session),
                      user: AppUser = Depends(require_roles("editor", "reviewer", "analyst"))):
    t = followup_svc.complete_task(db, task_id, user.id, body.findings)
    db.commit()
    return {"id": t.id, "status": t.status}


# ---------- 监控名单 B5 ----------

class WatchIn(BaseModel):
    need_id: str
    kind: str
    value: str
    aliases: list[str] = []
    reason: str | None = None
    tier: str = "B"


@api.post("/watch-targets")
def add_watch(body: WatchIn, db: Session = Depends(get_session),
              user: AppUser = Depends(require_roles("analyst"))):
    wt = WatchTarget(need_id=_nid(body.need_id), kind=body.kind, value=body.value,
                     aliases=body.aliases, reason=body.reason, tier=body.tier)
    db.add(wt)
    db.commit()
    return {"id": wt.id}


@api.get("/watch-targets")
def list_watch(need_id: str, db: Session = Depends(get_session), _: AppUser = Depends(current_user)):
    return [{"id": w.id, "kind": w.kind, "value": w.value, "tier": w.tier, "active": w.active}
            for w in db.query(WatchTarget).filter_by(need_id=need_id, active=True).all()]


# ---------- 线索 M8 ----------

@api.get("/leads")
def list_leads(need_id: str, status: str | None = None, min_score: float = 0,
               db: Session = Depends(get_session), _: AppUser = Depends(current_user)):
    q = db.query(Lead).filter(Lead.need_id == need_id, Lead.score >= min_score)
    if status:
        q = q.filter_by(status=status)
    return [{"id": l.id, "event_id": l.event_id, "target_org": l.target_org,
             "score": l.score, "window_stage": l.window_stage, "products": l.products,
             "talk_track": l.talk_track, "status": l.status}
            for l in q.order_by(Lead.score.desc()).all()]


# ---------- 报表与 KPI M7/M8 ----------

@api.get("/reports/heatmap")
def report_heatmap(need_id: str, days: int = 365, db: Session = Depends(get_session),
                   _: AppUser = Depends(current_user)):
    return kpi_svc.heatmap(db, need_id, days)


@api.get("/reports/loss")
def report_loss(need_id: str, scope: str | None = None, db: Session = Depends(get_session),
                _: AppUser = Depends(current_user)):
    trace = kpi_svc.traceability_check(db, need_id)
    if not trace["ok"]:
        raise HTTPException(409, f"口径校验失败,拒绝出数: {trace['violations']}")
    return kpi_svc.amount_stats(db, need_id, scope)


@api.get("/reports/controls")
def report_controls(need_id: str, db: Session = Depends(get_session), _: AppUser = Depends(current_user)):
    return kpi_svc.status_count(db, need_id)


@api.get("/reports/whitespace")
def report_whitespace(need_id: str, db: Session = Depends(get_session), _: AppUser = Depends(current_user)):
    return kpi_svc.missing_field(db, need_id)


@api.get("/kpi/dashboard")
def kpi_dashboard(need_id: str, db: Session = Depends(get_session), _: AppUser = Depends(current_user)):
    return kpi_svc.dashboard(db, need_id)


# ---------- 需求画像(框架层) ----------

@api.get("/needs")
def list_needs(db: Session = Depends(get_session), _: AppUser = Depends(current_user)):
    """已注册的需求(画像)。default=平台默认需求;未注册但 config/need_*.yaml 里有的画像也列出(registered=false)。"""
    default = need_ctx.default_need_id()
    out, seen = [], set()
    for n in db.query(NeedProfile).all():
        c = need_ctx.for_need(n)
        out.append({"id": n.id, "name": n.name, "active": n.active, "registered": True,
                    "default": n.id == default, "archetype": c.archetype,
                    "record_label": c.ui.get("record_label")})
        seen.add(n.id)
    for f in need_ctx.profile_files():
        cfg = need_ctx.load_profile_config_file(f)
        nid = ((cfg or {}).get("need") or {}).get("id")
        if nid and nid not in seen:
            out.append({"id": nid, "name": (cfg["need"].get("name") or nid), "active": False,
                        "registered": False, "default": nid == default, "file": str(f.name)})
    return out


@api.get("/needs/{need_id}/ui")
def need_ui(need_id: str, db: Session = Depends(get_session), _: AppUser = Depends(current_user)):
    """界面定义:页签/列/筛选/详情分区/仪表盘卡片/角色标签——前端按它渲染,不认行业字段名。"""
    return need_ctx.get(db, need_id).to_ui()


@api.post("/needs/{need_id}/setup")
def need_setup(need_id: str, db: Session = Depends(get_session),
               user: AppUser = Depends(require_roles("admin"))):
    """按画像文件把一个需求装起来(注册 + 词表 + 关键词 + 种子源,幂等)。"""
    from app.services import profiles
    try:
        r = profiles.setup_need(db, need_id)
    except profiles.ProfileError as e:
        raise HTTPException(400, str(e))
    db.add(AuditLog(user_id=user.id, action="need.setup", target=need_id, detail=r))
    db.commit()
    return r


@api.get("/audit-logs")
def audit_logs(limit: int = 100, db: Session = Depends(get_session),
               _: AppUser = Depends(require_roles("analyst"))):
    return [{"at": a.at.isoformat(), "user_id": a.user_id, "action": a.action, "target": a.target}
            for a in db.query(AuditLog).order_by(AuditLog.at.desc()).limit(limit).all()]


# ---------- 系统配置(前端「设置」页) ----------

@api.get("/settings")
def get_settings(_: AppUser = Depends(require_roles("analyst"))):
    from app.services import settings_service
    return settings_service.current()


@api.put("/settings")
def put_settings(body: dict, db: Session = Depends(get_session),
                 user: AppUser = Depends(require_roles("admin"))):
    from app.services import settings_service
    applied = settings_service.save(db, body)
    db.add(AuditLog(user_id=user.id, action="settings.update", target="app_setting",
                    detail={"keys": applied}))
    db.commit()
    resp = {"ok": True, "applied": applied,
            "note": "已保存并即时生效(LLM/采集/去重/存档参数);数据库、密钥等结构性配置仍走 .env,需重启。"}
    # 若本次动到了 LLM 配置,顺带回连通测试结果
    if any(k.startswith("llm_") for k in applied):
        resp["llm_test"] = settings_service.test_llm()
    return resp


@api.post("/settings/test-llm")
def test_llm_endpoint(_: AppUser = Depends(require_roles("analyst"))):
    """用当前已生效配置实测大模型连通(聊天+向量),供设置页「测试连通」按钮调用。"""
    from app.services import settings_service
    return settings_service.test_llm()


# ---------- 关键词矩阵(决定搜什么、搜多少) ----------

@api.get("/keywords")
def get_keywords(need_id: str = Depends(need_id_param), db: Session = Depends(get_session),
                 _: AppUser = Depends(current_user)):
    """当前生效的关键词矩阵内容 + 展开后的实际查询条数预览。"""
    from app.models import KeywordSet
    from app.services.scheduler import expand_queries
    ks = db.query(KeywordSet).filter_by(need_id=need_id, is_active=True).first()
    content = ks.content if ks else {}
    expanded = expand_queries(content) if content else []
    return {"version": ks.version if ks else None, "content": content,
            "expanded_count": len(expanded), "sample": expanded[:20]}


class KeywordsIn(BaseModel):
    need_id: str | None = None
    content: dict


@api.post("/keywords/generate")
def generate_keywords(need_id: str = Depends(need_id_param), expand: bool | None = None, persist: bool = True,
                      db: Session = Depends(get_session),
                      user: AppUser = Depends(require_roles("admin", "analyst"))):
    """按画像的范围限定(scope)+ 静态词组 + 监控名单自动生成关键词矩阵(可选模型扩展同义词),并设为生效版本。"""
    from app.services import capabilities
    r = capabilities.run("keywords.generate", db, need_id, expand=expand, persist=persist)
    if persist:
        db.add(AuditLog(user_id=user.id, action="keywords.generate", target=need_id, detail={"version": r["version"]}))
        db.commit()
    return r


# ---------- 能力注册表:每个底层能力都能独立调用 ----------

@api.get("/capabilities")
def capabilities_list(_: AppUser = Depends(current_user)):
    from app.services import capabilities
    return capabilities.list_capabilities()


class CapRunIn(BaseModel):
    need_id: str | None = None
    params: dict = {}


@api.post("/capabilities/{name}/run")
def capability_run(name: str, body: CapRunIn, db: Session = Depends(get_session),
                   _: AppUser = Depends(require_roles("admin", "analyst"))):
    """独立调用一个能力(调试一段正文的粗筛/抽取、只生成关键词、只跑一轮找源……)。"""
    from app.services import capabilities
    try:
        out = capabilities.run(name, db, _nid(body.need_id), **(body.params or {}))
    except KeyError as e:
        raise HTTPException(404, str(e))
    except TypeError as e:
        raise HTTPException(400, f"参数不匹配:{e}")
    db.commit()
    return {"capability": name, "result": out}


@api.put("/keywords")
def put_keywords(body: KeywordsIn, db: Session = Depends(get_session),
                 user: AppUser = Depends(require_roles("admin", "analyst"))):
    """保存关键词矩阵为新版本并激活;返回展开后实际查询条数。"""
    from app.models import KeywordSet
    from app.services.scheduler import expand_queries
    content = dict(body.content or {})
    # 版本号自增(基于已有最大数字版本)
    existing = db.query(KeywordSet).filter_by(need_id=_nid(body.need_id)).all()
    nums = [float(k.version) for k in existing if str(k.version).replace(".", "").isdigit()]
    content["version"] = str(round((max(nums) if nums else 0) + 0.1, 1))
    db.query(KeywordSet).filter_by(need_id=_nid(body.need_id)).update({"is_active": False})
    ks = KeywordSet(need_id=_nid(body.need_id), version=content["version"], content=content, is_active=True)
    from datetime import datetime
    ks.published_at = datetime.utcnow()
    db.add(ks)
    db.add(AuditLog(user_id=user.id, action="keywords.update", target=_nid(body.need_id),
                    detail={"version": content["version"]}))
    db.commit()
    from app.services import columns as columns_svc
    columns_svc.reset_terms_cache()   # 栏目相关度用的是这份词表,改完要立即生效
    expanded = expand_queries(content)
    return {"ok": True, "version": content["version"], "expanded_count": len(expanded),
            "sample": expanded[:20]}


# ---------- 采集触发与运行记录(前端"采集"页) ----------

class CrawlIn(BaseModel):
    need_id: str | None = None
    limit_sources: int = 3
    do_archive: bool = True


def _job_dict(job):
    if not job:
        return None
    return {
        "id": job.id, "status": job.status, "phase": job.phase,
        "total_sources": job.total_sources, "done_sources": job.done_sources,
        "total_docs": job.total_docs, "done_docs": job.done_docs,
        "new_docs": job.new_docs, "kept_docs": job.kept_docs,
        "dropped_docs": job.dropped_docs, "new_events": job.new_events,
        "error": job.error, "limit_sources": job.limit_sources,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }


@api.post("/crawl/run")
def crawl_run_now(body: CrawlIn, db: Session = Depends(get_session),
                  user: AppUser = Depends(require_roles("analyst", "editor"))):
    """后台启动一轮采集(不阻塞),返回任务 id;已有运行中任务则返回它。"""
    from app.services import crawl_runner
    running = crawl_runner.has_running(db, _nid(body.need_id))
    if running:
        return {"job_id": running.id, "already_running": True, "job": _job_dict(running)}
    jid = crawl_runner.start_job(_nid(body.need_id), body.limit_sources, user.id)
    return {"job_id": jid, "already_running": False}


@api.get("/crawl/current")
def crawl_current(need_id: str = Depends(need_id_param), db: Session = Depends(get_session),
                  _: AppUser = Depends(current_user)):
    """当前/最近一次采集任务的状态与进度(任何页面/刷新都能查到是否在运行)。"""
    from app.services import crawl_runner
    return {"job": _job_dict(crawl_runner.current_job(db, need_id)),
            "inflight": crawl_runner.inflight()}   # 当前在抓哪些源、各已耗时,便于定位卡住的源


@api.get("/crawl/jobs/{job_id}/logs")
def crawl_job_logs(job_id: int, level: str | None = None, limit: int = 300,
                   db: Session = Depends(get_session), _: AppUser = Depends(current_user)):
    """采集详细日志(可按 level 过滤:info/warn/error),用于排查故障。"""
    from app.models import CrawlLog
    q = db.query(CrawlLog).filter_by(job_id=job_id)
    if level:
        q = q.filter_by(level=level)
    rows = q.order_by(CrawlLog.id.desc()).limit(limit).all()
    return [{"at": r.at.isoformat(), "level": r.level, "source": r.source, "message": r.message}
            for r in rows]


@api.post("/crawl/jobs/{job_id}/cancel")
def crawl_job_cancel(job_id: int, _: AppUser = Depends(require_roles("analyst", "editor"))):
    from app.services import crawl_runner
    crawl_runner.cancel(job_id)
    return {"ok": True, "note": "已请求取消,当前步骤完成后停止"}


@api.get("/crawl/jobs/{job_id}/diagnostics")
def crawl_job_diagnostics(job_id: int, db: Session = Depends(get_session),
                          _: AppUser = Depends(require_roles("analyst"))):
    """整包导出一次采集的端到端诊断:任务元信息 + 全部日志 + 每步留痕(LLM 提示词/返回、
    粗筛/抽取/去重/建草稿的输入输出)。前端『下载诊断日志』用,下载回来即可离线分析。"""
    from fastapi.responses import JSONResponse

    from app.models import CrawlJob, CrawlLog, RunTrace
    job = db.get(CrawlJob, job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    logs = db.query(CrawlLog).filter_by(job_id=job_id).order_by(CrawlLog.id).all()
    traces = db.query(RunTrace).filter_by(job_id=job_id).order_by(RunTrace.id).all()
    bundle = {
        "job": {"id": job.id, "need_id": job.need_id, "status": job.status,
                "phase": job.phase, "limit_sources": job.limit_sources,
                "total_sources": job.total_sources, "new_docs": job.new_docs,
                "kept_docs": job.kept_docs, "dropped_docs": job.dropped_docs,
                "new_events": job.new_events, "error": job.error,
                "started_at": job.started_at.isoformat() if job.started_at else None,
                "finished_at": job.finished_at.isoformat() if job.finished_at else None},
        "logs": [{"at": r.at.isoformat(), "level": r.level, "source": r.source,
                  "message": r.message} for r in logs],
        "traces": [{"at": r.at.isoformat(), "kind": r.kind, "ref": r.ref,
                    "summary": r.summary, "detail": r.detail} for r in traces],
        "counts": {"logs": len(logs), "traces": len(traces)},
    }
    # 文件名带日期时间做版本区分(取任务结束时间,无则开始时间)
    stamp = (job.finished_at or job.started_at)
    ver = stamp.strftime("%Y%m%d-%H%M%S") if stamp else "unknown"
    fname = f"diagnostics-job{job_id}-{ver}.json"
    return JSONResponse(bundle, headers={
        "Content-Disposition": f'attachment; filename="{fname}"'})


# ---------- 每日简报 M-digest ----------

@api.get("/digest")
def get_digest(need_id: str = Depends(need_id_param), day: str | None = None,
               db: Session = Depends(get_session), _: AppUser = Depends(current_user)):
    """取某天日报(默认最新)。返回结构化内容 + Markdown。"""
    from datetime import date as _date

    from app.models import DailyDigest
    from app.services import digest as digest_svc
    if day:
        try:
            d = _date.fromisoformat(day)
        except ValueError:
            raise HTTPException(422, "day 格式应为 YYYY-MM-DD")
        row = db.query(DailyDigest).filter_by(need_id=need_id, day=d).one_or_none()
    else:
        row = digest_svc.latest(db, need_id)
    if not row:
        return {"exists": False, "note": "暂无日报,采集一轮后自动生成,或点『生成今日日报』"}
    return {"exists": True, "day": row.day.isoformat(), "content": row.content,
            "markdown": row.markdown, "delivered": row.delivered}


@api.get("/digests")
def list_digests(need_id: str = Depends(need_id_param), limit: int = 30,
                 db: Session = Depends(get_session), _: AppUser = Depends(current_user)):
    from app.models import DailyDigest
    rows = (db.query(DailyDigest).filter_by(need_id=need_id)
            .order_by(DailyDigest.day.desc()).limit(limit).all())
    return [{"day": r.day.isoformat(), "events": r.content.get("events_total", 0),
             "leads": r.content.get("leads_total", 0), "delivered": r.delivered} for r in rows]


@api.post("/digest/run")
def run_digest(need_id: str = Depends(need_id_param), push: bool = False,
               db: Session = Depends(get_session), _: AppUser = Depends(require_roles("analyst"))):
    """按需生成今日日报(不必等采集)。push=True 时尝试邮件推送。"""
    from app.services import digest as digest_svc
    if push:
        d = digest_svc.generate_today(db, need_id)
        from app.services.notify import send as notify_send
        title = (d.content or {}).get("title") or "日报"
        res = notify_send(f"{title} {d.day}", d.markdown or "", ctx=need_ctx.get(db, need_id))
        ok = any(v.get("ok") for v in res.values())
        msg = ";".join(f"{k}:{v['note']}" for k, v in res.items()) or "没有配置任何通知渠道"
        return {"day": d.day.isoformat(), "pushed": ok, "push_detail": msg,
                "events": d.content.get("events_total", 0)}
    d = digest_svc.upsert(db, need_id, __import__("datetime").datetime.utcnow().date())
    db.commit()
    return {"day": d.day.isoformat(), "pushed": False,
            "events": d.content.get("events_total", 0)}


@api.get("/digest/download")
def download_digest(need_id: str = Depends(need_id_param), day: str | None = None,
                    db: Session = Depends(get_session), _: AppUser = Depends(current_user)):
    """下载日报 Markdown。"""
    from datetime import date as _date

    from fastapi.responses import PlainTextResponse

    from app.models import DailyDigest
    from app.services import digest as digest_svc
    if day:
        row = db.query(DailyDigest).filter_by(need_id=need_id, day=_date.fromisoformat(day)).one_or_none()
    else:
        row = digest_svc.latest(db, need_id)
    if not row:
        raise HTTPException(404, "暂无日报")
    return PlainTextResponse(row.markdown or "", headers={
        "Content-Disposition": f'attachment; filename="digest-{row.day.isoformat()}.md"'})


@api.get("/crawl/runs")
def crawl_runs(limit: int = 30, db: Session = Depends(get_session),
               _: AppUser = Depends(current_user)):
    """按源的抓取执行记录 + 错误报告(失败源、原因)。"""
    from app.models import CrawlRun
    rows = db.query(CrawlRun).order_by(CrawlRun.id.desc()).limit(limit).all()
    out = []
    for r in rows:
        src = db.get(Source, r.source_id)
        out.append({"id": r.id, "source": src.name if src else r.source_id,
                    "status": r.status, "found": r.urls_found, "new": r.urls_new,
                    "skipped": r.urls_skipped, "failed": r.urls_failed, "error": r.error,
                    "started_at": r.started_at.isoformat() if r.started_at else None})
    return out


# ---------- 演示数据(前端"一键载入演示",空库也能看到界面效果) ----------

@api.post("/demo/seed")
def demo_seed(need_id: str = Depends(need_id_param), db: Session = Depends(get_session),
              user: AppUser = Depends(require_roles("analyst", "editor"))):
    """注入 3 条样例事件(已发布/待复核各态),便于快速体验界面。仅演示用。"""
    from datetime import datetime
    from app.models import Event, RawDocument
    from app.services import dedup
    from app.services.followup import schedule_followups
    from app.services.leads import generate_leads
    from app.services.pipeline import process_document
    from app.services.review import approve

    need = db.get(NeedProfile, need_id)
    if need is None:
        raise HTTPException(404, f"需求 {need_id} 未注册(先在需求页装载画像)")
    # 演示文档挂到服务本需求的源上;没有就用任意一个
    src = next((s for s in db.query(Source).all() if need_id in (s.serves_needs or [])), None) \
        or db.query(Source).first()
    # 幂等:已注入过演示数据则不再重复,避免"采集文档"数字反复累加(按需求分别判断)
    existed = db.query(RawDocument).filter(
        RawDocument.need_id == need_id,
        RawDocument.url.like(f"https://demo.local/{need_id}/%")).count()
    if existed:
        return {"created": [], "published": [],
                "note": f"演示数据已存在({existed} 条),未重复注入。"}

    ctx = need_ctx.for_need(need) if need else need_ctx.get(db, need_id)
    samples = [(str(x.get("title") or ""), str(x.get("text") or "")) for x in ctx.demo_samples if x.get("text")]
    if not samples:
        return {"created": [], "published": [], "note": "该需求画像未声明 demo.samples,没有可注入的演示样例"}
    if src is None:
        raise HTTPException(400, "还没有任何数据源,先载入种子源")
    created, published = [], []
    for i, (title, text) in enumerate(samples):
        url = f"https://demo.local/{need_id}/seed-{datetime.utcnow():%Y%m%d%H%M%S%f}-{i}"
        doc = RawDocument(need_id=need_id, source_id=src.id, url=url, url_normalized=url,
                          final_url=url, title=title, publisher=src.name,
                          published_at=datetime.utcnow(), content_text=text, screen_status="pending")
        db.add(doc)
        db.flush()
        dedup.assign_cluster(db, doc)
        result = process_document(db, need, doc)
        if result.get("event_id"):
            created.append(result["event_id"])
            if i == 0:  # 第一条走完复核发布,展示已发布态+回访+线索
                ev = db.get(Event, result["event_id"])
                try:
                    approve(db, ev.event_id, user.id, _record_schema(db, need_id),
                            _confirm_allowed(db, need_id))
                    schedule_followups(db, ev)
                    generate_leads(db, ev)
                    published.append(ev.event_id)
                except Exception:  # noqa: BLE001 演示容错
                    pass
    db.commit()
    return {"created": created, "published": published,
            "note": f"已注入演示{ctx.ui.get('record_label') or '记录'};第1条已走完复核发布并生成回访与线索"}
