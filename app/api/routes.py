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
             "credibility": s.credibility, "tier": s.tier, "lifecycle": s.lifecycle,
             "identity_key": s.identity_key, "discovery_score": s.discovery_score,
             "manual_assist": s.manual_assist, "docs_total": s.stat_docs_total}
            for s in q.order_by(Source.id).all()]


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
def list_documents(need_id: str, status: str | None = None, limit: int = 50,
                   db: Session = Depends(get_session), _: AppUser = Depends(current_user)):
    q = db.query(RawDocument).filter_by(need_id=need_id)
    if status:
        q = q.filter_by(screen_status=status)
    return [{"id": d.id, "title": d.title, "url": d.url, "publisher": d.publisher,
             "screen_status": d.screen_status, "screen_score": d.screen_score,
             "is_primary": d.is_primary, "snapshot_id": d.snapshot_id}
            for d in q.order_by(RawDocument.id.desc()).limit(limit).all()]


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
    if status:
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
def review_queue(stage: str = "first_review", db: Session = Depends(get_session),
                 _: AppUser = Depends(current_user)):
    rows = db.query(ReviewTask).filter_by(stage=stage).order_by(ReviewTask.updated_at).all()
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
