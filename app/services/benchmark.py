"""对标基准与漏报率(找得全的度量):把一份"官方汇总/人工抽样"的基准清单拿来和库里比,算召回。

每条基准项按 URL(归一化)→ 已采文档 → 记录,或按标题相似度 → 记录 匹配;
未匹配的分三类:doc_only(采到了但没成记录:粗筛/抽取/去重环节丢)、not_found(根本没采到:源/关键词缺口)。
结果落 BenchmarkBatch / BenchmarkItem,评分卡的完整性会把最近一次召回算进去。
"""
from __future__ import annotations

import difflib
import re

from app.services import need_ctx, url_tools

_CJK = re.compile(r"[\u4e00-\u9fffA-Za-z0-9]+")


def _norm_title(t: str) -> str:
    return "".join(_CJK.findall(str(t or "")))


def _similar(a: str, b: str) -> float:
    a, b = _norm_title(a), _norm_title(b)
    if not a or not b:
        return 0.0
    if a in b or b in a:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def run(db, ctx, name: str, items: list[dict], period: str | None = None, source_desc: str | None = None,
        title_threshold: float = 0.8) -> dict:
    from datetime import datetime
    from app.models import BenchmarkBatch, BenchmarkItem, Event, EventSource, RawDocument
    c = ctx or need_ctx.get(db, None)
    batch = BenchmarkBatch(need_id=c.id, name=name, period=period or datetime.utcnow().strftime("%Y-%m"),
                           source_desc=source_desc)
    db.add(batch)
    db.flush()
    events = db.query(Event).filter(Event.need_id == c.id).all()
    ev_titles = [(e, str((e.payload or {}).get("title") or "")) for e in events]
    out_items, matched = [], 0
    for it in items:
        title = str(it.get("title") or it.get("summary") or "").strip()
        url = str(it.get("url") or "").strip()
        ev_id, reason = None, "not_found"
        if url:
            norm = url_tools.normalize_url(url)
            doc = db.query(RawDocument).filter(RawDocument.need_id == c.id,
                                               RawDocument.url_normalized == norm).first()
            if doc is not None:
                es = db.query(EventSource).filter_by(doc_id=doc.id).first()
                if es:
                    ev_id = es.event_id
                else:
                    reason = "doc_only"
        if ev_id is None and title:
            best, score = None, 0.0
            for e, t in ev_titles:
                sc = _similar(title, t)
                if sc > score:
                    best, score = e, sc
            if best is not None and score >= title_threshold:
                ev_id = best.event_id
            elif reason == "not_found" and title:
                # 采到过同题文档但没成记录?
                for d in db.query(RawDocument).filter(RawDocument.need_id == c.id).all():
                    if _similar(title, d.title or "") >= title_threshold:
                        reason = "doc_only"
                        break
        missed = ev_id is None
        matched += 0 if missed else 1
        db.add(BenchmarkItem(batch_id=batch.id, summary=(title or url)[:1000], matched_event_id=ev_id,
                             is_missed=missed, miss_reason=(reason if missed else None)))
        out_items.append({"title": title, "url": url, "matched_event_id": ev_id,
                          "missed": missed, "miss_reason": reason if missed else None})
    db.flush()
    total = len(items)
    recall = round(matched / total, 4) if total else None
    return {"batch_id": batch.id, "name": name, "period": batch.period, "total": total, "matched": matched,
            "missed": total - matched, "recall": recall, "miss_rate": (round(1 - recall, 4) if recall is not None else None),
            "by_reason": {k: sum(1 for x in out_items if x["miss_reason"] == k) for k in ("doc_only", "not_found")},
            "items": out_items}


def latest(db, need_id: str) -> dict | None:
    from app.models import BenchmarkBatch, BenchmarkItem
    b = db.query(BenchmarkBatch).filter_by(need_id=need_id).order_by(BenchmarkBatch.created_at.desc()).first()
    if not b:
        return None
    rows = db.query(BenchmarkItem).filter_by(batch_id=b.id).all()
    total = len(rows)
    matched = sum(1 for r in rows if not r.is_missed)
    return {"batch_id": b.id, "name": b.name, "period": b.period, "total": total, "matched": matched,
            "recall": (round(matched / total, 4) if total else None),
            "created_at": b.created_at.isoformat(timespec="seconds") if b.created_at else None}


def history(db, need_id: str, limit: int = 12) -> list[dict]:
    from app.models import BenchmarkBatch, BenchmarkItem
    out = []
    for b in (db.query(BenchmarkBatch).filter_by(need_id=need_id)
              .order_by(BenchmarkBatch.created_at.desc()).limit(limit).all()):
        rows = db.query(BenchmarkItem).filter_by(batch_id=b.id).all()
        total = len(rows)
        out.append({"batch_id": b.id, "name": b.name, "period": b.period, "total": total,
                    "recall": (round(sum(1 for r in rows if not r.is_missed) / total, 4) if total else None)})
    return out
