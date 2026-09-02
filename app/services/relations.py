"""记录关系抽取(借鉴 data-collector 的引用关系图谱,做成平台能力):

从正文里找"废止/替代/修订/依据/引用"关系的提法,连同被提及的《文件名》或标准号,
写成 payload.related_docs = [{relation, title, evidence}],并在库内按标题匹配到已有记录时落
RecordRelation 边。文档型需求(法规/政策/标准)可把 "relations" 加进 pipeline.stages;
也可对单条记录独立调用(capability relations.extract)。规则可由画像 quality.relations 覆盖。
"""
from __future__ import annotations

import re

from app.services import need_ctx

# 关系触发词 → 关系类型(平台缺省;画像 quality.relations.patterns 可覆盖/追加)
_DEFAULT_PATTERNS = [
    {"relation": "repeals", "regex": r"(?:废止|同时废止|自本.{0,6}施行之日起废止)[^。;;]{0,30}?《([^》]{3,80})》"},
    {"relation": "repeals", "regex": r"《([^》]{3,80})》[^。;;]{0,15}?(?:同时废止|予以废止|自行废止)"},
    {"relation": "supersedes", "regex": r"(?:代替|替代|取代)[^。;;]{0,20}?((?:GB|GB/T|GB/Z|JR/T|YD/T|DB\d{2}(?:/T)?)\s?\d{3,6}(?:[.\-]\d{1,4})*)"},
    {"relation": "supersedes", "regex": r"(?:代替|替代|取代)[^。;;]{0,20}?《([^》]{3,80})》"},
    {"relation": "amends", "regex": r"(?:修订|修改|修正)[^。;;]{0,15}?《([^》]{3,80})》"},
    {"relation": "references", "regex": r"(?:根据|依据|按照|遵照)[^。;;]{0,15}?《([^》]{3,80})》"},
    {"relation": "references", "regex": r"(?:根据|依据|按照|参照)[^。;;]{0,20}?((?:GB|GB/T|GB/Z|JR/T|YD/T)\s?\d{3,6}(?:[.\-]\d{1,4})*)"},
]
RELATION_LABELS = {"repeals": "废止", "supersedes": "替代", "amends": "修订", "references": "依据/引用",
                   "parent": "上位", "child": "下位"}


def _patterns(ctx) -> list[dict]:
    rel = ((ctx.quality.get("relations") if ctx is not None else None) or {})
    pats = list(rel.get("patterns") or [])
    if not pats or rel.get("append_default", True):
        pats = pats + _DEFAULT_PATTERNS
    out = []
    for p in pats:
        try:
            out.append((str(p.get("relation") or "references"), re.compile(str(p["regex"]))))
        except (re.error, KeyError):
            continue
    return out


def extract(text: str, ctx=None, own_title: str | None = None, limit: int = 30) -> list[dict]:
    """正文 → [{relation, title, evidence}](去重;不指向自己)。"""
    text = text or ""
    seen, out = set(), []
    own = (own_title or "").strip()
    for relation, rx in _patterns(ctx):
        for m in rx.finditer(text):
            title = " ".join(m.group(1).split())
            if not title or title == own or (own and title in own and len(title) < len(own) - 2):
                continue
            key = (relation, title)
            if key in seen:
                continue
            seen.add(key)
            s, e = max(0, m.start() - 20), min(len(text), m.end() + 20)
            out.append({"relation": relation, "title": title, "evidence": text[s:e].replace("\n", " ")})
            if len(out) >= limit:
                return out
    return out


def link(db, ev, related: list[dict], ctx=None) -> list:
    """把抽到的关系落 RecordRelation:目标标题能在同需求库内匹配到记录就连上 target_event_id。"""
    from app.models import Event, RecordRelation
    c = ctx or need_ctx.get(db, ev.need_id)
    rows = []
    for r in related:
        title = str(r.get("title") or "").strip()
        if not title:
            continue
        target = None
        for cand in db.query(Event).filter(Event.need_id == ev.need_id).all():
            t = str((cand.payload or {}).get("title") or "")
            key = c.get_role(cand.payload or {}, "subject_key")
            if cand.event_id != ev.event_id and (title in t or t in title and len(t) >= 6 or (key and str(key) == title)):
                target = cand
                break
        exists = db.query(RecordRelation).filter_by(source_event_id=ev.event_id, relation=r["relation"],
                                                    target_title=title).first()
        if exists:
            if target and not exists.target_event_id:
                exists.target_event_id = target.event_id
            rows.append(exists)
            continue
        row = RecordRelation(need_id=ev.need_id, source_event_id=ev.event_id,
                             target_event_id=target.event_id if target else None,
                             target_title=title, relation=r["relation"], evidence=str(r.get("evidence") or "")[:500])
        db.add(row)
        rows.append(row)
    db.flush()
    return rows


def for_event(db, event_id: str) -> dict:
    """某记录的上下游关系(出边 + 入边)。"""
    from app.models import RecordRelation
    out = [{"relation": r.relation, "label": RELATION_LABELS.get(r.relation, r.relation),
            "target_event_id": r.target_event_id, "target_title": r.target_title, "evidence": r.evidence}
           for r in db.query(RecordRelation).filter_by(source_event_id=event_id).all()]
    inc = [{"relation": r.relation, "label": RELATION_LABELS.get(r.relation, r.relation),
            "source_event_id": r.source_event_id, "evidence": r.evidence}
           for r in db.query(RecordRelation).filter_by(target_event_id=event_id).all()]
    return {"event_id": event_id, "outgoing": out, "incoming": inc}
