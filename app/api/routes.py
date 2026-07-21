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
from app.services.events import PublishError, update_payload
from app.services.extraction import load_record_schema
from app.services.profiles import get_active_profile

api = APIRouter(prefix="/api/v1")


def _record_schema(db: Session, need_id: str) -> dict:
    cfg = get_active_profile(db, need_id).config
    schema_file = (cfg.get("record_schemas") or [{}])[0].get("file") or str(settings.schema_dir / "event.schema.json")
    return load_record_schema(schema_file)


def _confirm_allowed(db: Session, need_id: str) -> list[str]:
    cfg = get_active_profile(db, need_id).config
    return ((cfg.get("sources") or {}).get("credibility_levels") or {}).get("confirm_allowed") or ["S1", "S2"]


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
    q = db.query(Source)
    if lifecycle:
        q = q.filter_by(lifecycle=lifecycle)
    return [{"id": s.id, "name": s.name, "kind": s.kind, "adapter": s.adapter,
             "entry_url": s.entry_url, "note": s.note,
             "credibility": s.credibility, "tier": s.tier, "lifecycle": s.lifecycle,
             "identity_key": s.identity_key, "discovery_score": s.discovery_score,
             "manual_assist": s.manual_assist, "docs_total": s.stat_docs_total,
             "fail_streak": s.fail_streak, "discovered_from": s.discovered_from,
             "last_crawled": s.last_success_at.isoformat() if s.last_success_at else None}
            for s in q.order_by(Source.id).all()]


class SourceIn(BaseModel):
    name: str
    entry_url: str | None = None
    kind: str = "page"                 # page(栏目/RSS 抓取) / query(关键词检索)
    adapter: str | None = None         # 留空自动:page→generic_rss/list,query→baidu_search
    credibility: str = "S3"
    tier: str = "B"
    note: str | None = None
    need_id: str = "sec_events"


@api.post("/sources", status_code=201)
def create_source(body: SourceIn, db: Session = Depends(get_session),
                  _: AppUser = Depends(require_roles("analyst"))):
    """手动添加数据源。零适配器:留空 adapter 时按类型自动选通用适配器(RSS/列表/搜索)。"""
    kind = body.kind if body.kind in ("page", "query") else "page"
    if body.credibility not in ("S1", "S2", "S3", "S4"):
        raise HTTPException(422, "可信度须为 S1-S4")
    entry = (body.entry_url or "").strip() or None
    if kind == "page" and not entry:
        raise HTTPException(422, "页面型源必须填入口链接(栏目页或 RSS 地址)")
    adapter = (body.adapter or "").strip() or ("baidu_search" if kind == "query" else "generic_rss")
    ident = None
    if entry:
        try:
            ident = url_tools.identity_key_for(entry)
        except Exception:  # noqa: BLE001
            ident = None
    # identity_key 唯一:已存在同域源则合并需求而非重复建
    if ident and (dup := db.query(Source).filter_by(identity_key=ident).one_or_none()):
        needs = sorted(set(dup.serves_needs or []) | {body.need_id})
        dup.serves_needs = needs
        if dup.lifecycle == "retired":
            dup.lifecycle = "active"
        db.commit()
        return {"id": dup.id, "merged": True, "name": dup.name}
    src = Source(name=body.name.strip(), entry_url=entry, kind=kind, adapter=adapter,
                 adapter_config={}, credibility=body.credibility, tier=body.tier,
                 lifecycle="active", serves_needs=[body.need_id],
                 identity_key=ident, manual_assist=False, note=body.note,
                 discovered_from="manual")
    db.add(src)
    db.commit()
    return {"id": src.id, "merged": False, "name": src.name}


