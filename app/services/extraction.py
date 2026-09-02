"""粗筛 + 结构化抽取(M3,通用):LLM 产出 → schema 校验 → 断言三态守卫。提示词/守卫/完备度权重全部来自 NeedContext。"""
import json
from pathlib import Path

import jsonschema

from app.services import need_ctx
from app.services.llm import get_llm, get_screen_llm
from app.services.money_guard import apply_guard
from app.services.prompts import extract_prompts, screen_prompts

_SCHEMA_CACHE: dict[str, dict] = {}


def load_record_schema(path: str | Path) -> dict:
    key = str(path)
    if key not in _SCHEMA_CACHE:
        _SCHEMA_CACHE[key] = json.loads(Path(path).read_text(encoding="utf-8"))
    return _SCHEMA_CACHE[key]


def screen_document(profile_cfg: dict, title: str, text: str, ctx=None) -> dict:
    """粗筛:{'is_candidate': bool, 'confidence': float, 'reason': str}"""
    system, user = screen_prompts(profile_cfg, title or "", text or "", ctx=ctx)
    out = get_screen_llm().complete_json(system, user)   # 粗筛用小模型(配了才生效),省钱提速
    is_cand = bool(out.get("is_candidate"))
    # 阈值必须用「相关度」而不是「判断置信度」。历史上二者被混用:模型判"不相关"且很有把握时
    # confidence=0.99 反而 ≥ 待定阈值,导致越确信不相关越被塞进人工队列(实测 91% 文档中招)。
    rel = out.get("relevance")
    if rel is None:                       # 兼容未输出 relevance 的模型
        conf = float(out.get("confidence") or 0)
        rel = conf if is_cand else 1.0 - conf   # 判不相关时:越有把握 → 相关度越低
    return {
        "is_candidate": is_cand,
        "confidence": max(0.0, min(1.0, float(rel))),   # 对外仍叫 confidence(相关度语义)
        "judge_confidence": float(out.get("confidence") or 0),
        "reason": str(out.get("reason") or ""),
    }


# 系统信封字段:抽取阶段不由 LLM 产出,发布时由 events.full_record 注入
_ENVELOPE_FIELDS = {"event_id", "status", "review", "confidence_overall",
                    "completeness_score", "change_log", "sources"}


def validate_payload(payload: dict, record_schema: dict, strict: bool = False, ctx=None) -> list[str]:
    """schema 校验:strict=False(草稿)忽略系统信封字段,只报内容问题;strict=True(发布)全量。"""
    validator = jsonschema.Draft202012Validator(record_schema)
    errors = []
    envelope = set(_ENVELOPE_FIELDS) | (set(ctx.envelope_fields) if ctx is not None else set())
    clean = {k: v for k, v in payload.items() if not k.startswith("_")}
    for err in validator.iter_errors(clean):
        top = err.absolute_path[0] if err.absolute_path else None
        if not strict and top is None and err.validator == "required":
            missing = err.message.split("'")[1] if "'" in err.message else ""
            if missing in envelope:
                continue  # 草稿阶段不报系统信封字段缺失
        errors.append(f"{'/'.join(str(p) for p in err.absolute_path) or '(root)'}: {err.message[:160]}")
    return errors


def extract_record(profile_cfg: dict, dictionaries: dict, record_schema: dict,
                   title: str, text: str, ctx=None) -> dict:
    """抽取 + 守卫。返回 {'payload', 'violations', 'schema_errors', 'guard_demoted'}"""
    c = ctx or need_ctx.from_config(profile_cfg or {})
    system, user = extract_prompts(profile_cfg, dictionaries, record_schema, title or "", text or "", ctx=c)
    payload = get_llm().complete_json(system, user)
    if not isinstance(payload, dict):
        payload = {}
    guard = apply_guard(payload, full_text=text or "", ctx=c)
    schema_errors = validate_payload(guard.payload, record_schema, strict=False, ctx=c)
    return {
        "payload": guard.payload,
        "violations": guard.violations,
        "guard_demoted": guard.demoted_fields,
        "schema_errors": schema_errors,
    }


_UNINFORMATIVE = ("未披露", "未知", "不明", "未定级", "无", "N/A", "n/a")


def _weights_for(ctx, record_schema: dict | None) -> dict:
    """完备度权重:画像 completeness_weights;缺省 = Schema required(去信封字段)等权;再缺省 = 最少可发布字段。"""
    w = dict(ctx.completeness_weights) if ctx is not None else {}
    if w:
        return w
    req = []
    if record_schema is None and ctx is not None:
        try:
            record_schema = ctx.record_schema()
        except (OSError, ValueError):
            record_schema = None
    if record_schema:
        req = [f for f in (record_schema.get("required") or []) if f not in _ENVELOPE_FIELDS]
    if not req and ctx is not None:
        req = [f for f in ctx.min_publish_fields if f not in _ENVELOPE_FIELDS]
    return {f: 1 for f in req}


def completeness_score(payload: dict, min_fields: list[str] | None = None, ctx=None,
                       record_schema: dict | None = None) -> float:
    """字段完备度(0-100):画像声明各决策字段的权重;明确写『未披露』也给过程分(0.3)。"""
    c = ctx or need_ctx.get(None, need_ctx.default_need_id())
    weights = _weights_for(c, record_schema) if not min_fields else {f: 1 for f in min_fields}
    total = sum(weights.values())
    if not total:
        return 0.0
    got = 0.0
    for field, w in weights.items():
        v = payload.get(field) if isinstance(payload, dict) else None
        if v in (None, "", [], {}):
            continue
        if isinstance(v, dict):
            status = v.get("status") or v.get("level") or v.get("category")
            filled = any(val not in (None, "", [], {}) for k, val in v.items()
                         if k not in ("status",)) or (status and status not in _UNINFORMATIVE)
            got += w if filled else w * 0.3
        elif isinstance(v, list):
            informative = [x for x in v if str(x) not in _UNINFORMATIVE]
            got += w if informative else w * 0.3
        elif isinstance(v, str) and v.strip() in _UNINFORMATIVE:
            got += w * 0.3
        else:
            got += w
    return round(got / total * 100, 1)
