"""真实性验证(借鉴 data-collector 的 verification 引擎,按平台方式参数化):

对一篇采集文档给出 verification = {status, domain_trust, content_hash, title_consistent, sensitive, notes}。
- domain_trust:high(官方后缀 / 画像官方域名录)/ medium(机构域)/ low;
- content_hash:正文 SHA256,供"再核查"比对版本是否变化(recheck);
- title_consistent:标题核心词是否出现在正文里(防抓错页/占位页);
- sensitive:含"内部/密级/不对外公开"等标记 → 只登记不入库正文,转人工。
status:verified / pending_review / unverified。
参数:sources.verification(official_suffixes / official_domains / medium_suffixes / sensitive_markers),
缺省用平台常量;画像 sources.grading.official_domains 也计入官方域。
"""
from __future__ import annotations

import hashlib
import re
from urllib.parse import urlparse

from app.services import need_ctx

_OFFICIAL_SUFFIXES = (".gov.cn", ".mil.cn", ".gov")
_MEDIUM_SUFFIXES = (".org.cn", ".edu.cn", ".ac.cn", ".org")
_SENSITIVE = ("内部资料", "内部文件", "密级", "机密", "秘密", "不对外公开", "仅限内部", "内部使用")
_CJK = re.compile(r"[\u4e00-\u9fff]+")


def _cfg(ctx) -> dict:
    v = dict((ctx.sources_cfg.get("verification") or {}) if ctx is not None else {})
    official = set(str(x).lower() for x in (v.get("official_domains") or []))
    if ctx is not None:
        official |= {str(d).lower() for d in (ctx.grading.get("official_domains") or [])}
    return {
        "official_suffixes": tuple(v.get("official_suffixes") or _OFFICIAL_SUFFIXES),
        "medium_suffixes": tuple(v.get("medium_suffixes") or _MEDIUM_SUFFIXES),
        "official_domains": official,
        "sensitive_markers": tuple(v.get("sensitive_markers") or _SENSITIVE),
    }


def domain_trust(url: str, ctx=None) -> str:
    host = (urlparse(url or "").netloc or "").lower().split(":")[0]
    if not host:
        return "unknown"
    c = _cfg(ctx)
    if host.endswith(c["official_suffixes"]) or any(host == d or host.endswith("." + d) for d in c["official_domains"]):
        return "high"
    if host.endswith(c["medium_suffixes"]):
        return "medium"
    return "low"


def content_hash(text: str | None) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def title_consistent(title: str | None, text: str | None) -> bool:
    """标题核心汉字串(前 8 字)出现在正文里即认为一致;标题太短不判。"""
    core = "".join(_CJK.findall(title or ""))
    if len(core) < 4 or not text:
        return True
    return core[:8] in text or core[:6] in text


def verify_text(url: str, title: str | None, text: str | None, ctx=None,
                expected_hash: str | None = None) -> dict:
    c = _cfg(ctx)
    trust = domain_trust(url, ctx)
    h = content_hash(text)
    consistent = title_consistent(title, text)
    sensitive = any(m in (text or "")[:20000] for m in c["sensitive_markers"])
    notes = []
    if expected_hash and expected_hash != h:
        notes.append("内容较上次已变化")
    if not consistent:
        notes.append("标题与正文可能不一致(疑似抓到列表页/占位页)")
    if sensitive:
        notes.append("含内部/密级标记,只登记不传播,转人工")
    if sensitive:
        status = "pending_review"
    elif trust == "high" and consistent:
        status = "verified"
    elif consistent:
        status = "unverified"           # 非官方来源:内容可用,权威性待复核
    else:
        status = "pending_review"
    return {"status": status, "domain_trust": trust, "content_hash": h,
            "title_consistent": consistent, "sensitive": sensitive,
            "changed": bool(expected_hash and expected_hash != h), "notes": notes}


def verify_document(doc, ctx=None) -> dict:
    """给 RawDocument 打验证信息并写回 doc.verification。"""
    c = ctx or need_ctx.get(None, doc.need_id)
    prev = (doc.verification or {}).get("content_hash") if getattr(doc, "verification", None) else None
    v = verify_text(doc.final_url or doc.url, doc.title, doc.content_text, c, expected_hash=prev)
    doc.verification = v
    return v


def recheck(doc, ctx=None) -> dict:
    """再核查:重新抓取该文档,比对内容哈希是否变化(文档型记录的版本跃迁信号)。"""
    from app.services import archive, fetcher
    c = ctx or need_ctx.get(None, doc.need_id)
    prev = (doc.verification or {}).get("content_hash")
    fr = fetcher.fetch(doc.final_url or doc.url, render="auto")
    if not fr.ok:
        return {"ok": False, "error": fr.error or fr.status, "changed": None}
    text = archive.extract_text(fr.html) or ""
    v = verify_text(fr.final_url or doc.url, doc.title, text, c, expected_hash=prev)
    v["previous_hash"] = prev
    return {"ok": True, **v}