@api.delete("/sources/{source_id}")
def delete_source(source_id: int, db: Session = Depends(get_session),
                  _: AppUser = Depends(require_roles("analyst"))):
    """删除数据源:已采过文档的源转『停用』(保留历史与外键完整);无文档的源直接物理删除。"""
    src = db.get(Source, source_id)
    if not src:
        raise HTTPException(404, "源不存在")
    has_docs = db.query(RawDocument.id).filter_by(source_id=source_id).first() is not None
    if has_docs:
        src.lifecycle = "retired"
        db.commit()
        return {"id": source_id, "action": "retired",
                "note": "该源已有采集文档,转为停用(不再采集,历史保留)"}
    db.delete(src)
    db.commit()
    return {"id": source_id, "action": "deleted"}


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


# ---------- 候选源池 M10 ----------

@api.get("/source-candidates")
def source_candidates(min_score: float = 0, db: Session = Depends(get_session),
                      _: AppUser = Depends(current_user)):
    keys = {r.identity_key for r in db.query(SourceDiscoveryEvidence).all()}
    out = []
    for key in keys:
        score = discovery_svc.candidate_score(db, key)
        if score >= min_score:
            evs = db.query(SourceDiscoveryEvidence).filter_by(identity_key=key).all()
            out.append({"identity_key": key, "score": score,
                        "channels": sorted({e.channel for e in evs}),
                        "hits": sum(e.hit_count for e in evs)})
    return sorted(out, key=lambda x: -x["score"])


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
                limit: int = Query(50, le=500), db: Session = Depends(get_session),
                _: AppUser = Depends(current_user)):
    q = db.query(Event).filter_by(need_id=need_id)
    if status == "live":   # 已发布口径 = 已发布 + 跟踪中 + 已关闭(与 KPI 一致)
        q = q.filter(Event.status.in_(["published", "monitoring", "closed"]))
    elif status:
        q = q.filter_by(status=status)
    if industry:
        q = q.filter_by(industry_l1=industry)
    if province:
        q = q.filter_by(province=province)
    if severity:
        q = q.filter_by(severity=severity)
    return [{"event_id": e.event_id, "title": (e.payload or {}).get("title"),
             "status": e.status, "industry": e.industry_l1, "province": e.province,
             "severity": e.severity, "attack_types": e.attack_types,
             "completeness": e.completeness_score, "disclosed_date": str(e.disclosed_date or "")}
            for e in q.order_by(Event.event_id.desc()).limit(limit).all()]


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
    guard = apply_guard(dict(body.payload))
    update_payload(db, ev, guard.payload, by_user=user.id, source_ref=body.source_ref)
    db.commit()
    return {"event_id": event_id, "guard_violations": guard.violations}


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
    wt = WatchTarget(need_id=body.need_id, kind=body.kind, value=body.value,
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
def report_loss(need_id: str, scope: str = "confirmed", db: Session = Depends(get_session),
                _: AppUser = Depends(current_user)):
    trace = kpi_svc.traceability_check(db, need_id)
    if not trace["ok"]:
        raise HTTPException(409, f"口径校验失败,拒绝出数: {trace['violations']}")
    return kpi_svc.loss_stats(db, need_id, scope)


@api.get("/reports/controls")
def report_controls(need_id: str, db: Session = Depends(get_session), _: AppUser = Depends(current_user)):
    return kpi_svc.controls_stats(db, need_id)


@api.get("/reports/whitespace")
def report_whitespace(need_id: str, db: Session = Depends(get_session), _: AppUser = Depends(current_user)):
    return kpi_svc.whitespace(db, need_id)


@api.get("/kpi/dashboard")
def kpi_dashboard(need_id: str, db: Session = Depends(get_session), _: AppUser = Depends(current_user)):
    return kpi_svc.dashboard(db, need_id)


# ---------- 需求画像(框架层) ----------

@api.get("/needs")
def list_needs(db: Session = Depends(get_session), _: AppUser = Depends(current_user)):
    return [{"id": n.id, "name": n.name, "active": n.active} for n in db.query(NeedProfile).all()]


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
def get_keywords(need_id: str = "sec_events", db: Session = Depends(get_session),
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
    need_id: str = "sec_events"
    content: dict


@api.put("/keywords")
def put_keywords(body: KeywordsIn, db: Session = Depends(get_session),
                 user: AppUser = Depends(require_roles("admin", "analyst"))):
    """保存关键词矩阵为新版本并激活;返回展开后实际查询条数。"""
    from app.models import KeywordSet
    from app.services.scheduler import expand_queries
    content = dict(body.content or {})
    # 版本号自增(基于已有最大数字版本)
    existing = db.query(KeywordSet).filter_by(need_id=body.need_id).all()
    nums = [float(k.version) for k in existing if str(k.version).replace(".", "").isdigit()]
    content["version"] = str(round((max(nums) if nums else 0) + 0.1, 1))
    db.query(KeywordSet).filter_by(need_id=body.need_id).update({"is_active": False})
    ks = KeywordSet(need_id=body.need_id, version=content["version"], content=content, is_active=True)
    from datetime import datetime
    ks.published_at = datetime.utcnow()
    db.add(ks)
    db.add(AuditLog(user_id=user.id, action="keywords.update", target=body.need_id,
                    detail={"version": content["version"]}))
    db.commit()
    expanded = expand_queries(content)
    return {"ok": True, "version": content["version"], "expanded_count": len(expanded),
            "sample": expanded[:20]}


# ---------- 采集触发与运行记录(前端"采集"页) ----------

class CrawlIn(BaseModel):
    need_id: str = "sec_events"
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
    running = crawl_runner.has_running(db, body.need_id)
    if running:
        return {"job_id": running.id, "already_running": True, "job": _job_dict(running)}
    jid = crawl_runner.start_job(body.need_id, body.limit_sources, user.id)
    return {"job_id": jid, "already_running": False}


@api.get("/crawl/current")
def crawl_current(need_id: str = "sec_events", db: Session = Depends(get_session),
                  _: AppUser = Depends(current_user)):
    """当前/最近一次采集任务的状态与进度(任何页面/刷新都能查到是否在运行)。"""
    from app.services import crawl_runner
    return {"job": _job_dict(crawl_runner.current_job(db, need_id))}


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
def demo_seed(need_id: str = "sec_events", db: Session = Depends(get_session),
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
    src = db.query(Source).first()
    # 幂等:已注入过演示数据则不再重复,避免"采集文档"数字反复累加
    existed = db.query(RawDocument).filter(
        RawDocument.need_id == need_id,
        RawDocument.url.like("https://demo.local/%")).count()
    if existed:
        return {"created": [], "published": [],
                "note": f"演示数据已存在({existed} 条),未重复注入。"}

    samples = [
        ("某三甲医院遭勒索攻击 HIS系统瘫痪36小时",
         "某市第三人民医院遭勒索软件攻击,HIS 系统瘫痪超过36小时,门诊停诊。"
         "攻击者要求支付200万元赎金,医院未支付,数据由备份恢复,部分备份也被加密。"
         "初步判断与某VPN设备未修补漏洞有关,监管部门已介入。"),
        ("某城商行网银系统遭DDoS攻击 交易中断3小时",
         "某城市商业银行网上银行遭大规模DDoS攻击,交易系统中断约3小时,大量客户无法转账。"
         "银行称已启用流量清洗,未造成资金损失。"),
        ("某车企供应商数据泄露 涉及生产数据",
         "某汽车零部件供应商因第三方运维通道被入侵导致生产数据泄露,"
         "攻击者在泄露站列名索要赎金。企业尚未公开回应。"),
    ]
    created, published = [], []
    for i, (title, text) in enumerate(samples):
        url = f"https://demo.local/seed-{datetime.utcnow():%Y%m%d%H%M%S}-{i}"
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
            "note": "已注入演示事件;第1条已走完复核发布并生成回访与线索"}
