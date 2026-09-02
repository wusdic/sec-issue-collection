"""能力注册表:把平台的底层能力登记成"名字 → 可独立调用的函数",统一签名 fn(db, ctx, **params)。

用途:
- 组合:流水线/自动运维按画像声明的阶段名调用(见 pipeline.STAGES / autopilot.TASKS);
- 独立调用:`python -m app.cli cap-run <name> --need <id> --params '{...}'`、`POST /capabilities/{name}/run`,
  每个能力都能脱离流水线单跑(调试一个源、试一段正文的粗筛/抽取、只生成关键词、只跑找源…);
- 自描述:`cap-list` / `GET /capabilities` 列出名字、说明、参数,前端与外部程序据此拼装调用。
能力函数只依赖 NeedContext 取参,不读画像文件;新增能力只需在这里登记。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from app.services import need_ctx


@dataclass
class Capability:
    name: str
    group: str
    doc: str
    fn: Callable
    params: dict = field(default_factory=dict)      # 参数名 → 说明


_REG: dict[str, Capability] = {}


def register(name: str, group: str, doc: str, params: dict | None = None):
    def deco(fn):
        _REG[name] = Capability(name=name, group=group, doc=doc, fn=fn, params=params or {})
        return fn
    return deco


def list_capabilities() -> list[dict]:
    return [{"name": c.name, "group": c.group, "doc": c.doc, "params": c.params}
            for c in sorted(_REG.values(), key=lambda c: (c.group, c.name))]


def run(name: str, db, need_id: str | None = None, ctx=None, **params):
    cap = _REG.get(name)
    if cap is None:
        raise KeyError(f"未知能力:{name}(可用:{', '.join(sorted(_REG))})")
    c = ctx or need_ctx.get(db, need_id or need_ctx.default_need_id())
    return cap.fn(db, c, **params)


# ---------------- 采集层 ----------------

@register("fetch", "采集", "抓一个 URL(含渲染偏好),返回状态/最终链接/正文长度/标题", {"url": "地址", "render": "auto|true|false"})
def _fetch(db, ctx, url: str, render="auto"):
    from app.services import archive, fetcher
    fr = fetcher.fetch(url, render=render)
    text = archive.extract_text(fr.html) if fr.ok else ""
    return {"ok": fr.ok, "status": fr.status, "final_url": fr.final_url, "error": fr.error,
            "text_len": len(text or ""), "text_head": (text or "")[:500]}


@register("columns.find", "采集", "从站点根页识别与需求相关的栏目(按画像栏目提示词打分)", {"url": "站点根地址"})
def _columns_find(db, ctx, url: str):
    from app.services import columns, fetcher
    fr = fetcher.fetch(url, render="auto")
    if not fr.ok:
        return {"ok": False, "error": fr.error or fr.status, "columns": []}
    return {"ok": True, "columns": columns.find_columns(fr.html, fr.final_url or url, ctx=ctx)}


# ---------------- 处理层 ----------------

@register("screen", "处理", "粗筛一段内容是否属于本需求(画像 quality.screen + scope)", {"title": "标题", "text": "正文"})
def _screen(db, ctx, title: str = "", text: str = ""):
    from app.services.extraction import screen_document
    return screen_document(ctx.raw, title, text, ctx=ctx)


@register("extract", "处理", "按记录 Schema(或轻量 Schema)抽取 + 三态守卫 + Schema 校验", {"title": "标题", "text": "正文"})
def _extract(db, ctx, title: str = "", text: str = ""):
    from app.services.extraction import extract_record
    from app.services.profiles import get_active_dictionaries
    dicts = get_active_dictionaries(db, ctx.id) if db is not None else {}
    return extract_record(ctx.raw, dicts, ctx.record_schema(), title, text, ctx=ctx)


@register("guard", "处理", "对一条记录执行断言三态守卫(画像 quality.assertions)", {"payload": "记录 JSON", "text": "证据正文"})
def _guard(db, ctx, payload: dict, text: str = ""):
    from app.services.money_guard import apply_guard
    r = apply_guard(dict(payload or {}), full_text=text, ctx=ctx)
    return {"payload": r.payload, "violations": r.violations, "demoted": r.demoted_fields}


@register("scope_gate", "处理", "范畴闸门:record_type / 排除正则 / require_mention", {"payload": "记录 JSON", "title": "标题", "text": "正文"})
def _scope_gate(db, ctx, payload: dict | None = None, title: str = "", text: str = ""):
    from app.services.pipeline import _is_out_of_scope, _out_of_scope_reason
    out = _is_out_of_scope(payload or {}, title, text, ctx)
    return {"out_of_scope": out, "reason": _out_of_scope_reason(ctx) if out else ""}


@register("dedup.record", "处理", "记录级指纹去重:主体键+类型交集+时间窗(画像 record.dedup)", {"payload": "记录 JSON"})
def _dedup_record(db, ctx, payload: dict):
    from app.services import dedup
    ev = dedup.fingerprint_match(db, ctx.id, payload or {}, ctx=ctx)
    return {"matched_event": ev.event_id if ev else None, "subject_key": dedup.subject_key(payload or {}, ctx)}


@register("process_document", "处理", "对库里一篇文档跑画像声明的全部阶段", {"doc_id": "RawDocument.id"})
def _process_document(db, ctx, doc_id: int):
    from app.models import NeedProfile, RawDocument
    from app.services.pipeline import process_document
    doc = db.get(RawDocument, int(doc_id))
    need = db.get(NeedProfile, ctx.id)
    if doc is None or need is None:
        return {"error": "文档或需求不存在"}
    return process_document(db, need, doc)


# ---------------- 关键词 / 找源 ----------------

@register("keywords.groups", "关键词", "按 scope/静态词组/监控名单合成词组(可选模型扩展)", {"expand": "true=模型补同义词"})
def _kw_groups(db, ctx, expand: bool | None = None):
    from app.services import keywords
    return keywords.term_groups(ctx, db, expand)


@register("keywords.generate", "关键词", "生成关键词矩阵并存为生效版本", {"expand": "true=模型补同义词", "persist": "默认 true"})
def _kw_generate(db, ctx, expand: bool | None = None, persist: bool = True):
    from app.services import keywords
    content, ks = keywords.generate(db, ctx, expand, persist)
    return {"version": content["version"], "groups": {k: len(v) for k, v in keywords.content_groups(content).items()},
            "preview": content.get("preview"), "persisted": ks is not None}


@register("keywords.expand", "关键词", "把矩阵内容展开成查询列表", {"content": "矩阵 JSON(缺省=生效版本)"})
def _kw_expand(db, ctx, content: dict | None = None):
    from app.services import keywords
    if content is None and db is not None:
        from app.models import KeywordSet
        ks = db.query(KeywordSet).filter_by(need_id=ctx.id, is_active=True).first()
        content = ks.content if ks else {}
    qs = keywords.expand_queries(content or {}, ctx)
    return {"count": len(qs), "queries": qs}


@register("prospect.queries", "找源", "本轮找源词(基础词 + 覆盖空白词 + 配方组合,经进化机制排期)", {})
def _prospect_queries(db, ctx):
    from app.services import prospect
    return {"queries": prospect.build_queries(db, ctx.id)}


@register("prospect.run", "找源", "跑一轮主动找源(搜索引擎 → 候选渠道)", {})
def _prospect_run(db, ctx):
    from app.services import prospect
    return prospect.run_once(db, ctx.id)


@register("coverage.summary", "找源", "覆盖度盘点(按画像覆盖维度)", {"days": "窗口天数"})
def _coverage(db, ctx, days: int | None = None):
    from app.services import coverage
    return coverage.summary(db, ctx.id, days, ctx=ctx)


# ---------------- 更新 / 输出 ----------------

@register("followup.schedule", "更新", "给一条记录按画像触发器生成回访任务", {"event_id": "记录号"})
def _followup(db, ctx, event_id: str):
    from app.models import Event
    from app.services.followup import schedule_followups
    ev = db.get(Event, event_id)
    return {"tasks": [{"due": str(t.due_date), "reason": t.reason} for t in schedule_followups(db, ev, ctx=ctx)]} if ev else {"error": "记录不存在"}


@register("leads.generate", "输出", "给一条记录生成线索(画像 leads_engine.enabled 才生效)", {"event_id": "记录号"})
def _leads(db, ctx, event_id: str):
    from app.models import Event
    from app.services.leads import generate_leads
    ev = db.get(Event, event_id)
    return {"leads": [{"org": l.target_org, "score": l.score, "stage": l.window_stage} for l in generate_leads(db, ev, ctx=ctx)]} if ev else {"error": "记录不存在"}


@register("reports.heatmap", "输出", "交叉表(画像 reports_engine.crosstab)", {"days": "窗口"})
def _heatmap(db, ctx, days: int = 365):
    from app.services import kpi
    return kpi.heatmap(db, ctx.id, days, ctx=ctx)


@register("reports.amount", "输出", "金额汇总(三态/普通数值,画像 amount_sum)", {"scope": "confirmed|claimed|estimated"})
def _amount(db, ctx, scope: str | None = None):
    from app.services import kpi
    return kpi.amount_stats(db, ctx.id, scope, ctx=ctx)


@register("digest.build", "输出", "生成某天日报内容 + Markdown", {"day": "YYYY-MM-DD(缺省今天)"})
def _digest(db, ctx, day: str | None = None):
    from datetime import date, datetime
    from app.services import digest
    d = date.fromisoformat(day) if day else datetime.utcnow().date()
    content = digest.build_content(db, ctx.id, d, ctx=ctx)
    return {"content": content, "markdown": digest.render_markdown(content)}


# ---------------- 验证 / 关系 / 导出 / 质量(v1.3,借鉴同类项目) ----------------

@register("verify", "处理", "真实性验证:官方域可信度 / 正文哈希 / 标题一致 / 密级标记", {"url": "地址", "title": "标题", "text": "正文"})
def _verify(db, ctx, url: str = "", title: str = "", text: str = ""):
    from app.services import verify
    return verify.verify_text(url, title, text, ctx)


@register("verify.recheck", "更新", "再核查一篇文档:重抓并比对正文哈希,判断内容是否变化", {"doc_id": "RawDocument.id"})
def _recheck(db, ctx, doc_id: int):
    from app.models import RawDocument
    from app.services import verify
    doc = db.get(RawDocument, int(doc_id))
    return verify.recheck(doc, ctx) if doc else {"ok": False, "error": "文档不存在"}


@register("relations.extract", "处理", "从正文抽取 废止/替代/修订/依据 关系,并与库内记录连边", {"event_id": "记录号(可省:只抽不连)", "text": "正文(省略则用记录来源文档)"})
def _relations(db, ctx, event_id: str | None = None, text: str | None = None):
    from app.models import Event, EventSource, RawDocument
    from app.services import relations
    ev = db.get(Event, event_id) if event_id else None
    if text is None and ev is not None:
        es = db.query(EventSource).filter_by(event_id=ev.event_id).first()
        doc = db.get(RawDocument, es.doc_id) if es and es.doc_id else None
        text = (doc.content_text if doc else "") or ""
    rel = relations.extract(text or "", ctx, own_title=(ev.payload or {}).get("title") if ev else None)
    linked = relations.link(db, ev, rel, ctx) if ev else []
    return {"related_docs": rel, "linked": [{"relation": r.relation, "target": r.target_event_id or r.target_title} for r in linked]}


@register("exports.run", "输出", "按画像 outputs.exports 把已发布记录导出(飞书多维表格等)", {"name": "导出名(可省)", "dry_run": "只渲染不写"})
def _exports(db, ctx, name: str | None = None, dry_run: bool = False):
    from app.services import exports
    return exports.run(db, ctx, name=name, dry_run=dry_run)


@register("quality.scorecard", "输出", "数据质量评分卡:完整性/准确性/一致性/时效性加权,A–D 定级", {"days": "覆盖度窗口"})
def _scorecard(db, ctx, days: int | None = None):
    from app.services import kpi
    return kpi.quality_scorecard(db, ctx.id, days, ctx=ctx)


@register("notify.send", "组件", "通知组件:按渠道(email/feishu/webhook…;缺省=画像或设置里配置的)发送一段文本", {"subject": "标题", "text": "内容", "channels": "渠道列表(可省)"})
def _notify_send(db, ctx, subject: str = "通知", text: str = "", channels: list | None = None):
    from app.services import notify
    return notify.send(subject, text, channels, ctx)


@register("notify.feishu", "组件", "推一条文本到飞书群机器人(设置页 feishu_webhook)", {"text": "内容"})
def _notify_feishu(db, ctx, text: str):
    from app.services import notify
    ok, note = notify.deliver_feishu(text)
    return {"ok": ok, "note": note}
